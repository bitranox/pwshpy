"""Native scheduled tasks via systemd timers: pure mapping (os_agnostic) + a live read (Linux).

The ActiveState/UnitFileState mappings and the unit-name/unit-text helpers are pure, so they
run on every OS. The live D-Bus read needs Linux + jeepney; it self-skips otherwise.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from pwshpy.adapters.native import systemd_dbus, systemd_timers
from pwshpy.adapters.native.systemd_timers import (
    SystemdScheduledTaskController,
    arms_on_register,
    base_name,
    iter_timers,
    linux_user,
    service_last_result,
    service_task_state,
    service_unit,
    service_user_id,
    starts_when_armed,
    timer_enabled,
    timer_task_state,
    timer_to_task_state,
    timer_unit,
    usec_to_datetime,
)
from pwshpy.domain.enums import TaskLogonType, TaskRunLevel, TaskState
from pwshpy.domain.errors import ConfigurationError, NativeCallError, PlatformUnsupportedError
from pwshpy.domain.records import ScheduledTaskInfo
from pwshpy.domain.scheduling import (
    BootTrigger,
    DailyTrigger,
    LogonTrigger,
    OnceTrigger,
    TaskPrincipal,
    TaskTrigger,
)

_HAS_JEEPNEY = importlib.util.find_spec("jeepney") is not None


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("active_state", "state"),
    [
        ("active", TaskState.READY),
        ("activating", TaskState.QUEUED),
        ("inactive", TaskState.DISABLED),
        ("deactivating", TaskState.DISABLED),
        ("failed", TaskState.DISABLED),
        ("nonsense", TaskState.UNKNOWN),
    ],
)
def test_timer_state_maps(active_state: str, state: TaskState) -> None:
    """A systemd timer ActiveState maps to a TaskState (unknown -> Unknown, not a wrong guess)."""
    assert timer_to_task_state(active_state) is state


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("unit_file_state", "enabled"),
    [("enabled", True), ("enabled-runtime", True), ("disabled", False), ("static", False), ("masked", False)],
)
def test_timer_enabled_maps(unit_file_state: str, enabled: bool) -> None:
    """timer_enabled is True only for the enabled UnitFileStates."""
    assert timer_enabled(unit_file_state) is enabled


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("active_state", "enabled", "state"),
    [
        ("inactive", True, TaskState.READY),  # enabled but not started: armed for the next boot
        ("inactive", False, TaskState.DISABLED),
        ("active", True, TaskState.READY),
        ("failed", True, TaskState.DISABLED),  # every other mapping is timer_to_task_state's
        ("deactivating", True, TaskState.DISABLED),
        ("activating", False, TaskState.QUEUED),
    ],
)
def test_timer_task_state(active_state: str, enabled: bool, state: TaskState) -> None:
    """An enabled, inactive timer is Ready (it arms at the next boot); the rest maps by ActiveState."""
    assert timer_task_state(active_state, enabled=enabled) is state


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("bases", "starts"),
    [
        ([], True),
        (["OnUnitActiveUSec", "OnActiveUSec"], True),  # relative to the timer/service, not to boot
        (["OnBootUSec"], False),
        (["OnUnitActiveUSec", "OnStartupUSec"], False),
    ],
)
def test_starts_when_armed(bases: list[str], starts: bool) -> None:
    """A timer with a boot-relative monotonic timer must not be started: it would elapse at once."""
    assert starts_when_armed(bases) is starts


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("active_state", "state"),
    [
        ("inactive", TaskState.READY),
        ("failed", TaskState.READY),
        ("active", TaskState.RUNNING),
        ("activating", TaskState.RUNNING),
    ],
)
def test_service_task_state(active_state: str, state: TaskState) -> None:
    """An on-demand service task is RUNNING while executing, else READY (it can always be run)."""
    assert service_task_state(active_state) is state


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("task_path", "base"),
    [
        ("pwshpy-backup", "pwshpy-backup"),
        ("pwshpy-backup.timer", "pwshpy-backup"),
        ("pwshpy-backup.service", "pwshpy-backup"),
        ("\\Folder\\pwshpy-backup", "pwshpy-backup"),
        ("/etc/systemd/system/pwshpy-backup.timer", "pwshpy-backup"),
    ],
)
def test_base_name(task_path: str, base: str) -> None:
    """base_name reduces any task identifier to the systemd unit base name."""
    assert base_name(task_path) == base


@pytest.mark.os_agnostic
def test_service_unit_text() -> None:
    """service_unit renders a oneshot exec unit; arguments are appended to ExecStart."""
    text = service_unit("/usr/bin/backup", "--full /data", "nightly backup")
    assert "Type=oneshot" in text
    assert "ExecStart=/usr/bin/backup --full /data" in text
    assert "Description=nightly backup" in text


@pytest.mark.os_agnostic
def test_service_unit_no_arguments() -> None:
    """With no arguments, ExecStart is just the program (no trailing space)."""
    assert "ExecStart=/bin/true\n" in service_unit("/bin/true", "", "")


_AT = datetime(2026, 10, 21, 21, 59, tzinfo=timezone.utc)


@pytest.mark.os_agnostic
def test_service_unit_runs_as_the_given_uid() -> None:
    text = service_unit("/bin/true", "", "d", user="1000")
    assert "\nUser=1000\n" in text


@pytest.mark.os_agnostic
def test_service_unit_without_a_user_has_no_user_line() -> None:
    """No user: no User= line at all, so the system manager runs the unit as root."""
    assert "User=" not in service_unit("/bin/true", "", "d")


@pytest.mark.os_agnostic
def test_timer_unit_renders_each_supported_trigger_in_utc() -> None:
    once_local = OnceTrigger(at=datetime(2026, 10, 21, 23, 59, tzinfo=timezone(timedelta(hours=2))))
    text = timer_unit("job", "d", [once_local, DailyTrigger(at=_AT), BootTrigger(delay=timedelta(seconds=90))])
    assert "OnCalendar=2026-10-21 21:59:00 UTC\n" in text
    assert "OnCalendar=*-*-* 21:59:00 UTC\n" in text
    assert "OnBootSec=90s\n" in text
    assert "Unit=job.service\n" in text
    assert "WantedBy=timers.target\n" in text


@pytest.mark.os_agnostic
def test_timer_unit_fires_on_time() -> None:
    """AccuracySec=1s: systemd's default 1 min window would let a calendar trigger fire late."""
    assert "\nAccuracySec=1s\n" in timer_unit("job", "d", [OnceTrigger(at=_AT)])


