"""Shared systemd D-Bus plumbing (jeepney) for the services and timers adapters.

This is the native interface ``systemctl`` itself speaks - talk to the systemd Manager
over the system bus, never shell out and scrape text. ``jeepney`` (pure-Python D-Bus, a
base Linux dependency) is loaded lazily through ``importlib``: importing this module is
safe on Windows/macOS; calling it without jeepney raises :class:`PlatformUnsupportedError`.

Contents:
    * :class:`Bus` - a live system-bus connection plus the jeepney handles.
    * :func:`open_bus` - open the system bus (the caller closes it).
    * :func:`call` - one Manager method call, mapping a D-Bus error to :class:`NativeCallError`.
    * :func:`unit_property` / :func:`active_state` - read a ``Unit`` (or, with ``interface``, a
      ``Timer`` / ``Service``) property of a loaded unit.
    * :func:`has_pending_job` - whether a unit still has a job queued or running.
    * :func:`unit_path` / :func:`path_property` - resolve a unit's object path once, then read
      several properties from it (one ``GetUnit`` instead of one per property).
    * :func:`load_unit_path` - the same, but ``LoadUnit`` loads an unloaded unit from its file
      (None for a name with no unit file, which systemd answers as a ``not-found`` object).
    * :func:`unit_files` - every unit file of one type with its UnitFileState: the units
      ``ListUnits`` omits because nothing has loaded them (a stopped, disabled service or timer).
    * :data:`UNLISTED_LOAD_STATES` / :data:`UNLISTED_FILE_STATES` - the states that are no unit
      of their own, so a listing skips them.
    * :func:`is_unlisted_unit` - the one skip rule for a ``ListUnits`` entry: not-found or masked,
      and not running.
"""

from __future__ import annotations

import importlib
from typing import Any, NamedTuple, cast

from ...domain.errors import NativeCallError, PlatformUnsupportedError

MANAGER_PATH = "/org/freedesktop/systemd1"
MANAGER_BUS = "org.freedesktop.systemd1"
MANAGER_IFACE = "org.freedesktop.systemd1.Manager"
UNIT_IFACE = "org.freedesktop.systemd1.Unit"
TIMER_IFACE = "org.freedesktop.systemd1.Timer"  # LastTriggerUSec, NextElapseUSecRealtime, Unit
SERVICE_IFACE = "org.freedesktop.systemd1.Service"  # ExecMainCode, ExecMainStatus, ExecMainStartTimestamp
_PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

#: ListUnits LoadStates that are no unit to list while stopped: not-found is a name some unit
#: references with no unit file behind it; masked is made unstartable on purpose. A running unit in
#: either state is still listed (:func:`is_unlisted_unit`).
UNLISTED_LOAD_STATES = frozenset({"not-found", "masked"})

#: ActiveStates of a unit that runs or is changing state. A unit whose file was deleted under it, or
#: that was masked without ``--now``, keeps running in one of these; Get-Service keeps listing such a
#: service (marked for deletion, or Disabled/Running), so the listings keep it too.
RUNNING_ACTIVE_STATES = frozenset({"active", "activating", "deactivating", "reloading"})

#: UnitFileStates of a unit file that is no unit of its own: masked is made unstartable on purpose;
#: alias is a second name for a unit listed under its real name.
UNLISTED_FILE_STATES = frozenset({"masked", "masked-runtime", "alias"})


def is_unlisted_unit(load_state: str, active_state: str) -> bool:
    """Whether a ``ListUnits`` entry is no unit to list: not-found or masked, and not running.

    Args:
        load_state: The entry's ``LoadState`` (``ListUnits`` field 2).
        active_state: The entry's ``ActiveState`` (``ListUnits`` field 3); systemd reports the two
            independently, so a not-found or masked unit can still be ``active``.

    Returns:
        True for a stopped (inactive or failed) not-found or masked unit, else False.

    Example:
        >>> is_unlisted_unit("not-found", "inactive"), is_unlisted_unit("masked", "active")
        (True, False)
    """
    return load_state in UNLISTED_LOAD_STATES and active_state not in RUNNING_ACTIVE_STATES


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


def unit_path(bus: Bus, name: str) -> str | None:
    """The object path of a loaded unit, or None when it is not loaded."""
    try:
        (path,) = call(bus, "GetUnit", "s", (name,))
    except NativeCallError:
        return None  # a stopped/absent unit unloads -> GetUnit reports "not loaded"
    return str(path)


