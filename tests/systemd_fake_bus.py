"""A fake systemd system bus for the D-Bus adapters (services and timers), modelled on systemd 259.

``open_bus`` is the adapters' connect seam onto the system D-Bus, the external edge; :func:`use_bus`
replaces it with a :class:`FakeConn` that answers the Manager calls and property reads the
adapters send. Its fidelity is the point: loaded versus unloaded units, ``LoadUnit`` of a name
with no unit file, and the unit-file listings behave the way busctl shows them on a real host.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from typing import Any, ClassVar, NamedTuple

import pytest

from pwshpy.adapters.native import systemd_dbus


class Address(NamedTuple):
    path: str


class Message(NamedTuple):
    path: str
    method: str
    body: tuple[Any, ...]


class Reply:
    def __init__(self, body: tuple[Any, ...], *, error: bool = False) -> None:
        self.body = body
        self.header = type("H", (), {"message_type": type("T", (), {"name": "error" if error else "method_return"})})


class FakeCore:
    """The two jeepney entry points the adapter uses: DBusAddress and new_method_call."""

    @staticmethod
    def DBusAddress(path: str, *, bus_name: str, interface: str) -> Address:  # noqa: N802 - jeepney API name
        return Address(path)

    @staticmethod
    def new_method_call(address: Address, method: str, signature: str = "", body: tuple[Any, ...] = ()) -> Message:
        return Message(address.path, method, body)


class FakeConn:
    """A system bus holding units: (unit, interface, property) -> value; unknown -> D-Bus error.

    ``unit_files`` are the units with a unit file (GetUnitFileState answers ``enabled`` for them
    only, unless ``file_states`` names another state; ListUnitFiles[ByPatterns] lists them); the
    Manager verbs a controller sends (Reload, StopUnit, ...) are recorded and succeed, except
    those listed in ``failing`` (which also fails the unit-file listings).

    A unit holding properties is LOADED unless it is in ``unloaded``: like systemd, GetUnit and
    property reads fail for an unloaded unit until ``LoadUnit`` loads it from its file. A
    registered-but-not-started boot timer and a disabled, stopped timer are unloaded on a real
    host, so modelling every unit as loaded hides read-backs that only GetUnit would miss.

    ``LoadUnit`` of a name the bus knows nothing about SUCCEEDS, as on systemd 259 (measured with
    busctl): it returns an object whose ``LoadState`` is ``not-found`` and whose properties read
    systemd's defaults (``User`` ``""``, ``ExecMainStartTimestamp`` 0, ...). ``load_states``
    overrides a known unit's ``LoadState`` (``loaded`` by default), in ListUnits field 2 too;
    ``file_dirs`` puts a unit file somewhere other than ``/etc/systemd/system`` (a vendor unit).
    A ListUnits row carries LoadState and ActiveState independently, as on a real host: a unit whose
    file was deleted under it, or masked without ``--now``, reads ``not-found``/``masked`` while
    still ``active``.

    ``appeared`` names units systemd cached as ``not-found`` whose unit file was written later,
    without a daemon-reload: ListUnits still reports the cached ``not-found``, but ``LoadUnit``
    loads them from the new file (systemd's unit-cache retry, ``manager_unit_cache_should_retry_load``,
    which applies to a not-found unit only, never to a masked one).

    ``pending`` names units whose unit file a controller is about to write: systemd knows nothing of
    them until the controller's own daemon-reload (the first ``Reload`` verb), so before it
    ``LoadUnit`` answers ``not-found`` and ``GetUnitFileState`` fails, as for any unknown name; from
    it on they behave as configured.

    ``refused_loads`` names units whose ``LoadUnit`` call itself fails with a D-Bus error (an
    invalid name, a refused or broken bus call) - unlike a not-found unit, which systemd answers.
    ``refused_reads`` names (unit, property) pairs whose ``Properties.Get`` fails with a D-Bus
    error although the unit is loaded (a refused or broken property read).

    ``aliases`` maps an alias name to its unit: ``LoadUnit`` of the alias returns the unit's own
    object, whose ``Id`` is the unit's name, as on systemd 259 (measured: ``LoadUnit("sshd.service")``
    answers the ``ssh.service`` object, ``Id`` ``ssh.service``). Every unit object reads its own
    name as ``Id``, a not-found one too. ``reads`` records each property read as (unit, property).

    ``Unit.Job`` is ``(0, "/")`` (no job, as busctl and jeepney show it) unless the unit's
    ActiveState is a transition (activating, deactivating, reloading), which holds a job path, as a
    start or stop job does on systemd; a ``Job`` entry in ``props`` overrides that. ``walks`` maps a
    verb (``StartUnit``, ...) to the ActiveStates the unit named in that verb passes through after
    it: each ActiveState or Job read returns the current step and moves to the next, the last step
    sticking. Like systemd, which installs the job before it answers the verb, the unit holds a job
    until it reaches that last step - so a restart reads active WITH a job right after the verb,
    and a Type=oneshot start walks activating -> inactive, its job ending there.
    """

    _VERBS = frozenset({"Reload", "StartUnit", "StopUnit", "RestartUnit", "EnableUnitFiles", "DisableUnitFiles"})
    _TRANSITIONS = frozenset({"activating", "deactivating", "reloading"})

    def __init__(
        self,
        units: list[tuple[str, str, str]],
        props: dict[tuple[str, str, str], Any],
        unit_files: frozenset[str] = frozenset(),
        *,
        file_states: dict[str, str] | None = None,
        failing: frozenset[str] = frozenset(),
        unloaded: frozenset[str] = frozenset(),
        load_states: dict[str, str] | None = None,
        file_dirs: dict[str, str] | None = None,
        appeared: frozenset[str] = frozenset(),
        walks: dict[str, list[str]] | None = None,
        refused_loads: frozenset[str] = frozenset(),
        refused_reads: frozenset[tuple[str, str]] = frozenset(),
        pending: frozenset[str] = frozenset(),
        aliases: dict[str, str] | None = None,
    ) -> None:
        self._units = units  # (name, description, active_state)
        self._props = props
        self._unit_files = unit_files
        self._file_states = file_states or {}
        self._failing = failing
        self._unloaded = set(unloaded)  # shrinks as LoadUnit loads them
        self._load_states = dict(load_states or {})  # a copy: LoadUnit of an appeared unit updates it
        self._appeared = appeared
        self._file_dirs = file_dirs or {}
        self._walks = walks or {}
        self._refused_loads = refused_loads
        self._refused_reads = refused_reads
        self._walking: dict[str, list[str]] = {}  # unit -> ActiveStates still ahead of it
        self._known = {unit for unit, _iface, _prop in props} | set(unit_files) | set(unloaded)
        self._not_found: set[str] = set()  # names LoadUnit answered with a not-found object
        self._pending = set(pending)  # unknown to systemd until the first Reload
        self._aliases = aliases or {}
        self.verbs: list[str] = []
        self.reads: list[tuple[str, str]] = []  # every property read as (unit, property), in order
        self.calls: list[tuple[str, tuple[Any, ...]]] = []  # each verb with its body (the unit names sent)
        self.methods: list[str] = []  # every method sent, in order (to count round trips)
        self.closed = False

    def _loaded(self) -> set[str]:
        return {unit for unit, _iface, _prop in self._props} - self._unloaded

    def _list_units(self, msg: Message) -> Reply:
        rows = [(n, d, self._load_states.get(n, "loaded"), a, "", "", f"/u/{n}", 0, "", "/") for n, d, a in self._units]
        return Reply((rows,))

    def _list_unit_files(self, msg: Message) -> Reply:
        if msg.method in self._failing:  # e.g. a systemd before 230 has no ListUnitFilesByPatterns
            return Reply(("unknown method",), error=True)
        patterns = list(msg.body[1]) if msg.body else []
        files = [n for n in sorted(self._unit_files) if not patterns or any(fnmatch.fnmatch(n, p) for p in patterns)]
        rows = [
            (f"{self._file_dirs.get(n, '/etc/systemd/system')}/{n}", self._file_states.get(n, "enabled")) for n in files
        ]
        return Reply((rows,))

    def _get_unit(self, msg: Message) -> Reply:
        (name,) = msg.body
        return Reply((f"/u/{name}",)) if name in self._loaded() else Reply(("not loaded",), error=True)

    def _load_unit(self, msg: Message) -> Reply:
        (name,) = msg.body
        if name in self._refused_loads:
            return Reply(("org.freedesktop.DBus.Error.AccessDenied",), error=True)
        if name in self._aliases:  # systemd resolves an alias to the unit it names
            name = self._aliases[name]
        self._unloaded.discard(name)  # loaded from its unit file
        if name in self._appeared and self._load_states.get(name) == "not-found":
            self._load_states[name] = "loaded"  # the unit-cache retry finds the file written since
        if name not in self._known or name in self._pending:
            self._not_found.add(name)  # systemd still answers: a "not-found" unit object
        return Reply((f"/u/{name}",))

    def _get_unit_file_state(self, msg: Message) -> Reply:
        (name,) = msg.body
        state = self._file_states.get(name, "enabled")
        known = name in self._unit_files and name not in self._pending
        return Reply((state,)) if known else Reply(("no unit file",), error=True)

    def _verb(self, msg: Message) -> Reply:
        self.verbs.append(msg.method)
        self.calls.append((msg.method, tuple(msg.body)))
        if msg.method == "Reload":  # the controller's daemon-reload: the files it wrote now load
            self._not_found -= self._pending
            self._pending.clear()
        if msg.method in self._failing:
            return Reply(("failed",), error=True)
        if msg.method in self._walks:
            self._walking[str(msg.body[0])] = list(self._walks[msg.method])
        return Reply(())

    def _step(self, name: str) -> tuple[str, bool] | None:
        """The walking unit's current ActiveState and whether its job is still on, moving one step on."""
        steps = self._walking.get(name)
        if not steps:
            return None
        current, job_on = steps[0], len(steps) > 1
        if job_on:
            steps.pop(0)
        return current, job_on

    def steps_ahead(self, name: str) -> list[str]:
        """The ActiveStates the walking unit has still to pass (its last, sticking step included)."""
        return list(self._walking.get(name, []))

    def _job(self, active_state: str) -> tuple[int, str]:
        return (7, "/org/freedesktop/systemd1/job/7") if active_state in self._TRANSITIONS else (0, "/")

    def _transient(self, name: str, iface: str, prop: str) -> Reply | None:
        """ActiveState and the derived Job of a loaded unit; None for any other property."""
        if prop not in {"ActiveState", "Job"} or (prop == "Job" and (name, iface, prop) in self._props):
            return None
        step = self._step(name)
        state = step[0] if step else self._props.get((name, iface, "ActiveState"))
        if state is None:
            return None
        job = self._job("activating" if step and step[1] else state)
        return Reply((("t", state if prop == "ActiveState" else job),))

    #: What systemd 259 reports for a not-found unit object (busctl get-property, measured).
    _NOT_FOUND_DEFAULTS: ClassVar[dict[str, Any]] = {
        "LoadState": "not-found",
        "ActiveState": "inactive",
        "User": "",
        "ExecMainStartTimestamp": 0,
        "ExecMainCode": 0,
        "ExecMainStatus": 0,
        "Job": (0, "/"),
    }

    def _get(self, msg: Message) -> Reply:
        name, _iface, prop = (msg.path[len("/u/") :], *msg.body)
        if name in self._not_found:
            value = self._NOT_FOUND_DEFAULTS.get(prop, name if prop in {"Description", "Id"} else None)
            return Reply(("no prop",), error=True) if value is None else Reply((("t", value),))
        if name in self._unloaded:  # an unloaded unit has no readable properties
            return Reply(("not loaded",), error=True)
        transient = self._transient(name, _iface, prop)
        if transient is not None:
            return transient
        key = (name, _iface, prop)
        if key in self._props:
            return Reply((("t", self._props[key]),))
        derived = self._derived(name, prop)
        return Reply(("no prop",), error=True) if derived is None else Reply((("t", derived),))

    def _derived(self, name: str, prop: str) -> Any:
        """The LoadState and Id systemd reports for a known unit with no ``props`` entry; None otherwise."""
        if name not in self._known:
            return None
        if prop == "LoadState":
            return self._load_states.get(name, "loaded")
        return name if prop == "Id" else None

    def send_and_get_reply(self, msg: Message) -> Reply:
        self.methods.append(msg.method)
        if msg.method == "Get":
            self.reads.append((msg.path[len("/u/") :], str(msg.body[1])))
        handlers: dict[str, Callable[[Message], Reply]] = {
            "ListUnits": self._list_units,
            "ListUnitFilesByPatterns": self._list_unit_files,
            "ListUnitFiles": self._list_unit_files,
            "GetUnit": self._get_unit,
            "LoadUnit": self._load_unit,
            "GetUnitFileState": self._get_unit_file_state,
            "Get": self._get,
            **dict.fromkeys(self._VERBS, self._verb),
        }
        if msg.method == "Get" and (msg.path[len("/u/") :], msg.body[1]) in self._refused_reads:
            return Reply(("org.freedesktop.DBus.Error.AccessDenied",), error=True)
        handler = handlers.get(msg.method)
        return Reply(("unexpected",), error=True) if handler is None else handler(msg)

    def close(self) -> None:
        self.closed = True


def use_bus(monkeypatch: pytest.MonkeyPatch, conn: FakeConn) -> None:
    # open_bus is the adapter's connect seam onto the system D-Bus (the external edge).
    bus = systemd_dbus.Bus(conn=conn, core=FakeCore(), mgr=Address("/org/freedesktop/systemd1"))
    monkeypatch.setattr(systemd_dbus, "open_bus", lambda: bus)


__all__ = ["Address", "FakeConn", "FakeCore", "Message", "Reply", "use_bus"]