@pytest.mark.os_agnostic
def test_unit_text_escapes_percent_specifiers() -> None:
    """A literal % reaches the program and the description, never a systemd specifier."""
    service = service_unit("/usr/bin/date", "+%Y-%m-%d", "100% done")
    assert "ExecStart=/usr/bin/date +%%Y-%%m-%%d\n" in service
    assert "Description=100%% done\n" in service
    assert "Description=50%% off\n" in timer_unit("job", "50% off", [OnceTrigger(at=_AT)])


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("program", "arguments", "description"),
    [
        ("/bin/true", "x\nExecStartPre=/bin/evil", ""),
        ("/bin/true\nUser=0", "", ""),
        ("/bin/true", "", "d\n[Service]\nUser=0"),
        ("/bin/true", "x\ry", ""),
    ],
)
def test_service_unit_refuses_a_line_break(program: str, arguments: str, description: str) -> None:
    """A line break would inject unit-file directives, so it is refused, not escaped."""
    with pytest.raises(ConfigurationError):
        service_unit(program, arguments, description)


@pytest.mark.os_agnostic
def test_timer_unit_refuses_a_line_break_in_the_description() -> None:
    with pytest.raises(ConfigurationError):
        timer_unit("job", "d\nOnBootSec=0", [OnceTrigger(at=_AT)])


@pytest.mark.os_agnostic
@pytest.mark.parametrize("trigger", [DailyTrigger(at=_AT, every_days=2), LogonTrigger()])
def test_timer_unit_refuses_what_systemd_cannot_express(trigger: TaskTrigger) -> None:
    with pytest.raises(PlatformUnsupportedError):
        timer_unit("job", "d", [trigger])


