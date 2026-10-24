"""Shared systemd D-Bus plumbing (jeepney) for the services and timers adapters.

This is the native interface ``systemctl`` itself speaks - talk to the systemd Manager
over the system bus, never shell out and scrape text. ``jeepney`` (pure-Python D-Bus, a
base Linux dependency) is loaded lazily through ``importlib``: importing this module is
safe on Windows/macOS; calling it without jeepney raises :class:`PlatformUnsupportedError`.

Contents:
    * :class:`LoadState` / :class:`ActiveState` / :class:`UnitFileState` - the systemd unit states,
      parsed once where they are read off the bus; a value systemd adds later reads as ``UNKNOWN``.
    * :class:`Bus` - a live system-bus connection plus the jeepney handles.
    * :func:`open_bus` - open the system bus (the caller closes it).
    * :func:`call` - one Manager method call, mapping a D-Bus error to :class:`NativeCallError`.
    * :func:`unit_property` / :func:`active_state` - read a ``Unit`` (or, with ``interface``, a
      ``Timer`` / ``Service``) property of a loaded unit.
    * :func:`path_active_state` - the ``ActiveState`` of a unit object already resolved.
    * :func:`has_pending_job` - whether a unit still has a job queued or running.
    * :func:`unit_path` / :func:`path_property` - resolve a unit's object path once, then read
      several properties from it (one ``GetUnit`` instead of one per property).
    * :func:`load_unit_path` - the same, but ``LoadUnit`` loads an unloaded unit from its file
      (None for a name with no unit file, which systemd answers as a ``not-found`` object).
    * :func:`load_unit` - :func:`load_unit_path` that raises when the ``LoadUnit`` call or the
      ``LoadState`` read fails, for a caller that must tell "no such unit" from "could not ask".
    * :func:`load_unit_state` - :func:`load_unit` that also returns the ``LoadState`` it read
      (:class:`LoadedUnit`).
    * :func:`unit_files` - every unit file of one type with its UnitFileState (:class:`UnitFile`): the units
      ``ListUnits`` omits because nothing has loaded them (a stopped, disabled service or timer).
    * :data:`UNLISTED_LOAD_STATES` / :data:`UNLISTED_FILE_STATES` - the states that are no unit
      of their own, so a listing skips them.
    * :func:`is_unlisted_unit` - the one skip rule for a ``ListUnits`` entry: not-found or masked,
      and not running.
"""

from __future__ import annotations

import importlib
from enum import Enum
from typing import Any, NamedTuple, cast

from ...domain.errors import NativeCallError, PlatformUnsupportedError

MANAGER_PATH = "/org/freedesktop/systemd1"
MANAGER_BUS = "org.freedesktop.systemd1"
MANAGER_IFACE = "org.freedesktop.systemd1.Manager"
UNIT_IFACE = "org.freedesktop.systemd1.Unit"
TIMER_IFACE = "org.freedesktop.systemd1.Timer"  # LastTriggerUSec, NextElapseUSecRealtime, Unit
SERVICE_IFACE = "org.freedesktop.systemd1.Service"  # ExecMainCode, ExecMainStatus, ExecMainStartTimestamp
_PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"


class LoadState(str, Enum):
    """A unit's ``LoadState`` (org.freedesktop.systemd1(5)); a value not named here is :attr:`UNKNOWN`.

    systemd grows its state lists between releases, so an unnamed value must not fail a listing:
    it reads as UNKNOWN, which no rule here matches - a unit in it is listed and loaded, as before
    these states were typed.

    Example:
        >>> LoadState("not-found") is LoadState.NOT_FOUND, LoadState("from-the-future") is LoadState.UNKNOWN
        (True, True)
    """

    STUB = "stub"
    LOADED = "loaded"
    NOT_FOUND = "not-found"
    BAD_SETTING = "bad-setting"
    ERROR = "error"
    MERGED = "merged"
    MASKED = "masked"
    UNKNOWN = "unknown"  # not a systemd value: the fallback for one this list does not name

    @classmethod
    def _missing_(cls, value: object) -> LoadState:
        return cls.UNKNOWN


class ActiveState(str, Enum):
    """A unit's ``ActiveState`` (org.freedesktop.systemd1(5)); a value not named here is :attr:`UNKNOWN`.

    ``maintenance`` and ``refreshing`` are named but, like UNKNOWN, fall to every mapping's default:
    no rule or table here lists them, so they read as they did while the states were plain text.

    Example:
        >>> ActiveState("failed") is ActiveState.FAILED, ActiveState("from-the-future") is ActiveState.UNKNOWN
        (True, True)
    """

    ACTIVE = "active"
    RELOADING = "reloading"
    INACTIVE = "inactive"
    FAILED = "failed"
    ACTIVATING = "activating"
    DEACTIVATING = "deactivating"
    MAINTENANCE = "maintenance"
    REFRESHING = "refreshing"
    UNKNOWN = "unknown"  # not a systemd value: the fallback for one this list does not name

    @classmethod
    def _missing_(cls, value: object) -> ActiveState:
        return cls.UNKNOWN


