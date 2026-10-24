"""Native services on Linux via systemd over D-Bus (jeepney).

The portable counterpart to the win32service adapter: reads the systemd units through
``org.freedesktop.systemd1.Manager`` (``ListUnits``, the service unit files, ``LoadUnit`` for an
unloaded one) - the native interface ``systemctl`` itself uses, so no subprocess and no text
scraping. Each ``.service`` unit marshals into the same :class:`ServiceInfo`.

The shared D-Bus plumbing (open the bus, call the Manager, read a Unit property) lives in
:mod:`.systemd_dbus`; this module adds the service-specific mapping and the mutating controller.

Contents:
    * :func:`to_service_state` - systemd ActiveState -> :class:`ServiceState` (pure).
    * :func:`to_start_type` - systemd UnitFileState -> :class:`ServiceStartType` (pure).
    * :func:`iter_services` - stream the ``.service`` units as records.
    * :class:`SystemdServiceController` - start/stop/restart/enable/disable (**mutating**).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from enum import Enum

from ...domain.enums import ServiceStartType, ServiceState
from ...domain.errors import NativeCallError, OperationTimeoutError
from ...domain.records import ServiceInfo
from . import systemd_dbus as _dbus
from .marshal import skip_or_raise
from .systemd_dbus import ActiveState, UnitFileState

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.1  # seconds between polls while a start/stop/restart settles
#: A stop also counts as settled at these ActiveStates, even with its job record not yet gone.
_STOPPED_STATES = frozenset({ActiveState.INACTIVE, ActiveState.FAILED})


class _Job(str, Enum):
    """The systemd Manager method that queues a unit's start, stop or restart job."""

    START = "StartUnit"
    STOP = "StopUnit"
    RESTART = "RestartUnit"


#: systemd ActiveState -> canonical ServiceState (Windows has no failed/reloading, mapped to nearest).
_ACTIVE_STATE = {
    ActiveState.ACTIVE: ServiceState.RUNNING,
    ActiveState.INACTIVE: ServiceState.STOPPED,
    ActiveState.FAILED: ServiceState.STOPPED,
    ActiveState.ACTIVATING: ServiceState.START_PENDING,
    ActiveState.DEACTIVATING: ServiceState.STOP_PENDING,
    ActiveState.RELOADING: ServiceState.CONTINUE_PENDING,
}
#: systemd UnitFileState -> ServiceStartType (static/indirect/... have no exact Windows twin -> Manual).
_UNIT_FILE_STATE = {
    UnitFileState.ENABLED: ServiceStartType.AUTOMATIC,
    UnitFileState.ENABLED_RUNTIME: ServiceStartType.AUTOMATIC,
    UnitFileState.DISABLED: ServiceStartType.DISABLED,
    UnitFileState.MASKED: ServiceStartType.DISABLED,
    UnitFileState.MASKED_RUNTIME: ServiceStartType.DISABLED,
    UnitFileState.STATIC: ServiceStartType.MANUAL,
    UnitFileState.INDIRECT: ServiceStartType.MANUAL,
    UnitFileState.GENERATED: ServiceStartType.MANUAL,
    UnitFileState.TRANSIENT: ServiceStartType.MANUAL,
    UnitFileState.LINKED: ServiceStartType.MANUAL,
    UnitFileState.LINKED_RUNTIME: ServiceStartType.MANUAL,
}


def to_service_state(active_state: ActiveState) -> ServiceState:
    """Map a systemd ``ActiveState`` to the canonical :class:`ServiceState` (an unmapped one is Stopped).

    Example:
        >>> to_service_state(ActiveState.ACTIVE).value, to_service_state(ActiveState.INACTIVE).value
        ('Running', 'Stopped')
    """
    return _ACTIVE_STATE.get(active_state, ServiceState.STOPPED)


def to_start_type(unit_file_state: UnitFileState) -> ServiceStartType | None:
    """Map a systemd ``UnitFileState`` to a :class:`ServiceStartType` (``None`` if unmapped).

    Example:
        >>> to_start_type(UnitFileState.ENABLED).value, to_start_type(UnitFileState.DISABLED).value
        ('Automatic', 'Disabled')
    """
    return _UNIT_FILE_STATE.get(unit_file_state)