@pytest.mark.os_agnostic
def test_timer_unit_refuses_a_logon_trigger_naming_why() -> None:
    """The refusal names the reason: a user unit fires at the user manager's start, not each logon."""
    with pytest.raises(PlatformUnsupportedError, match="first session"):
        timer_unit("job", "d", [LogonTrigger(user_id="1000")])


@pytest.mark.os_agnostic
def test_linux_user_mapping() -> None:
    assert linux_user(TaskPrincipal()) is None
    assert linux_user(TaskPrincipal.system()) is None  # SYSTEM is root: a system unit with no User=
    assert linux_user(TaskPrincipal(user_id="1000")) == "1000"
    with pytest.raises(PlatformUnsupportedError):
        linux_user(TaskPrincipal(user_id="S-1-5-21-1-2-3-1001"))
    with pytest.raises(PlatformUnsupportedError):
        linux_user(TaskPrincipal(user_id="1000", run_level=TaskRunLevel.HIGHEST))
    with pytest.raises(PlatformUnsupportedError):
        linux_user(TaskPrincipal(logon_type=TaskLogonType.S4U))


@pytest.mark.os_agnostic
def test_linux_user_refuses_the_other_service_sids() -> None:
    """Only SYSTEM maps to root; LOCAL SERVICE / NETWORK SERVICE have no Linux account to map to."""
    with pytest.raises(PlatformUnsupportedError):
        linux_user(TaskPrincipal(user_id="S-1-5-19"))


@pytest.mark.os_agnostic
def test_linux_user_allows_highest_for_root() -> None:
    """Root already holds every privilege, so HIGHEST is satisfiable for uid 0."""
    assert linux_user(TaskPrincipal(user_id="0", run_level=TaskRunLevel.HIGHEST)) == "0"


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("triggers", "arms"),
    [
        ([OnceTrigger(at=_AT)], True),
        ([DailyTrigger(at=_AT), OnceTrigger(at=_AT)], True),
        ([BootTrigger()], False),  # started now, an elapsed OnBootSec= would fire at once
        ([BootTrigger(), BootTrigger(delay=timedelta(minutes=5))], False),
    ],
)
def test_arms_on_register(triggers: list[TaskTrigger], arms: bool) -> None:
    """A calendar timer is started at once; a boot timer only waits for the next boot."""
    assert arms_on_register(triggers) is arms


@pytest.mark.os_agnostic
def test_arms_on_register_refuses_boot_mixed_with_calendar() -> None:
    """Boot and calendar triggers cannot share one timer: starting it would fire the boot one now."""
    with pytest.raises(PlatformUnsupportedError, match="boot"):
        arms_on_register([BootTrigger(), OnceTrigger(at=_AT)])


def _no_account(name: str) -> int | None:
    return None


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, ""),  # service not loaded / property unreadable: unknown, never a guess
        ("", "0"),  # no User=: the system manager runs the service as root
        ("1000", "1000"),
        ("0", "0"),
        ("01", ""),  # not a canonical uid and not an account name either
        ("S-1-5-18", ""),
    ],
)
def test_service_user_id_without_accounts(configured: str | None, expected: str) -> None:
    """User= read back as a decimal uid; an unresolvable value is "" (the task is still listed)."""
    assert service_user_id(configured, resolve=_no_account) == expected


