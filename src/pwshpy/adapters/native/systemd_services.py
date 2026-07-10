"""Native services on Linux via systemd over D-Bus (jeepney).

The portable counterpart to the win32service adapter: reads the systemd unit list through
``org.freedesktop.systemd1.Manager`` (``ListUnits`` + per-unit ``GetUnitFileState``) - the
native interface ``systemctl`` itself uses, so no subprocess and no text scraping. Each
``.service`` unit marshals into the same :class:`ServiceInfo`.

The shared D-Bus plumbing (open the bus, call the Manager, read a Unit property) lives in
:mod:`.systemd_dbus`; this module adds the service-specific mapping and the mutating controller.

Contents:
    * :func:`to_service_state` - systemd ActiveState -> :class:`ServiceState` (pure).
    * :func:`to_start_type` - systemd UnitFileState -> :class:`ServiceStartType` (pure).
    * :func:`iter_services` - stream the ``.service`` units as records.
    * :class:`SystemdServiceController` - start/stop/restart/enable/disable (**mutating**).
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from ...domain.enums import ServiceStartType, ServiceState
from ...domain.errors import NativeCallError
from ...domain.records import ServiceInfo
from . import systemd_dbus as _dbus

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


def iter_services() -> Iterator[ServiceInfo]:
    """Yield a :class:`ServiceInfo` per systemd ``.service`` unit (like Get-Service, Linux).

    Lazy: ``GetUnitFileState`` is only called for the units actually consumed, so
    ``get_service().where(...).take(5)`` costs one ``ListUnits`` plus five state lookups.

    Example:
        >>> callable(iter_services)
        True
    """
    bus = _dbus.open_bus()
    try:
        (units,) = _dbus.call(bus, "ListUnits")
        for unit in units:
            name = str(unit[0])
            if not name.endswith(".service"):
                continue
            yield ServiceInfo(
                name=name,
                display_name=str(unit[1]),
                status=to_service_state(str(unit[3])),
                start_type=_start_type_of(bus, name),
                can_stop=True,
                can_pause_continue=False,
            )
    finally:
        bus.conn.close()


def _get_one(bus: _dbus.Bus, name: str) -> ServiceInfo:
    """Build a :class:`ServiceInfo` for a single unit (used to report post-mutation state)."""
    return ServiceInfo(
        name=name,
        display_name=str(_dbus.unit_property(bus, name, "Description", "")),
        status=to_service_state(_dbus.active_state(bus, name)),
        start_type=_start_type_of(bus, name),
        can_stop=True,
        can_pause_continue=False,
    )


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