def _start_type_of(bus: _dbus.Bus, name: str) -> ServiceStartType | None:
    """GetUnitFileState for one unit; ``None`` for a transient unit that has no unit file."""
    state = _dbus.unit_file_state(bus, name)
    return None if state is None else to_start_type(state)


def _service_at(bus: _dbus.Bus, name: str, path: str | None, *, start_type: ServiceStartType | None) -> ServiceInfo:
    """The record for service ``name`` read from its object ``path`` (None: unreadable, fields default).

    With no ``path`` the state is still read, through ``GetUnit``: ``LoadUnit`` reads a service whose
    file was deleted under it as not-found, yet it can still run, and GetUnit answers for any loaded
    unit whatever its LoadState (an absent one reads inactive).
    """
    active_state = _dbus.active_state(bus, name) if path is None else _dbus.path_active_state(bus, path)
    return ServiceInfo(
        name=name,
        display_name=str(_dbus.path_property(bus, path, "Description", "")),
        status=to_service_state(active_state),
        start_type=start_type,
        can_stop=True,
        can_pause_continue=False,
    )


def _service_files(bus: _dbus.Bus, *, strict: bool) -> list[_dbus.UnitFile]:
    """Every ``.service`` unit file as a :class:`~.systemd_dbus.UnitFile`; [] when no listing answers.

    When no unit-file listing answers, the default keeps the loaded services with a WARNING (every
    unloaded service is then missing); ``strict`` raises instead.
    """
    try:
        return _dbus.unit_files(bus, ".service")
    except NativeCallError as exc:
        what = "cannot list the service unit files, so no unloaded service is listed"
        skip_or_raise(what, exc, strict=strict, log=logger)
        return []


def _service_file_path(bus: _dbus.Bus, name: str, *, strict: bool) -> str | None:
    """The loaded object path of service file ``name``; None for a stale file or (by default) a failed load.

    :func:`~.systemd_dbus.load_unit`, not the fail-soft ``load_unit_path``: a refused ``LoadUnit``
    or an unreadable ``LoadState`` is "could not ask", which must not pass for systemd's own
    "no such unit" answer.
    """
    try:
        return _dbus.load_unit(bus, name)
    except NativeCallError as exc:
        skip_or_raise(f"cannot load service unit '{name}'", exc, strict=strict, log=logger)
        return None


