"""Shared systemd D-Bus plumbing (jeepney) for the services and timers adapters.

This is the native interface ``systemctl`` itself speaks - talk to the systemd Manager
over the system bus, never shell out and scrape text. ``jeepney`` (pure-Python D-Bus, a
base Linux dependency) is loaded lazily through ``importlib``: importing this module is
safe on Windows/macOS; calling it without jeepney raises :class:`PlatformUnsupportedError`.

Contents:
    * :class:`Bus` - a live system-bus connection plus the jeepney handles.
    * :func:`open_bus` - open the system bus (the caller closes it).
    * :func:`call` - one Manager method call, mapping a D-Bus error to :class:`NativeCallError`.
    * :func:`unit_property` / :func:`active_state` - read a ``Unit`` property of a loaded unit.
"""

from __future__ import annotations

import importlib
from typing import Any, NamedTuple, cast

from ...domain.errors import NativeCallError, PlatformUnsupportedError

MANAGER_PATH = "/org/freedesktop/systemd1"
MANAGER_BUS = "org.freedesktop.systemd1"
MANAGER_IFACE = "org.freedesktop.systemd1.Manager"
UNIT_IFACE = "org.freedesktop.systemd1.Unit"
_PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"


class Bus(NamedTuple):
    """A live system-bus connection plus the jeepney handles needed to call the Manager."""

    conn: Any
    core: Any
    mgr: Any


def _load_dbus() -> tuple[Any, Any]:
    """Import jeepney (core + blocking io); raise a clear error without it (only an odd Linux install lacks it)."""
    try:
        core = importlib.import_module("jeepney")
        blocking = importlib.import_module("jeepney.io.blocking")
    except ImportError as exc:  # pragma: no cover - jeepney is a base Linux dep
        raise PlatformUnsupportedError(
            "systemd needs Windows (win32), or Linux with jeepney (a base Linux dependency; reinstall pwshpy)."
        ) from exc
    return core, blocking


def open_bus() -> Bus:
    """Open a system-bus connection (the caller closes ``bus.conn``); jeepney is loaded lazily."""
    core, blocking = _load_dbus()
    try:
        conn = blocking.open_dbus_connection(bus="SYSTEM")
    except OSError as exc:
        raise NativeCallError(f"cannot connect to the system D-Bus: {exc}") from exc
    mgr = core.DBusAddress(MANAGER_PATH, bus_name=MANAGER_BUS, interface=MANAGER_IFACE)
    return Bus(conn=conn, core=core, mgr=mgr)


def is_error(reply: Any) -> bool:
    """Whether a D-Bus reply is an error (jeepney names the type ``error``, lowercased)."""
    return str(reply.header.message_type.name).lower() == "error"


def call(bus: Bus, method: str, signature: str = "", body: tuple[Any, ...] = ()) -> Any:
    """Send one Manager method call and return its body, mapping a D-Bus error to NativeCallError."""
    reply = bus.conn.send_and_get_reply(bus.core.new_method_call(bus.mgr, method, signature, body))
    if is_error(reply):
        raise NativeCallError(f"systemd {method} failed: {reply.body}")
    return reply.body


def unit_property(bus: Bus, name: str, prop: str, default: Any) -> Any:
    """Read one ``org.freedesktop.systemd1.Unit`` property; ``default`` if the unit is not loaded."""
    try:
        (path,) = call(bus, "GetUnit", "s", (name,))
    except NativeCallError:
        return default  # a stopped/absent unit unloads -> GetUnit reports "not loaded"
    props = bus.core.DBusAddress(path, bus_name=MANAGER_BUS, interface=_PROPERTIES_IFACE)
    reply = bus.conn.send_and_get_reply(bus.core.new_method_call(props, "Get", "ss", (UNIT_IFACE, prop)))
    if is_error(reply):
        return default
    value: Any = reply.body[0]
    if isinstance(value, tuple):  # a D-Bus variant arrives as (signature, value); take the value
        value = cast("Any", value[-1])
    return value


def active_state(bus: Bus, name: str) -> str:
    """The unit's ActiveState, or ``"inactive"`` when it is not loaded."""
    return str(unit_property(bus, name, "ActiveState", "inactive"))


def unit_file_state(bus: Bus, name: str) -> str | None:
    """GetUnitFileState for one unit; ``None`` for a transient unit that has no unit file."""
    try:
        (state,) = call(bus, "GetUnitFileState", "s", (name,))
    except NativeCallError:
        return None
    return str(state)


__all__ = [
    "Bus",
    "MANAGER_BUS",
    "UNIT_IFACE",
    "active_state",
    "call",
    "is_error",
    "open_bus",
    "unit_file_state",
    "unit_property",
]
