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

from ...domain.enums import ServiceStartType, ServiceState
from ...domain.errors import NativeCallError
from ...domain.records import ServiceInfo
from . import systemd_dbus as _dbus

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.1  # seconds between ActiveState polls while a start/stop/restart settles

#: systemd ActiveState -> canonical ServiceState (Windows has no failed/reloading, mapped to nearest).
_ACTIVE_STATE = {
    "active": ServiceState.RUNNING,
    "inactive": ServiceState.STOPPED,
    "failed": ServiceState.STOPPED,
    "activating": ServiceState.START_PENDING,
    "deactivating": ServiceState.STOP_PENDING,
    "reloading": ServiceState.CONTINUE_PENDING,
}
#: systemd UnitFileState -> ServiceStartType (static/indirect/... have no exact Windows twin -> Manual).
_UNIT_FILE_STATE = {
    "enabled": ServiceStartType.AUTOMATIC,
    "enabled-runtime": ServiceStartType.AUTOMATIC,
    "disabled": ServiceStartType.DISABLED,
    "masked": ServiceStartType.DISABLED,
    "masked-runtime": ServiceStartType.DISABLED,
    "static": ServiceStartType.MANUAL,
    "indirect": ServiceStartType.MANUAL,
    "generated": ServiceStartType.MANUAL,
    "transient": ServiceStartType.MANUAL,
    "linked": ServiceStartType.MANUAL,
    "linked-runtime": ServiceStartType.MANUAL,
}


def to_service_state(active_state: str) -> ServiceState:
    """Map a systemd ``ActiveState`` to the canonical :class:`ServiceState`.

    Example:
        >>> to_service_state("active").value, to_service_state("inactive").value
        ('Running', 'Stopped')
    """
    return _ACTIVE_STATE.get(active_state, ServiceState.STOPPED)


def to_start_type(unit_file_state: str) -> ServiceStartType | None:
    """Map a systemd ``UnitFileState`` to a :class:`ServiceStartType` (``None`` if unknown).

    Example:
        >>> to_start_type("enabled").value, to_start_type("disabled").value
        ('Automatic', 'Disabled')
    """
    return _UNIT_FILE_STATE.get(unit_file_state)


def _start_type_of(bus: _dbus.Bus, name: str) -> ServiceStartType | None:
    """GetUnitFileState for one unit; ``None`` for a transient unit that has no unit file."""
    state = _dbus.unit_file_state(bus, name)
    return None if state is None else to_start_type(state)


def _service_at(bus: _dbus.Bus, name: str, path: str | None, *, start_type: ServiceStartType | None) -> ServiceInfo:
    """The record for service ``name`` read from its object ``path`` (None: unreadable, fields default)."""
    return ServiceInfo(
        name=name,
        display_name=str(_dbus.path_property(bus, path, "Description", "")),
        status=to_service_state(str(_dbus.path_property(bus, path, "ActiveState", "inactive"))),
        start_type=start_type,
        can_stop=True,
        can_pause_continue=False,
    )


def _service_files(bus: _dbus.Bus) -> list[tuple[str, str]]:
    """Every ``.service`` unit file as (unit name, UnitFileState); [] when systemd lists none.

    Fail-soft: when no unit-file listing answers, the listing keeps the loaded services rather
    than failing.
    """
    try:
        return _dbus.unit_files(bus, ".service")
    except NativeCallError as exc:
        logger.debug("cannot list the service unit files, listing loaded services only: %s", exc)
        return []


def iter_services() -> Iterator[ServiceInfo]:
    """Yield a :class:`ServiceInfo` per systemd ``.service`` unit (like Get-Service, Linux).

    The loaded services come first, from ``ListUnits``, which already carries their description
    and state. Then the service unit FILES not among them - a stopped, disabled service is
    unloaded and ``ListUnits`` omits it, while Get-Service lists a stopped service - each read
    through ``LoadUnit``, with its start type taken from the file listing. Skipped as no service:
    a ListUnits entry whose LoadState is not-found or masked, and a masked file, an alias, a
    template or a file ``LoadUnit`` reads as not-found. Lazy: ``GetUnitFileState``, the unit-file
    listing and each ``LoadUnit`` run only for consumed services, so
    ``get_service().where(...).take(5)`` over loaded services costs one ``ListUnits`` plus five
    state lookups. Reading an unloaded service loads it transiently; systemd unloads it again when
    nothing references it.

    Example:
        >>> callable(iter_services)
        True
    """
    bus = _dbus.open_bus()
    try:
        (units,) = _dbus.call(bus, "ListUnits")
        seen: set[str] = set()
        for unit in units:
            name = str(unit[0])
            if not name.endswith(".service"):
                continue
            seen.add(name)
            if str(unit[2]) in _dbus.UNLISTED_LOAD_STATES:  # ListUnits field 2 is the LoadState
                continue
            # ListUnits fields: 1 Description, 3 ActiveState.
            yield ServiceInfo(
                name=name,
                display_name=str(unit[1]),
                status=to_service_state(str(unit[3])),
                start_type=_start_type_of(bus, name),
                can_stop=True,
                can_pause_continue=False,
            )
        for name, state in _service_files(bus):
            if name in seen or state in _dbus.UNLISTED_FILE_STATES:
                continue
            seen.add(name)
            path = _dbus.load_unit_path(bus, name)
            if path is None:  # a stale or broken file: systemd has no unit behind the name
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
    verbs need admin. Each verb waits for the unit to settle, then returns its :class:`ServiceInfo`.

    Example:
        >>> callable(SystemdServiceController().start)
        True
    """

    def start(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        """Start a unit; wait until active/failed; return its state (like Start-Service)."""
        return self._transition(name, "StartUnit", {"active", "failed"}, timeout)

    def stop(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        """Stop a unit; wait until inactive/failed; return its state (like Stop-Service)."""
        return self._transition(name, "StopUnit", {"inactive", "failed"}, timeout)

    def restart(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        """Restart a unit; wait until active/failed; return its state (like Restart-Service)."""
        return self._transition(name, "RestartUnit", {"active", "failed"}, timeout)

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

    def _transition(self, name: str, method: str, settled: set[str], timeout: float) -> ServiceInfo:
        bus = _dbus.open_bus()
        try:
            _dbus.call(bus, method, "ss", (name, "replace"))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if _dbus.active_state(bus, name) in settled:
                    break
                time.sleep(_POLL_INTERVAL)
            return _get_one(bus, name)
        finally:
            bus.conn.close()


__all__ = ["SystemdServiceController", "iter_services", "to_service_state", "to_start_type"]