def iter_services(*, strict: bool = False) -> Iterator[ServiceInfo]:
    """Yield a :class:`ServiceInfo` per systemd ``.service`` unit (like Get-Service, Linux).

    The loaded services come first, from ``ListUnits``, which already carries their description
    and state. Then the service unit FILES not among them - a stopped, disabled service is
    unloaded and ``ListUnits`` omits it, while Get-Service lists a stopped service - each read
    through ``LoadUnit``, with its start type taken from the file listing. Skipped as no service:
    a stopped ListUnits entry whose LoadState is not-found or masked
    (:func:`~.systemd_dbus.is_unlisted_unit`; a running one is listed, as Get-Service lists a
    running service marked for deletion), and a masked file, an alias, a template or a file
    ``LoadUnit`` reads as not-found. A skipped entry does not hide its file: a name systemd cached
    as not-found before its file was written is listed from the file (systemd >= 246; an older
    one needs a daemon-reload first). Lazy: ``GetUnitFileState``,
    the unit-file listing and each ``LoadUnit`` run only for consumed services, so
    ``get_service().where(...).take(5)`` over loaded services costs one ``ListUnits`` plus five
    state lookups. Reading an unloaded service loads it transiently; systemd unloads it again when
    nothing references it.

    Args:
        strict: What happens when systemd does not answer a read the listing needs - the
            unit-file listing (without it no unloaded service can be listed), or the ``LoadUnit``
            of one service file or its ``LoadState`` read. False (default) skips what it could not
            read and logs a WARNING naming it; True raises :class:`NativeCallError` naming it, when
            iteration reaches it. The deliberate filtering above (not-found, masked, alias,
            template, stale) is systemd's answer, not a failure, and is the same either way; a
            single property that cannot be read (description, state, start type) still falls back
            to its default in both modes (the service is kept).

    Raises:
        NativeCallError: The system bus or ``ListUnits`` cannot be reached, or (with ``strict``)
            a read named above fails.

    Example:
        >>> callable(iter_services)
        True
        >>> import inspect
        >>> inspect.signature(iter_services).parameters["strict"].default
        False
    """
    bus = _dbus.open_bus()
    try:
        (units,) = _dbus.call(bus, "ListUnits")
        seen: set[str] = set()
        for unit in units:
            name = str(unit[0])
            if not name.endswith(".service"):
                continue
            # ListUnits fields: 1 Description, 2 LoadState, 3 ActiveState.
            active_state = ActiveState(str(unit[3]))
            if _dbus.is_unlisted_unit(_dbus.LoadState(str(unit[2])), active_state):
                # Not marked seen: a cached not-found name whose file appeared since (no
                # daemon-reload) is still listed by the unit-file pass, whose LoadUnit retries the
                # load from disk on systemd >= 246: manager_load_unit_prepare (src/core/manager.c)
                # re-loads a not-found unit when the unit-file cache is stale or was refreshed
                # after its failed load (commits d904afc7 and 7233e91a, both first in v246-rc1;
                # not in NEWS). An older systemd answers not-found until a daemon-reload, so
                # there the unit-file pass skips the name as well.
                continue
            seen.add(name)
            yield ServiceInfo(
                name=name,
                display_name=str(unit[1]),
                status=to_service_state(active_state),
                start_type=_start_type_of(bus, name),
                can_stop=True,
                can_pause_continue=False,
            )
        for name, state in _service_files(bus, strict=strict):
            if name in seen or state in _dbus.UNLISTED_FILE_STATES:
                continue
            seen.add(name)
            path = _service_file_path(bus, name, strict=strict)
            if path is None:  # a stale or broken file (no unit behind the name), or a refused load
                continue
            yield _service_at(bus, name, path, start_type=to_start_type(state))
    finally:
        bus.conn.close()


def _get_one(bus: _dbus.Bus, name: str) -> ServiceInfo:
    """Build a :class:`ServiceInfo` for a single unit (used to report post-mutation state).

    Resolved with ``LoadUnit``, never ``GetUnit``: a stopped or just-disabled service is unloaded,
    and ``GetUnit`` would read its description as ``""``. The unit is resolved once for every
    property read from it.
    """
    return _service_at(bus, name, _dbus.load_unit_path(bus, name), start_type=_start_type_of(bus, name))