@pytest.mark.os_agnostic
def test_service_user_id_resolves_an_account_name() -> None:
    """A User= account name resolves to its uid through the injected account lookup."""
    accounts = {"alice": 1001}
    assert service_user_id("alice", resolve=accounts.get) == "1001"
    assert service_user_id("ghost", resolve=accounts.get) == ""


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs the Linux account database")
def test_service_user_id_resolves_root_by_name() -> None:
    """The default lookup reads the real account database: root is uid 0 on every Linux."""
    assert service_user_id("root") == "0"


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("usec", "expected"),
    [
        (0, None),  # never triggered / no realtime elapse
        (2**64 - 1, None),  # UINT64_MAX: systemd "infinity"
        (1_700_000_000_000_000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)),
        (1_700_000_000_000_001, datetime(2023, 11, 14, 22, 13, 20, 1, tzinfo=timezone.utc)),  # exact to the microsecond
    ],
)
def test_usec_to_datetime(usec: int, expected: datetime | None) -> None:
    """A systemd USec timestamp maps to aware UTC exactly; 0 and infinity map to None."""
    assert usec_to_datetime(usec) == expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("exec_code", "exec_status", "started_usec", "expected"),
    [
        (0, 0, 0, None),  # never started: no result, not "0 = success"
        (0, 0, 1_700_000_000_000_000, None),  # started, still running: no exit recorded yet
        (1, 0, 1_700_000_000_000_000, 0),  # CLD_EXITED, status 0
        (1, 3, 1_700_000_000_000_000, 3),  # CLD_EXITED, status 3
        (2, 9, 1_700_000_000_000_000, 137),  # CLD_KILLED by SIGKILL -> 128 + signal (shell convention)
    ],
)
def test_service_last_result(exec_code: int, exec_status: int, started_usec: int, expected: int | None) -> None:
    """The service's last exit status, None until it has run once."""
    assert service_last_result(exec_code=exec_code, exec_status=exec_status, started_usec=started_usec) == expected


# --- Fake system bus: pins which D-Bus interface/property each run-info field reads -------------

_TIMER = systemd_dbus.TIMER_IFACE
_SERVICE = systemd_dbus.SERVICE_IFACE
_T0 = 1_700_000_000_000_000  # 2023-11-14 22:13:20 UTC in microseconds
_T1 = _T0 + 3_600_000_000  # one hour later


class _Address(NamedTuple):
    path: str


class _Message(NamedTuple):
    path: str
    method: str
    body: tuple[Any, ...]


class _Reply:
    def __init__(self, body: tuple[Any, ...], *, error: bool = False) -> None:
        self.body = body
        self.header = type("H", (), {"message_type": type("T", (), {"name": "error" if error else "method_return"})})


class _FakeCore:
    """The two jeepney entry points the adapter uses: DBusAddress and new_method_call."""

    @staticmethod
    def DBusAddress(path: str, *, bus_name: str, interface: str) -> _Address:  # noqa: N802 - jeepney API name
        return _Address(path)

    @staticmethod
    def new_method_call(address: _Address, method: str, signature: str = "", body: tuple[Any, ...] = ()) -> _Message:
        return _Message(address.path, method, body)