class UnitFileState(str, Enum):
    """A unit file's ``UnitFileState`` (org.freedesktop.systemd1(5)); a value not named here is :attr:`UNKNOWN`.

    :attr:`EMPTY` is systemd's ``""`` (no unit file state to report), which callers also use for a
    ``GetUnitFileState`` that did not answer. UNKNOWN matches no rule or table, as an unnamed text
    value did before these states were typed.

    Example:
        >>> UnitFileState("enabled-runtime") is UnitFileState.ENABLED_RUNTIME, UnitFileState("") is UnitFileState.EMPTY
        (True, True)
        >>> UnitFileState("from-the-future") is UnitFileState.UNKNOWN
        True
    """

    ENABLED = "enabled"
    ENABLED_RUNTIME = "enabled-runtime"
    LINKED = "linked"
    LINKED_RUNTIME = "linked-runtime"
    ALIAS = "alias"
    MASKED = "masked"
    MASKED_RUNTIME = "masked-runtime"
    STATIC = "static"
    INDIRECT = "indirect"
    DISABLED = "disabled"
    GENERATED = "generated"
    TRANSIENT = "transient"
    BAD = "bad"
    EMPTY = ""
    UNKNOWN = "unknown"  # not a systemd value: the fallback for one this list does not name

    @classmethod
    def _missing_(cls, value: object) -> UnitFileState:
        return cls.UNKNOWN


#: ListUnits LoadStates that are no unit to list while stopped: not-found is a name some unit
#: references with no unit file behind it; masked is made unstartable on purpose. A running unit in
#: either state is still listed (:func:`is_unlisted_unit`).
UNLISTED_LOAD_STATES = frozenset({LoadState.NOT_FOUND, LoadState.MASKED})

#: ActiveStates of a unit that runs or is changing state. A unit whose file was deleted under it, or
#: that was masked without ``--now``, keeps running in one of these; Get-Service keeps listing such a
#: service (marked for deletion, or Disabled/Running), so the listings keep it too.
RUNNING_ACTIVE_STATES = frozenset(
    {ActiveState.ACTIVE, ActiveState.ACTIVATING, ActiveState.DEACTIVATING, ActiveState.RELOADING}
)

#: UnitFileStates of a unit file that is no unit of its own: masked is made unstartable on purpose;
#: alias is a second name for a unit listed under its real name.
UNLISTED_FILE_STATES = frozenset({UnitFileState.MASKED, UnitFileState.MASKED_RUNTIME, UnitFileState.ALIAS})


def is_unlisted_unit(load_state: LoadState, active_state: ActiveState) -> bool:
    """Whether a ``ListUnits`` entry is no unit to list: not-found or masked, and not running.

    Args:
        load_state: The entry's ``LoadState`` (``ListUnits`` field 2).
        active_state: The entry's ``ActiveState`` (``ListUnits`` field 3); systemd reports the two
            independently, so a not-found or masked unit can still be ``active``.

    Returns:
        True for a stopped (inactive or failed) not-found or masked unit, else False.

    Example:
        >>> is_unlisted_unit(LoadState.NOT_FOUND, ActiveState.INACTIVE)
        True
        >>> is_unlisted_unit(LoadState.MASKED, ActiveState.ACTIVE)
        False
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
    """The object path of a loaded unit, or None when it is not loaded.

    Args:
        bus: The open system-bus connection.
        name: The unit name, e.g. ``"sshd.service"``.

    Returns:
        The unit's D-Bus object path, or None when systemd has not loaded it (a stopped
        unit is unloaded, so ``GetUnit`` answers "not loaded" for it).

    Example:
        >>> callable(unit_path)
        True
    """
    try:
        (path,) = call(bus, "GetUnit", "s", (name,))
    except NativeCallError:
        return None  # a stopped/absent unit unloads -> GetUnit reports "not loaded"
    return str(path)


class LoadedUnit(NamedTuple):
    """A unit :func:`load_unit_state` loaded: its object path and the ``LoadState`` read with it."""

    path: str
    load_state: LoadState


def load_unit_state(bus: Bus, name: str) -> LoadedUnit | None:
    """The object path and ``LoadState`` of a unit, loaded from its file if needed; None if it has none.

    systemd answers ``LoadUnit`` for a valid name with NO unit file too (measured on 259): the
    object's ``LoadState`` is ``not-found`` and every property reads a default - ``User`` ``""``,
    which would read as root. So one ``LoadState`` read turns that into None, letting callers apply
    their "unknown" defaults. The state comes back with the path so a caller that also judges
    masked units needs no second read, which could fail where the first succeeded.

    For an alias name ``LoadUnit`` returns the object of the unit the alias names (measured on 259:
    ``sshd.service`` answers the ``ssh.service`` object); that object's ``Id`` is the real name.

    Raises:
        NativeCallError: When the ``LoadUnit`` call itself or the ``LoadState`` read fails (an
            invalid name, a refused or broken bus call): systemd gave no answer, which is not
            the same as "no such unit".
    """
    (path,) = call(bus, "LoadUnit", "s", (name,))
    resolved = str(path)
    # None, not "not-found", as the default: a failed read must not pass for systemd's answer.
    load_state = path_property(bus, resolved, "LoadState", None)
    if load_state is None:
        raise NativeCallError(f"cannot read the LoadState of '{name}'")
    state = LoadState(str(load_state))
    if state is LoadState.NOT_FOUND:
        return None
    return LoadedUnit(resolved, state)


def load_unit(bus: Bus, name: str) -> str | None:
    """The object path of :func:`load_unit_state`; None when the unit has no unit file.

    Raises:
        NativeCallError: When the ``LoadUnit`` call itself or the ``LoadState`` read fails.
    """
    loaded = load_unit_state(bus, name)
    return None if loaded is None else loaded.path


def load_unit_path(bus: Bus, name: str) -> str | None:
    """:func:`load_unit`, but a failed ``LoadUnit`` call or ``LoadState`` read is also None (fail-soft).

    For field read-backs and the controllers, whose callers apply their "unknown" defaults to None.
    """
    try:
        return load_unit(bus, name)
    except NativeCallError:
        return None  # an invalid unit name or an unreadable unit: nothing to read


class UnitFile(NamedTuple):
    """One entry of a unit-file listing: the unit name and its file's ``UnitFileState``."""

    name: str
    state: UnitFileState