class SystemdServiceController:
    """Start/stop/restart systemd units and change their enablement (Linux, **mutating**).

    The counterpart to the win32 NativeServiceController; needs polkit/root, like the Windows
    verbs need admin. Each verb waits for the unit to settle, then returns its :class:`ServiceInfo`;
    a unit that has not settled by the deadline raises :class:`OperationTimeoutError`, as on
    Windows, while a job systemd refuses raises a plain :class:`NativeCallError`. Settled means the
    unit has no pending job (a stop also settles at inactive/failed). A start or restart whose unit
    settles at ``failed`` raises :class:`NativeCallError` naming systemd's ``Service.Result`` (such as
    ``exit-code``), like Start-Service on a service that fails to start; any other settled state is
    reported as it is - inactive for a Type=oneshot unit without RemainAfterExit, which succeeded.
    ``stop(wait=False)`` sends ``StopUnit`` and returns the unit's state right after it.

    Example:
        >>> callable(SystemdServiceController().start)
        True
    """

    def start(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        """Start a unit; wait until its start job is done; return its state (like Start-Service).

        Raises:
            NativeCallError: systemd refused the job, or the unit settled at ``failed``.
            OperationTimeoutError: the unit did not settle within ``timeout`` seconds.
        """
        return self._transition(name, _Job.START, timeout)

    def stop(self, name: str, *, timeout: float = 30.0, wait: bool = True) -> ServiceInfo:
        """Stop a unit; wait until inactive/failed or its job is done; return its state (like Stop-Service).

        With ``wait=False`` the StopUnit job is queued and the unit is read back at once
        (typically ``deactivating``, so ``StopPending``) without polling; ``timeout`` is unused.
        """
        return self._transition(name, _Job.STOP, timeout, wait=wait)

    def restart(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        """Restart a unit; wait until its restart job is done; return its state (like Restart-Service).

        Raises:
            NativeCallError: systemd refused the job, or the unit settled at ``failed``.
            OperationTimeoutError: the unit did not settle within ``timeout`` seconds.
        """
        return self._transition(name, _Job.RESTART, timeout)

    def set_startup(self, name: str, start_type: ServiceStartType) -> ServiceInfo:
        """Enable/disable a unit to match ``start_type`` (like Set-Service -StartupType)."""
        bus = _dbus.open_bus()
        try:
            if start_type is ServiceStartType.AUTOMATIC:
                _dbus.call(bus, "EnableUnitFiles", "asbb", ([name], False, False))
            elif start_type is ServiceStartType.DISABLED:
                _dbus.call(bus, "DisableUnitFiles", "asb", ([name], False))
            else:
                raise NativeCallError(
                    f"systemd has no {start_type.value!r} start type; use Automatic (enable) or Disabled (disable)."
                )
            _dbus.call(bus, "Reload")
            return _get_one(bus, name)
        finally:
            bus.conn.close()

    def _transition(
        self,
        name: str,
        job: _Job,
        timeout: float,
        *,
        wait: bool = True,
    ) -> ServiceInfo:
        """Send ``job`` for ``name``; unless ``wait`` is False, poll until it settles.

        A stop also settles at inactive or failed (both mean stopped); a start or restart that
        settles at ``failed`` raises instead (:func:`_refuse_a_failed_unit`).
        """
        stopping = job is _Job.STOP
        bus = _dbus.open_bus()
        try:
            _dbus.call(bus, job.value, "ss", (name, "replace"))
            if wait:
                _wait_until_settled(bus, name, _STOPPED_STATES if stopping else frozenset(), timeout)
                if not stopping:
                    _refuse_a_failed_unit(bus, name)
            return _get_one(bus, name)
        finally:
            bus.conn.close()


def _refuse_a_failed_unit(bus: _dbus.Bus, name: str) -> None:
    """Raise when the settled unit's ActiveState is ``failed``: its start or restart did not work.

    The job settling only says systemd finished with it; a unit whose process exited non-zero, was
    killed or timed out settles at ``failed``, and returning that as a plain Stopped would read as
    success. ``Service.Result`` names why (``exit-code``, ``signal``, ``timeout``, ...); an
    unreadable one only leaves the reason unnamed, never the failure unreported.
    """
    if _dbus.active_state(bus, name) is not ActiveState.FAILED:
        return
    result = _dbus.unit_property(bus, name, "Result", None, interface=_dbus.SERVICE_IFACE)
    reason = f" (Result={result})" if isinstance(result, str) and result else ""
    raise NativeCallError(f"unit {name!r} failed{reason}: systemd ended its job with the unit in the failed state.")


def _is_settled(bus: _dbus.Bus, name: str, settled_states: frozenset[ActiveState]) -> bool:
    """Whether the unit's job is done, or its ActiveState is already one of ``settled_states``."""
    if not _dbus.has_pending_job(bus, name):
        return True
    return bool(settled_states) and _dbus.active_state(bus, name) in settled_states


def _wait_until_settled(bus: _dbus.Bus, name: str, settled_states: frozenset[ActiveState], timeout: float) -> None:
    """Poll until the unit has no pending job (or reads a ``settled_states`` state); raise at ``timeout``.

    Settling on the job, not on one target state, lets a start whose unit legitimately ends
    inactive (Type=oneshot without RemainAfterExit, a simple process exiting 0) return at once.
    The deadline raises OperationTimeoutError rather than returning the unsettled state, so a
    caller tells a job that is still running (a slow stop) from a job systemd refused, as the win32
    controller does.
    """
    deadline = time.monotonic() + timeout
    while not _is_settled(bus, name, settled_states):
        if time.monotonic() >= deadline:
            raise OperationTimeoutError(f"unit {name!r} did not settle within {timeout}s", timeout=timeout)
        time.sleep(_POLL_INTERVAL)


__all__ = ["SystemdServiceController", "iter_services", "to_service_state", "to_start_type"]