class _FakeConn:
    """A system bus holding loaded units: (unit, interface, property) -> value; unknown -> D-Bus error.

    ``unit_files`` are the units with a unit file (GetUnitFileState answers ``enabled`` for them
    only, unless ``file_states`` names another state); the Manager verbs a controller sends
    (Reload, StopUnit, ...) are recorded and succeed, except those listed in ``failing``.
    ``LoadUnit`` answers for a loaded unit or one with a unit file, like systemd loading it.
    """

    _VERBS = frozenset({"Reload", "StartUnit", "StopUnit", "EnableUnitFiles", "DisableUnitFiles"})

    def __init__(
        self,
        units: list[tuple[str, str, str]],
        props: dict[tuple[str, str, str], Any],
        unit_files: frozenset[str] = frozenset(),
        *,
        file_states: dict[str, str] | None = None,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self._units = units  # (name, description, active_state)
        self._props = props
        self._unit_files = unit_files
        self._file_states = file_states or {}
        self._failing = failing
        self.verbs: list[str] = []
        self.methods: list[str] = []  # every method sent, in order (to count round trips)
        self.closed = False

    def send_and_get_reply(self, msg: _Message) -> _Reply:
        self.methods.append(msg.method)
        loaded = {unit for unit, _iface, _prop in self._props}
        if msg.method == "ListUnits":
            return _Reply(([(n, d, "loaded", a, "", "", f"/u/{n}", 0, "", "/") for n, d, a in self._units],))
        if msg.method in ("GetUnit", "LoadUnit"):
            (name,) = msg.body
            # GetUnit answers only for a loaded unit; LoadUnit also loads one from its unit file.
            known = name in loaded or (msg.method == "LoadUnit" and name in self._unit_files)
            return _Reply((f"/u/{name}",)) if known else _Reply(("not loaded",), error=True)
        if msg.method == "GetUnitFileState":
            (name,) = msg.body
            state = self._file_states.get(name, "enabled")
            return _Reply((state,)) if name in self._unit_files else _Reply(("no unit file",), error=True)
        if msg.method in self._VERBS:
            self.verbs.append(msg.method)
            return _Reply(("failed",), error=True) if msg.method in self._failing else _Reply(())
        if msg.method == "Get":
            key = (msg.path[len("/u/") :], *msg.body)
            return _Reply((("t", self._props[key]),)) if key in self._props else _Reply(("no prop",), error=True)
        return _Reply(("unexpected",), error=True)

    def close(self) -> None:
        self.closed = True


def _use_bus(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> None:
    # open_bus is the adapter's connect seam onto the system D-Bus (the external edge).
    bus = systemd_dbus.Bus(conn=conn, core=_FakeCore(), mgr=_Address("/org/freedesktop/systemd1"))
    monkeypatch.setattr(systemd_dbus, "open_bus", lambda: bus)


def _service_props(unit: str, *, code: int, status: int, started: int) -> dict[tuple[str, str, str], Any]:
    return {
        (unit, _SERVICE, "ExecMainCode"): code,
        (unit, _SERVICE, "ExecMainStatus"): status,
        (unit, _SERVICE, "ExecMainStartTimestamp"): started,
    }


@pytest.mark.os_agnostic
def test_iter_timers_reads_run_info_from_the_timer_and_its_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Listing: last/next run come from the Timer interface, the result from the service its Unit= names."""
    props: dict[tuple[str, str, str], Any] = {
        ("backup.timer", _TIMER, "LastTriggerUSec"): _T0,
        ("backup.timer", _TIMER, "NextElapseUSecRealtime"): _T1,
        ("backup.timer", _TIMER, "Unit"): "backup-job.service",  # Unit= differs from the timer's base name
        **_service_props("backup-job.service", code=1, status=3, started=_T0),
        ("backup-job.service", _SERVICE, "User"): "",  # no User=: root
        ("never.timer", _TIMER, "LastTriggerUSec"): 0,
        ("never.timer", _TIMER, "NextElapseUSecRealtime"): 2**64 - 1,
        ("never.timer", _TIMER, "Unit"): "never.service",
    }
    units = [("backup.timer", "Backup", "active"), ("x.service", "", "active"), ("never.timer", "", "active")]
    conn = _FakeConn(units, props, frozenset({"backup.timer", "never.timer"}))
    _use_bus(monkeypatch, conn)
    backup, never = list(iter_timers())
    assert backup.task_name == "backup"
    assert backup.last_run_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert backup.next_run_time == datetime(2023, 11, 14, 23, 13, 20, tzinfo=timezone.utc)
    assert backup.last_task_result == 3
    assert (never.last_run_time, never.last_task_result, never.next_run_time) == (None, None, None)
    assert (backup.user_id, backup.run_level) == ("0", None)
    assert never.user_id == ""  # its service is not loaded: unknown, and the timer is still listed
    assert conn.closed is True
    # The timer's own path comes from ListUnits; only the triggered service needs one GetUnit.
    assert conn.methods.count("GetUnit") == 2


@pytest.mark.os_agnostic
def test_controller_info_timer_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Controller snapshot of a timer task carries the same run info as the listing."""
    props: dict[tuple[str, str, str], Any] = {
        ("job.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "active",
        ("job.timer", _TIMER, "LastTriggerUSec"): _T0,
        ("job.timer", _TIMER, "NextElapseUSecRealtime"): _T1,
        ("job.timer", _TIMER, "Unit"): "job.service",
        **_service_props("job.service", code=2, status=9, started=_T0),
        ("job.service", _SERVICE, "User"): "1000",
    }
    conn = _FakeConn([], props, frozenset({"job.timer"}))
    _use_bus(monkeypatch, conn)
    info = SystemdScheduledTaskController().disable("job")
    assert conn.verbs == ["StopUnit", "DisableUnitFiles", "Reload"]
    assert conn.methods.count("GetUnit") == 2  # one for the timer, one for its service
    assert info.state is TaskState.READY
    assert info.last_run_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert info.next_run_time == datetime(2023, 11, 14, 23, 13, 20, tzinfo=timezone.utc)
    assert info.last_task_result == 137  # killed by SIGKILL
    assert (info.user_id, info.run_level) == ("1000", None)


@pytest.mark.os_agnostic
def test_controller_info_on_demand_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An on-demand service: last run is its own last start, the result its exit, nothing scheduled."""
    props = {**_service_props("adhoc.service", code=1, status=0, started=_T0), ("adhoc.service", _SERVICE, "User"): ""}
    conn = _FakeConn([], props)
    _use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)  # filesystem edge: never write /etc here
    info = SystemdScheduledTaskController().register("adhoc", program="/bin/true")
    assert (tmp_path / "adhoc.service").is_file()
    assert conn.methods.count("GetUnit") == 1  # the service, once, for every field
    assert info.last_run_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert info.last_task_result == 0  # ran and succeeded, never folded into None
    assert info.next_run_time is None
    assert info.user_id == "0"  # an empty User= is root


def _job_props(base: str, *, user: str) -> dict[tuple[str, str, str], Any]:
    """A loaded timer + service pair, as systemd reports it right after register arms it."""
    return {
        (f"{base}.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "active",
        (f"{base}.timer", _TIMER, "LastTriggerUSec"): 0,
        (f"{base}.timer", _TIMER, "NextElapseUSecRealtime"): _T1,
        (f"{base}.timer", _TIMER, "Unit"): f"{base}.service",
        **_service_props(f"{base}.service", code=0, status=0, started=0),
        (f"{base}.service", _SERVICE, "User"): user,
    }


@pytest.mark.os_agnostic
def test_register_with_a_calendar_trigger_writes_and_arms_a_timer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A uid principal lands as User=, the trigger as a .timer that is enabled AND started now."""
    conn = _FakeConn([], _job_props("job", user="1000"), frozenset({"job.timer"}))
    _use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)  # filesystem edge: never write /etc here
    info = SystemdScheduledTaskController().register(
        "job", program="/bin/true", principal=TaskPrincipal(user_id="1000"), triggers=[DailyTrigger(at=_AT)]
    )
    assert "\nUser=1000\n" in (tmp_path / "job.service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 21:59:00 UTC\n" in (tmp_path / "job.timer").read_text(encoding="utf-8")
    assert conn.verbs == ["Reload", "EnableUnitFiles", "StartUnit"]
    assert (info.user_id, info.enabled, info.state) == ("1000", True, TaskState.READY)
    assert info.next_run_time == datetime(2023, 11, 14, 23, 13, 20, tzinfo=timezone.utc)
    assert conn.methods.count("GetUnit") == 2  # one for the timer, one for its service
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_register_with_a_boot_trigger_enables_but_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A boot timer is only enabled: starting it now would fire an already-elapsed OnBootSec= at once."""
    props = {**_job_props("boot", user=""), ("boot.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "inactive"}
    conn = _FakeConn([], props, frozenset({"boot.timer"}))
    _use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    info = SystemdScheduledTaskController().register(
        "boot", program="/bin/true", triggers=[BootTrigger(delay=timedelta(seconds=600))]
    )
    assert "OnBootSec=600s\n" in (tmp_path / "boot.timer").read_text(encoding="utf-8")
    assert "User=" not in (tmp_path / "boot.service").read_text(encoding="utf-8")
    assert conn.verbs == ["Reload", "EnableUnitFiles"]
    assert info.user_id == "0"
    assert (info.state, info.enabled) == (TaskState.READY, True)  # armed for the next boot, not "Disabled"


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("principal", "triggers"),
    [
        (TaskPrincipal(user_id="S-1-5-21-1-2-3-1001"), []),
        (None, [LogonTrigger()]),
        (None, [DailyTrigger(at=_AT, every_days=7)]),
        (None, [BootTrigger(), OnceTrigger(at=_AT)]),
    ],
)
def test_register_refuses_before_writing_or_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, principal: TaskPrincipal | None, triggers: list[TaskTrigger]
) -> None:
    """Every refusal happens before a unit file is written or the bus is opened."""
    conn = _FakeConn([], {})
    _use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(PlatformUnsupportedError):
        SystemdScheduledTaskController().register("job", program="/bin/true", principal=principal, triggers=triggers)
    assert list(tmp_path.iterdir()) == []
    assert conn.methods == []


@pytest.mark.os_agnostic
def test_register_refuses_a_line_break_before_writing_or_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    conn = _FakeConn([], {})
    _use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(ConfigurationError):
        SystemdScheduledTaskController().register("job", program="/bin/true", description="a\nb")
    assert list(tmp_path.iterdir()) == []
    assert conn.methods == []


@pytest.mark.os_agnostic
def test_register_without_triggers_retires_a_previous_timer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Re-registering as on demand replaces the task (like CREATE_OR_UPDATE): the old .timer goes."""
    (tmp_path / "job.timer").write_text("[Timer]\nOnBootSec=1s\n", encoding="utf-8")
    props = {**_service_props("job.service", code=0, status=0, started=0), ("job.service", _SERVICE, "User"): ""}
    conn = _FakeConn([], props)
    _use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    info = SystemdScheduledTaskController().register("job", program="/bin/true")
    assert not (tmp_path / "job.timer").exists()
    assert (tmp_path / "job.service").is_file()
    assert conn.verbs == ["StopUnit", "DisableUnitFiles", "Reload"]
    assert info.next_run_time is None  # on demand again


@pytest.mark.os_agnostic
def test_register_with_triggers_stops_the_previous_timer_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A re-registered timer is stopped before its file is rewritten, so the old schedule cannot fire."""
    (tmp_path / "job.timer").write_text("[Timer]\nOnCalendar=daily\n", encoding="utf-8")
    conn = _FakeConn([], _job_props("job", user=""), frozenset({"job.timer"}))
    _use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    SystemdScheduledTaskController().register("job", program="/bin/true", triggers=[OnceTrigger(at=_AT)])
    assert "OnCalendar=2026-10-21 21:59:00 UTC\n" in (tmp_path / "job.timer").read_text(encoding="utf-8")
    assert conn.verbs == ["StopUnit", "Reload", "EnableUnitFiles", "StartUnit"]


@pytest.mark.os_agnostic
def test_unregister_disables_the_timer_before_deleting_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """unregister drops the timers.target.wants link too, so no dangling enablement is left behind."""
    for unit in ("job.timer", "job.service"):
        (tmp_path / unit).write_text("", encoding="utf-8")
    conn = _FakeConn([], {})
    _use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    SystemdScheduledTaskController().unregister("job")
    assert list(tmp_path.iterdir()) == []
    assert conn.verbs == ["StopUnit", "DisableUnitFiles", "StopUnit", "Reload"]


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux") or not _HAS_JEEPNEY, reason="needs Linux + jeepney")
def test_live_read_yields_timers() -> None:
    """A live D-Bus read yields typed timer records (ListUnits is unprivileged; a box always has timers)."""
    tasks = list(iter_timers())
    assert tasks  # every systemd box ships timers (e.g. systemd-tmpfiles-clean, logrotate, apt-daily)
    assert all(isinstance(t, ScheduledTaskInfo) for t in tasks)
    assert all(not t.task_name.endswith(".timer") for t in tasks)  # base name, suffix stripped
    assert all(t.task_path == "/" for t in tasks)


@pytest.mark.local_only
@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux") or not _HAS_JEEPNEY, reason="needs Linux + jeepney")
def test_live_read_reports_run_info() -> None:
    """Liveness for the run-info property names: a real box has a calendar timer with a next elapse.

    A misspelt property or interface reads the "none" default and would leave every timer at None,
    so requiring at least one real next_run_time is what proves the names are right.
    """
    tasks = list(iter_timers())
    assert any(t.next_run_time is not None for t in tasks)
    assert all(t.next_run_time is None or t.next_run_time.tzinfo is timezone.utc for t in tasks)
    assert all(t.last_run_time is None or t.last_run_time.tzinfo is timezone.utc for t in tasks)


@pytest.mark.os_agnostic
def test_unregister_never_disables_a_timer_it_did_not_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A same-named vendor timer (no file under /etc) is not disabled: pwshpy owns only its own units."""
    conn = _FakeConn([], {})
    _use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    SystemdScheduledTaskController().unregister("apt-daily")
    assert "DisableUnitFiles" not in conn.verbs


@pytest.mark.os_agnostic
def test_unregister_keeps_the_timer_file_when_disabling_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A real DisableUnitFiles failure raises before the file goes, so the wants link never dangles."""
    (tmp_path / "job.timer").write_text("", encoding="utf-8")
    conn = _FakeConn([], {}, failing=frozenset({"DisableUnitFiles"}))
    _use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(NativeCallError, match="DisableUnitFiles"):
        SystemdScheduledTaskController().unregister("job")
    assert (tmp_path / "job.timer").exists()
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_unregister_wraps_a_failed_delete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A unit file that cannot be deleted is a NativeCallError, not a raw OSError."""
    (tmp_path / "job.timer").mkdir()  # unlink() on a directory raises IsADirectoryError
    conn = _FakeConn([], {})
    _use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(NativeCallError, match="cannot delete unit"):
        SystemdScheduledTaskController().unregister("job")


def _enable_props(base: str, monotonic: list[tuple[str, int, int]] | None) -> dict[tuple[str, str, str], Any]:
    """An inactive, enabled timer task; ``monotonic`` is TimersMonotonic (None: unreadable)."""
    props = {**_job_props(base, user=""), (f"{base}.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "inactive"}
    if monotonic is not None:
        props[(f"{base}.timer", _TIMER, "TimersMonotonic")] = monotonic
    return props


@pytest.mark.os_agnostic
def test_enable_a_boot_timer_does_not_start_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """enable on a boot timer only enables it: starting it would run the job now (elapsed OnBootSec=)."""
    props = _enable_props("boot", [("OnBootUSec", 600_000_000, 0)])
    conn = _FakeConn([], props, frozenset({"boot.timer"}))
    _use_bus(monkeypatch, conn)
    info = SystemdScheduledTaskController().enable("boot")
    assert conn.verbs == ["EnableUnitFiles", "Reload"]
    assert (info.state, info.enabled) == (TaskState.READY, True)


@pytest.mark.os_agnostic
def test_enable_a_calendar_timer_starts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timer with no boot-relative part is enabled AND started, armed from now on."""
    conn = _FakeConn([], _enable_props("cal", [("OnUnitActiveUSec", 86_400_000_000, 0)]), frozenset({"cal.timer"}))
    _use_bus(monkeypatch, conn)
    SystemdScheduledTaskController().enable("cal")
    assert conn.verbs == ["EnableUnitFiles", "StartUnit", "Reload"]


@pytest.mark.os_agnostic
def test_enable_does_not_start_a_timer_it_cannot_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreadable TimersMonotonic: fail safe toward not running the job; it still arms at the next boot."""
    conn = _FakeConn([], _enable_props("odd", None), frozenset({"odd.timer"}))
    _use_bus(monkeypatch, conn)
    SystemdScheduledTaskController().enable("odd")
    assert "StartUnit" not in conn.verbs


@pytest.mark.os_agnostic
def test_listing_reads_an_enabled_inactive_timer_as_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """iter_timers applies the same enabled-and-inactive -> Ready rule as the controller."""
    props: dict[tuple[str, str, str], Any] = {
        ("armed.timer", _TIMER, "Unit"): "armed.service",
        ("off.timer", _TIMER, "Unit"): "off.service",
    }
    units = [("armed.timer", "", "inactive"), ("off.timer", "", "inactive")]
    conn = _FakeConn(units, props, frozenset({"armed.timer", "off.timer"}), file_states={"off.timer": "disabled"})
    _use_bus(monkeypatch, conn)
    armed, off = list(iter_timers())
    assert (armed.state, armed.enabled) == (TaskState.READY, True)
    assert (off.state, off.enabled) == (TaskState.DISABLED, False)