def load_unit_path(bus: Bus, name: str) -> str | None:
    """The object path of a unit, loading it from its unit file if needed; None when it has none.

    systemd answers ``LoadUnit`` for a valid name with NO unit file too (measured on 259): the
    object's ``LoadState`` is ``not-found`` and every property reads a default - ``User`` ``""``,
    which would read as root. So one ``LoadState`` read turns that into None, letting callers apply
    their "unknown" defaults; an invalid name is a D-Bus error, also None.
    """
    try:
        (path,) = call(bus, "LoadUnit", "s", (name,))
    except NativeCallError:
        return None  # an invalid unit name: nothing to read
    resolved = str(path)
    if path_property(bus, resolved, "LoadState", "not-found") == "not-found":
        return None
    return resolved


def unit_files(bus: Bus, suffix: str) -> list[tuple[str, str]]:
    """Every unit file named ``*<suffix>`` as (unit name, UnitFileState), templates excluded.

    ``ListUnitFilesByPatterns`` lets systemd filter (systemd >= 230); an older manager lacks it, so
    the full ``ListUnitFiles`` is filtered here instead. A template (``name@.service``) is no unit
    until instantiated: ``LoadUnit`` cannot load it. Raises :class:`NativeCallError` when neither
    listing answers; a caller that lists loaded units first can fall back to those alone.
    """
    try:
        (files,) = call(bus, "ListUnitFilesByPatterns", "asas", ([], [f"*{suffix}"]))
    except NativeCallError:
        (files,) = call(bus, "ListUnitFiles")
    named = ((str(path).rsplit("/", 1)[-1], str(state)) for path, state in files)
    return [(name, state) for name, state in named if name.endswith(suffix) and not name.endswith(f"@{suffix}")]


def unit_property(bus: Bus, name: str, prop: str, default: Any, *, interface: str = UNIT_IFACE) -> Any:
    """Read one property of a loaded unit; ``default`` if the unit is not loaded or lacks it.

    ``interface`` picks the property's D-Bus interface: the generic ``Unit`` one by default, or
    the type-specific :data:`TIMER_IFACE` / :data:`SERVICE_IFACE` for a timer's or service's own
    properties (systemd exposes those only under the type's interface). To read several
    properties of one unit, resolve :func:`unit_path` once and use :func:`path_property`.
    """
    return path_property(bus, unit_path(bus, name), prop, default, interface=interface)


def path_property(bus: Bus, path: str | None, prop: str, default: Any, *, interface: str = UNIT_IFACE) -> Any:
    """Read one property from a unit object ``path``; ``default`` for a None path or a missing property."""
    if path is None:
        return default
    props = bus.core.DBusAddress(path, bus_name=MANAGER_BUS, interface=_PROPERTIES_IFACE)
    reply = bus.conn.send_and_get_reply(bus.core.new_method_call(props, "Get", "ss", (interface, prop)))
    if is_error(reply):
        return default
    value: Any = reply.body[0]
    if isinstance(value, tuple):  # a D-Bus variant arrives as (signature, value); take the value
        value = cast("Any", value[-1])
    return value


def active_state(bus: Bus, name: str) -> str:
    """The unit's ActiveState, or ``"inactive"`` when it is not loaded."""
    return str(unit_property(bus, name, "ActiveState", "inactive"))


def has_pending_job(bus: Bus, name: str) -> bool:
    """Whether the unit has a job queued or running (``Unit.Job`` is ``(0, "/")`` when it has none).

    The job, not the ActiveState, says whether a start/stop is still on its way: a Type=oneshot
    unit's start job ends with the unit inactive, and right after RestartUnit an active unit still
    reads active. An unloaded unit has no job.
    """
    job: Any = path_property(bus, unit_path(bus, name), "Job", (0, "/"))
    if not isinstance(job, tuple) or not job:
        return False
    return int(cast("Any", job[0])) != 0


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
    "RUNNING_ACTIVE_STATES",
    "SERVICE_IFACE",
    "TIMER_IFACE",
    "UNIT_IFACE",
    "UNLISTED_FILE_STATES",
    "UNLISTED_LOAD_STATES",
    "active_state",
    "call",
    "has_pending_job",
    "is_error",
    "is_unlisted_unit",
    "load_unit_path",
    "open_bus",
    "path_property",
    "unit_file_state",
    "unit_files",
    "unit_path",
    "unit_property",
]