def unit_files(bus: Bus, suffix: str) -> list[UnitFile]:
    """Every unit file named ``*<suffix>`` as a :class:`UnitFile`, templates excluded.

    ``ListUnitFilesByPatterns`` lets systemd filter (systemd >= 230); an older manager lacks it, so
    the full ``ListUnitFiles`` is filtered here instead. A template (``name@.service``) is no unit
    until instantiated: ``LoadUnit`` cannot load it. Raises :class:`NativeCallError` when neither
    listing answers; a caller that lists loaded units first can fall back to those alone.
    """
    try:
        (files,) = call(bus, "ListUnitFilesByPatterns", "asas", ([], [f"*{suffix}"]))
    except NativeCallError:
        (files,) = call(bus, "ListUnitFiles")
    named = ((str(path).rsplit("/", 1)[-1], state) for path, state in files)
    return [
        UnitFile(name, UnitFileState(str(state)))
        for name, state in named
        if name.endswith(suffix) and not name.endswith(f"@{suffix}")
    ]


def unit_property(bus: Bus, name: str, prop: str, default: Any, *, interface: str = UNIT_IFACE) -> Any:
    """Read one property of a loaded unit; ``default`` if the unit is not loaded or lacks it.

    ``interface`` picks the property's D-Bus interface: the generic ``Unit`` one by default, or
    the type-specific :data:`TIMER_IFACE` / :data:`SERVICE_IFACE` for a timer's or service's own
    properties (systemd exposes those only under the type's interface). To read several
    properties of one unit, resolve :func:`unit_path` once and use :func:`path_property`.
    """
    return path_property(bus, unit_path(bus, name), prop, default, interface=interface)


def path_property(bus: Bus, path: str | None, prop: str, default: Any, *, interface: str = UNIT_IFACE) -> Any:
    """Read one property from a unit object ``path``; ``default`` for a None path or a missing property.

    Args:
        bus: The open system-bus connection.
        path: The unit's object path (from :func:`unit_path` / :func:`load_unit`), or None.
        prop: The property name, e.g. ``"ActiveState"``.
        default: Returned for a None ``path`` or when systemd answers the read with an error.
        interface: The D-Bus interface that holds ``prop`` (the unit interface by default).

    Returns:
        The property's value, unwrapped from its D-Bus variant, or ``default``.

    Example:
        >>> path_property(None, None, "ActiveState", "unknown")
        'unknown'
    """
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


def path_active_state(bus: Bus, path: str | None) -> ActiveState:
    """The ActiveState of the unit object ``path``; INACTIVE for a None path or an unanswered read.

    Example:
        >>> path_active_state(None, None) is ActiveState.INACTIVE
        True
    """
    return ActiveState(str(path_property(bus, path, "ActiveState", ActiveState.INACTIVE.value)))


def active_state(bus: Bus, name: str) -> ActiveState:
    """The unit's ActiveState, or INACTIVE when it is not loaded."""
    return path_active_state(bus, unit_path(bus, name))


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


def unit_file_state(bus: Bus, name: str) -> UnitFileState | None:
    """GetUnitFileState for one unit; ``None`` for a transient unit that has no unit file."""
    try:
        (state,) = call(bus, "GetUnitFileState", "s", (name,))
    except NativeCallError:
        return None
    return UnitFileState(str(state))


__all__ = [
    "ActiveState",
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
    "LoadState",
    "LoadedUnit",
    "load_unit",
    "load_unit_path",
    "load_unit_state",
    "open_bus",
    "path_active_state",
    "path_property",
    "UnitFile",
    "UnitFileState",
    "unit_file_state",
    "unit_files",
    "unit_path",
    "unit_property",
]
