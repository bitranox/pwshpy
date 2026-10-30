"""Native scheduled tasks via systemd timers: pure mapping (os_agnostic) + a live read (Linux).

The ActiveState/UnitFileState mappings and the unit-name/unit-text helpers are pure, so they
run on every OS. The live D-Bus read needs Linux + jeepney; it self-skips otherwise.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from elevation_doubles import RUNNING_AS_ROOT
from systemd_fake_bus import ACCESS_DENIED, Address, FakeConn, FakeCore, Message, Reply, use_bus

from pwshpy import __init__conf__
from pwshpy.adapters.native import systemd_dbus, systemd_timers
from pwshpy.adapters.native.systemd_dbus import ActiveState, UnitFileState
from pwshpy.adapters.native.systemd_timers import (
    SystemdScheduledTaskController,
    TimerBase,
    arms_on_register,
    base_name,
    battery_start_allowed,
    check_settings,
    iter_timers,
    linux_user,
    service_last_result,
    service_task_state,
    service_unit,
    service_user_id,
    start_time_limit,
    starts_when_armed,
    timer_enabled,
    timer_task_state,
    timer_to_task_state,
    timer_unit,
    unit_base,
    usec_to_datetime,
)
from pwshpy.domain.enums import TaskLogonType, TaskMultipleInstances, TaskRunLevel, TaskState
from pwshpy.domain.errors import (
    ConfigurationError,
    NativeAccessDeniedError,
    NativeCallError,
    PlatformUnsupportedError,
)
from pwshpy.domain.records import ScheduledTaskInfo
from pwshpy.domain.scheduling import (
    BootTrigger,
    DailyTrigger,
    LogonTrigger,
    OnceTrigger,
    TaskPrincipal,
    TaskSettings,
    TaskTrigger,
)

if sys.platform == "win32":
    import winreg

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
        ("reloading", TaskState.UNKNOWN),  # a timer never reloads: mapped by no table
        ("maintenance", TaskState.UNKNOWN),
        ("nonsense", TaskState.UNKNOWN),
    ],
)
def test_timer_state_maps(active_state: str, state: TaskState) -> None:
    """A wire timer ActiveState, parsed, maps to a TaskState (unknown -> Unknown, not a wrong guess)."""
    assert timer_to_task_state(ActiveState(active_state)) is state


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("unit_file_state", "enabled"),
    [
        ("enabled", True),
        ("enabled-runtime", True),
        ("static", True),  # no [Install]: cannot be disabled, and it runs (systemd-tmpfiles-clean)
        ("disabled", False),
        ("masked", False),
        ("", False),  # GetUnitFileState did not answer
        ("something-new", False),
    ],
)
def test_timer_enabled_maps(unit_file_state: str, enabled: bool) -> None:
    """timer_enabled is True for the enabled UnitFileStates and for a static timer, False for an unknown one."""
    assert timer_enabled(UnitFileState(unit_file_state)) is enabled


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
        ("something-new", True, TaskState.UNKNOWN),  # only inactive counts as armed
    ],
)
def test_timer_task_state(active_state: str, enabled: bool, state: TaskState) -> None:
    """An enabled, inactive timer is Ready (it arms at the next boot); the rest maps by ActiveState."""
    assert timer_task_state(ActiveState(active_state), enabled=enabled) is state


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("bases", "starts"),
    [
        ([], True),
        ([TimerBase.ON_UNIT_ACTIVE, TimerBase.ON_ACTIVE], True),  # relative to the timer/service, not to boot
        ([TimerBase.ON_UNIT_INACTIVE], True),
        ([TimerBase.UNKNOWN], True),  # a base a later systemd adds is not boot-relative
        ([TimerBase.ON_BOOT], False),
        ([TimerBase.ON_UNIT_ACTIVE, TimerBase.ON_STARTUP], False),
    ],
)
def test_starts_when_armed(bases: list[TimerBase], starts: bool) -> None:
    """A timer with a boot-relative monotonic timer must not be started: it would elapse at once."""
    assert starts_when_armed(bases) is starts


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("wire", "base"),
    [
        ("OnActiveUSec", TimerBase.ON_ACTIVE),
        ("OnBootUSec", TimerBase.ON_BOOT),
        ("OnStartupUSec", TimerBase.ON_STARTUP),
        ("OnUnitActiveUSec", TimerBase.ON_UNIT_ACTIVE),
        ("OnUnitInactiveUSec", TimerBase.ON_UNIT_INACTIVE),
        ("OnSomethingNewUSec", TimerBase.UNKNOWN),
        ("", TimerBase.UNKNOWN),
    ],
)
def test_timer_base_parses_the_wire_name(wire: str, base: TimerBase) -> None:
    """Each TimersMonotonic base name parses to its member; a name this version does not know is UNKNOWN."""
    assert TimerBase(wire) is base


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("active_state", "state"),
    [
        ("inactive", TaskState.READY),
        ("failed", TaskState.READY),
        ("active", TaskState.RUNNING),
        ("activating", TaskState.RUNNING),
        ("deactivating", TaskState.READY),
        ("something-new", TaskState.READY),
    ],
)
def test_service_task_state(active_state: str, state: TaskState) -> None:
    """An on-demand service task is RUNNING while executing, else READY (it can always be run)."""
    assert service_task_state(ActiveState(active_state)) is state


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
    assert 'ExecStart="/usr/bin/backup" --full /data' in text
    assert "Description=nightly backup" in text


@pytest.mark.os_agnostic
def test_service_unit_no_arguments() -> None:
    """With no arguments, ExecStart is just the program (no trailing space)."""
    assert 'ExecStart="/bin/true"\n' in service_unit("/bin/true", "", "")


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
    assert 'ExecStart="/usr/bin/date" +%%Y-%%m-%%d\n' in service
    assert "Description=100%% done\n" in service
    assert "Description=50%% off\n" in timer_unit("job", "50% off", [OnceTrigger(at=_AT)])


@pytest.mark.os_agnostic
@pytest.mark.parametrize("prefix", ["+", "!", "-", "@", ":"])
def test_service_unit_refuses_an_execstart_prefix_on_the_program(prefix: str) -> None:
    """[C3]: systemd reads a leading + ! - @ : on ExecStart='s executable as a prefix, quoted or not
    ('+' runs it with full privileges, ignoring User=), so a program starting with one is refused."""
    with pytest.raises(ConfigurationError, match="prefix"):
        service_unit(f"{prefix}/bin/true", "", "", user="1000")


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("program", "rendered"),
    [
        ("/opt/my app/run", '"/opt/my app/run"'),
        ("/opt/it's/run", '"/opt/it\'s/run"'),
        ('/opt/a"b/run', '"/opt/a\\"b/run"'),
        ("/opt/a\\b/run", '"/opt/a\\\\b/run"'),
    ],
    ids=["space", "single-quote", "double-quote", "backslash"],
)
def test_service_unit_quotes_the_program_as_one_word(program: str, rendered: str) -> None:
    """[C3]: unquoted, systemd splits ExecStart= on whitespace, so "/opt/my app/run" ran "/opt/my"
    (measured with systemd-analyze verify on 259); quoted with C escapes, the path is one word."""
    assert f"ExecStart={rendered} --flag\n" in service_unit(program, "--flag", "")


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


# --- Fake system bus (systemd_fake_bus): pins which D-Bus interface/property each run-info field reads

_TIMER = systemd_dbus.TIMER_IFACE
_SERVICE = systemd_dbus.SERVICE_IFACE
_T0 = 1_700_000_000_000_000  # 2023-11-14 22:13:20 UTC in microseconds
_T1 = _T0 + 3_600_000_000  # one hour later


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
    conn = FakeConn(units, props, frozenset({"backup.timer", "never.timer"}))
    use_bus(monkeypatch, conn)
    backup, never = list(iter_timers())
    assert backup.task_name == "backup"
    assert backup.last_run_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert backup.next_run_time == datetime(2023, 11, 14, 23, 13, 20, tzinfo=timezone.utc)
    assert backup.last_task_result == 3
    assert (never.last_run_time, never.last_task_result, never.next_run_time) == (None, None, None)
    assert (backup.user_id, backup.run_level) == ("0", None)
    # Its service has no unit file: LoadUnit still answers (a "not-found" object with User ""),
    # which must read as unknown, never as root's "0"; the timer is still listed.
    assert never.user_id == ""
    assert conn.closed is True
    # The timer's own path comes from ListUnits; only the triggered service needs one LoadUnit.
    assert conn.methods.count("LoadUnit") == 2
    assert conn.methods.count("GetUnit") == 0


@pytest.mark.os_agnostic
def test_controller_info_timer_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Controller snapshot of a timer task carries the same run info as the listing."""
    props: dict[tuple[str, str, str], Any] = {
        ("job.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "active",
        ("job.timer", _TIMER, "LastTriggerUSec"): _T0,
        ("job.timer", _TIMER, "NextElapseUSecRealtime"): _T1,
        ("job.timer", _TIMER, "Unit"): "job.service",
        ("job.timer", systemd_dbus.UNIT_IFACE, "Description"): "nightly job",
        **_service_props("job.service", code=2, status=9, started=_T0),
        ("job.service", _SERVICE, "User"): "1000",
    }
    # Stopped and disabled, both units are unloaded: only LoadUnit reaches them (GetUnit says "not loaded").
    unloaded = frozenset({"job.timer", "job.service"})
    conn = FakeConn([], props, frozenset({"job.timer"}), unloaded=unloaded)
    use_bus(monkeypatch, conn)
    info = SystemdScheduledTaskController().disable("job")
    assert (info.description, info.user_id, info.run_level) == ("nightly job", "1000", None)
    assert info.state is TaskState.READY
    assert info.last_run_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert info.next_run_time == datetime(2023, 11, 14, 23, 13, 20, tzinfo=timezone.utc)
    assert info.last_task_result == 137  # killed by SIGKILL
    assert conn.verbs == ["StopUnit", "DisableUnitFiles", "Reload"]
    assert conn.methods.count("LoadUnit") == 2  # one for the timer, one for its service
    assert conn.methods.count("GetUnit") == 0


@pytest.mark.os_agnostic
def test_controller_info_on_demand_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An on-demand service: last run is its own last start, the result its exit, nothing scheduled."""
    props = {**_service_props("adhoc.service", code=1, status=0, started=_T0), ("adhoc.service", _SERVICE, "User"): ""}
    # A finished oneshot is unloaded; before register writes it, systemd knows no such unit.
    conn = FakeConn([], props, unloaded=frozenset({"adhoc.service"}), pending=frozenset({"adhoc.service"}))
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)  # filesystem edge: never write /etc here
    info = SystemdScheduledTaskController().register("adhoc", program="/bin/true")
    assert (tmp_path / "adhoc.service").is_file()
    assert info.user_id == "0"  # an empty User= is root
    assert info.last_run_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert info.last_task_result == 0  # ran and succeeded, never folded into None
    assert info.next_run_time is None
    # Two for the ownership check (neither unit may exist yet), then the service once for every field.
    assert conn.methods.count("LoadUnit") == 3
    assert conn.methods.count("GetUnit") == 0


#: The head of a unit file pwshpy wrote: its [Unit] section carries the ownership key.
_OWNED = f"[Unit]\nDescription=pwshpy task\n{systemd_timers.OWNERSHIP_KEY}=test\n\n"

#: A task's two unit names, unknown to systemd until register writes them and reloads.
_NEW_JOB = frozenset({"job.service", "job.timer"})


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
    conn = FakeConn([], _job_props("job", user="1000"), frozenset({"job.timer"}), pending=_NEW_JOB)
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)  # filesystem edge: never write /etc here
    info = SystemdScheduledTaskController().register(
        "job", program="/bin/true", principal=TaskPrincipal(user_id="1000"), triggers=[DailyTrigger(at=_AT)]
    )
    assert "\nUser=1000\n" in (tmp_path / "job.service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 21:59:00 UTC\n" in (tmp_path / "job.timer").read_text(encoding="utf-8")
    # Reload after EnableUnitFiles too (like systemctl enable), so no unit is left "changed on disk".
    assert conn.verbs == ["Reload", "EnableUnitFiles", "Reload", "StartUnit"]
    assert (info.user_id, info.enabled, info.state) == ("1000", True, TaskState.READY)
    assert info.next_run_time == datetime(2023, 11, 14, 23, 13, 20, tzinfo=timezone.utc)
    # Two for the ownership check (neither unit may exist yet), then one each for the read-back.
    assert conn.methods.count("LoadUnit") == 4
    assert conn.methods.count("GetUnit") == 0
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_register_with_a_boot_trigger_enables_but_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A boot timer is only enabled: starting it now would fire an already-elapsed OnBootSec= at once."""
    props = {
        **_job_props("boot", user=""),
        ("boot.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "inactive",
        ("boot.timer", systemd_dbus.UNIT_IFACE, "Description"): "pwshpy task",
    }
    # Enabled but never started, neither unit is loaded: GetUnit would read every field as a default.
    conn = FakeConn(
        [],
        props,
        frozenset({"boot.timer"}),
        unloaded=frozenset({"boot.timer", "boot.service"}),
        pending=frozenset({"boot.timer", "boot.service"}),
    )
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    info = SystemdScheduledTaskController().register(
        "boot", program="/bin/true", triggers=[BootTrigger(delay=timedelta(seconds=600))]
    )
    assert "OnBootSec=600s\n" in (tmp_path / "boot.timer").read_text(encoding="utf-8")
    assert "User=" not in (tmp_path / "boot.service").read_text(encoding="utf-8")
    assert (info.user_id, info.description) == ("0", "pwshpy task")
    assert (info.state, info.enabled) == (TaskState.READY, True)  # armed for the next boot, not "Disabled"
    assert conn.verbs == ["Reload", "EnableUnitFiles", "Reload"]


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
    conn = FakeConn([], {})
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(PlatformUnsupportedError):
        SystemdScheduledTaskController().register("job", program="/bin/true", principal=principal, triggers=triggers)
    assert list(tmp_path.iterdir()) == []
    assert conn.methods == []


@pytest.mark.os_agnostic
def test_register_refuses_a_line_break_before_writing_or_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    conn = FakeConn([], {})
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(ConfigurationError):
        SystemdScheduledTaskController().register("job", program="/bin/true", description="a\nb")
    assert list(tmp_path.iterdir()) == []
    assert conn.methods == []


@pytest.mark.os_agnostic
def test_register_without_triggers_retires_a_previous_timer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Re-registering as on demand replaces the task (like CREATE_OR_UPDATE): the old .timer goes."""
    (tmp_path / "job.timer").write_text(_OWNED + "[Timer]\nOnBootSec=1s\n", encoding="utf-8")
    props = {**_service_props("job.service", code=0, status=0, started=0), ("job.service", _SERVICE, "User"): ""}
    conn = FakeConn([], props, pending=frozenset({"job.service"}))
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    info = SystemdScheduledTaskController().register("job", program="/bin/true")
    assert not (tmp_path / "job.timer").exists()
    assert (tmp_path / "job.service").is_file()
    assert conn.verbs == ["StopUnit", "DisableUnitFiles", "Reload"]
    assert info.next_run_time is None  # on demand again


@pytest.mark.os_agnostic
def test_register_with_triggers_stops_the_previous_timer_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A re-registered timer is stopped before its file is rewritten, so the old schedule cannot fire."""
    (tmp_path / "job.timer").write_text(_OWNED + "[Timer]\nOnCalendar=daily\n", encoding="utf-8")
    conn = FakeConn([], _job_props("job", user=""), frozenset({"job.timer"}), pending=frozenset({"job.service"}))
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    SystemdScheduledTaskController().register("job", program="/bin/true", triggers=[OnceTrigger(at=_AT)])
    assert "OnCalendar=2026-10-21 21:59:00 UTC\n" in (tmp_path / "job.timer").read_text(encoding="utf-8")
    assert conn.verbs == ["StopUnit", "Reload", "EnableUnitFiles", "Reload", "StartUnit"]


@pytest.mark.os_agnostic
def test_unregister_disables_the_timer_before_deleting_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """unregister drops the timers.target.wants link too, so no dangling enablement is left behind."""
    for unit in ("job.timer", "job.service"):
        (tmp_path / unit).write_text(_OWNED, encoding="utf-8")
    conn = FakeConn([], {})
    use_bus(monkeypatch, conn)
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
def test_unregister_touches_nothing_it_did_not_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[C1]: a same-named vendor unit (no file under /etc) is neither stopped nor disabled: with no
    unit pwshpy wrote there is no such task, and nothing is sent to systemd at all."""
    conn = FakeConn([], {}, frozenset({"apt-daily.timer", "apt-daily.service"}))
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(NativeCallError, match="no such task"):
        SystemdScheduledTaskController().unregister("apt-daily")
    assert conn.verbs == []


@pytest.mark.os_agnostic
def test_unregister_keeps_the_timer_file_when_disabling_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A real DisableUnitFiles failure raises before the file goes, so the wants link never dangles."""
    (tmp_path / "job.timer").write_text(_OWNED, encoding="utf-8")
    conn = FakeConn([], {}, failing=frozenset({"DisableUnitFiles"}))
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(NativeCallError, match="DisableUnitFiles"):
        SystemdScheduledTaskController().unregister("job")
    assert (tmp_path / "job.timer").exists()
    assert conn.closed is True


@pytest.mark.os_agnostic
@pytest.mark.skipif(
    sys.platform == "win32" or RUNNING_AS_ROOT,
    reason="needs a directory whose entries this process cannot delete: modes do not bind root; Windows ignores them",
)
def test_unregister_wraps_a_failed_delete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A unit file that cannot be deleted is a NativeCallError, not a raw OSError."""
    units = tmp_path / "units"
    units.mkdir()
    (units / "job.service").write_text(_OWNED, encoding="utf-8")
    conn = FakeConn([], {})
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", units)
    units.chmod(0o555)  # its entries can be read but not deleted
    try:
        with pytest.raises(NativeCallError, match="cannot delete unit"):
            SystemdScheduledTaskController().unregister("job")
    finally:
        units.chmod(0o755)


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
    conn = FakeConn([], props, frozenset({"boot.timer"}))
    use_bus(monkeypatch, conn)
    info = SystemdScheduledTaskController().enable("boot")
    assert conn.verbs == ["EnableUnitFiles", "Reload"]
    assert (info.state, info.enabled) == (TaskState.READY, True)


@pytest.mark.os_agnostic
def test_enable_a_calendar_timer_starts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timer with no boot-relative part is enabled AND started, armed from now on."""
    conn = FakeConn([], _enable_props("cal", [("OnUnitActiveUSec", 86_400_000_000, 0)]), frozenset({"cal.timer"}))
    use_bus(monkeypatch, conn)
    SystemdScheduledTaskController().enable("cal")
    # Reload before StartUnit (as register does): the new timers.target link is in place when it starts.
    assert conn.verbs == ["EnableUnitFiles", "Reload", "StartUnit"]


@pytest.mark.os_agnostic
def test_enable_starts_a_timer_whose_only_base_this_version_does_not_know(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TimersMonotonic base a later systemd adds parses to UNKNOWN, which is not boot-relative: started."""
    conn = FakeConn([], _enable_props("new", [("OnSomethingNewUSec", 60_000_000, 0)]), frozenset({"new.timer"}))
    use_bus(monkeypatch, conn)
    SystemdScheduledTaskController().enable("new")
    assert conn.verbs == ["EnableUnitFiles", "Reload", "StartUnit"]


@pytest.mark.os_agnostic
def test_enable_does_not_start_a_timer_it_cannot_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreadable TimersMonotonic: fail safe toward not running the job; it still arms at the next boot."""
    conn = FakeConn([], _enable_props("odd", None), frozenset({"odd.timer"}))
    use_bus(monkeypatch, conn)
    SystemdScheduledTaskController().enable("odd")
    assert "StartUnit" not in conn.verbs


@pytest.mark.os_agnostic
def test_enable_raises_a_refused_monotonic_read_before_changing_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """[250] A refused TimersMonotonic read is no "cannot inspect": enable raises it before EnableUnitFiles."""
    props = _enable_props("odd", [("OnBootUSec", 600_000_000, 0)])
    conn = FakeConn([], props, frozenset({"odd.timer"}), refused_reads=frozenset({("odd.timer", "TimersMonotonic")}))
    use_bus(monkeypatch, conn)
    with pytest.raises(NativeAccessDeniedError, match="TimersMonotonic"):
        SystemdScheduledTaskController().enable("odd")
    assert conn.verbs == []
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_listing_reads_an_enabled_inactive_timer_as_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """iter_timers applies the same enabled-and-inactive -> Ready rule as the controller."""
    props: dict[tuple[str, str, str], Any] = {
        ("armed.timer", _TIMER, "Unit"): "armed.service",
        ("off.timer", _TIMER, "Unit"): "off.service",
    }
    units = [("armed.timer", "", "inactive"), ("off.timer", "", "inactive")]
    conn = FakeConn(units, props, frozenset({"armed.timer", "off.timer"}), file_states={"off.timer": "disabled"})
    use_bus(monkeypatch, conn)
    armed, off = list(iter_timers())
    assert (armed.state, armed.enabled) == (TaskState.READY, True)
    assert (off.state, off.enabled) == (TaskState.DISABLED, False)


def _listing_conn(*, failing: frozenset[str] = frozenset()) -> FakeConn:
    """A box's timers as systemd 259 reports them.

    ListUnits: one real loaded timer, one ``not-found`` (referenced, no unit file) and one
    ``masked``. Unit files only (unloaded): a disabled timer, a static vendor timer under
    /usr/lib, plus a masked one, an alias and a template, which are no tasks.
    """
    props: dict[tuple[str, str, str], Any] = {
        ("loaded.timer", _TIMER, "Unit"): "loaded.service",
        ("vendor.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "active",
        ("vendor.timer", systemd_dbus.UNIT_IFACE, "Description"): "Vendor job",
        ("vendor.timer", _TIMER, "Unit"): "vendor.service",
        ("off.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "inactive",
        ("off.timer", systemd_dbus.UNIT_IFACE, "Description"): "Off job",
        ("off.timer", _TIMER, "Unit"): "off.service",
        ("off.timer", _TIMER, "LastTriggerUSec"): _T0,
        **_service_props("off.service", code=1, status=0, started=_T0),
        ("off.service", _SERVICE, "User"): "1000",
    }
    unit_files = frozenset(
        {
            "loaded.timer",
            "off.timer",
            "gone.timer",
            "veiled.timer",
            "alias.timer",
            "tpl@.timer",
            "vendor.timer",
            "x.service",
        }
    )
    return FakeConn(
        [
            ("loaded.timer", "Loaded", "active"),
            ("ghost.timer", "ghost.timer", "inactive"),
            ("veiled.timer", "", "inactive"),
        ],
        props,
        unit_files,
        file_states={
            "off.timer": "disabled",
            "gone.timer": "masked",
            "veiled.timer": "masked",
            "alias.timer": "alias",
            "vendor.timer": "static",
        },
        failing=failing,
        unloaded=frozenset({"off.timer", "off.service", "vendor.timer"}),
        load_states={"ghost.timer": "not-found", "veiled.timer": "masked"},
        file_dirs={"vendor.timer": "/usr/lib/systemd/system"},
    )


_NOTHING_FAILS: frozenset[str] = frozenset()


@pytest.mark.os_agnostic
@pytest.mark.parametrize("failing", [_NOTHING_FAILS, frozenset({"ListUnitFilesByPatterns"})])
def test_iter_timers_lists_a_timer_that_is_not_loaded(monkeypatch: pytest.MonkeyPatch, failing: frozenset[str]) -> None:
    """A disabled timer is unloaded, so ListUnits misses it; its unit file lists it, read via LoadUnit.

    Like Get-ScheduledTask listing a disabled task. Each timer once (no duplicate for the loaded
    one), never a masked timer or a template; the second case is a systemd without
    ListUnitFilesByPatterns (before 230), where the full ListUnitFiles is filtered instead.
    """
    conn = _listing_conn(failing=failing)
    use_bus(monkeypatch, conn)
    # Not listed: ghost (LoadState not-found), veiled/gone (masked), alias (an alias name), tpl@ (template).
    loaded, off, vendor = list(iter_timers())
    assert (loaded.task_name, loaded.description) == ("loaded", "Loaded")
    assert (off.task_name, off.description, off.user_id) == ("off", "Off job", "1000")
    assert (off.state, off.enabled) == (TaskState.DISABLED, False)
    # A vendor timer under /usr/lib is a real task too; static reads enabled (it cannot be disabled and runs).
    assert (vendor.task_name, vendor.description, vendor.state, vendor.enabled) == (
        "vendor",
        "Vendor job",
        TaskState.READY,
        True,
    )
    assert off.last_run_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_iter_timers_still_lists_loaded_timers_when_no_unit_file_listing_answers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail-soft: with both unit-file listings refused, the loaded timers are still listed (and it is logged)."""
    conn = _listing_conn(failing=frozenset({"ListUnitFilesByPatterns", "ListUnitFiles"}))
    use_bus(monkeypatch, conn)
    with caplog.at_level(logging.DEBUG, logger=systemd_timers.__name__):
        assert [t.task_name for t in iter_timers()] == ["loaded"]
    # [54] a WARNING, not a debug line: every unloaded timer is missing from what was listed.
    assert [r.levelno for r in caplog.records if "unit files" in r.getMessage()] == [logging.WARNING]


@pytest.mark.os_agnostic
def test_strict_raises_when_no_unit_file_listing_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """[54] strict=True: the refused unit-file listing raises instead of dropping every unloaded timer.

    Lazy: the loaded timer from ListUnits is yielded first; the raise comes with the file pass.
    """
    conn = _listing_conn(failing=frozenset({"ListUnitFilesByPatterns", "ListUnitFiles"}))
    use_bus(monkeypatch, conn)
    timers = iter_timers(strict=True)
    assert next(timers).task_name == "loaded"
    with pytest.raises(NativeCallError, match="timer unit files"):
        list(timers)
    assert conn.closed is True


def _refused_load_conn(failure: str) -> FakeConn:
    """Two unloaded timer files; reading the first fails at ``failure``.

    ``LoadUnit``: the LoadUnit call itself fails. ``LoadState``: LoadUnit answers, but reading
    the loaded object's LoadState fails - which must not pass for systemd saying "not-found".
    """
    props: dict[tuple[str, str, str], Any] = {
        ("good.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "inactive",
        ("good.timer", systemd_dbus.UNIT_IFACE, "Description"): "Good job",
    }
    return FakeConn(
        [],
        props,
        frozenset({"denied.timer", "good.timer"}),
        file_states={"denied.timer": "disabled", "good.timer": "disabled"},
        unloaded=frozenset({"denied.timer", "good.timer"}),
        refused_loads=frozenset({"denied.timer"}) if failure == "LoadUnit" else frozenset(),
        refused_reads=frozenset({("denied.timer", "LoadState")}) if failure == "LoadState" else frozenset(),
    )


_LOAD_FAILURES = ["LoadUnit", "LoadState"]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("failure", _LOAD_FAILURES)
def test_strict_raises_naming_a_timer_file_that_cannot_be_loaded(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """[54] strict=True: a listed timer file whose LoadUnit call or LoadState read fails raises, naming it."""
    use_bus(monkeypatch, _refused_load_conn(failure))
    with pytest.raises(NativeCallError, match=r"'denied\.timer'") as caught:
        list(iter_timers(strict=True))
    assert failure in str(caught.value)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("failure", _LOAD_FAILURES)
def test_lenient_skips_a_timer_file_that_cannot_be_loaded_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    """[54] The default lists on past a timer file it cannot load, but logs a WARNING naming it."""
    use_bus(monkeypatch, _refused_load_conn(failure))
    with caplog.at_level(logging.WARNING, logger=systemd_timers.__name__):
        assert [t.task_name for t in iter_timers()] == ["good"]
    (warning,) = caplog.records
    assert warning.levelno == logging.WARNING
    assert "'denied.timer'" in warning.getMessage()
    assert failure in warning.getMessage()


def _two_timer_conn(refusal: str) -> FakeConn:
    """Two timers, ``denied.timer`` first; systemd refuses one read of it, named by ``refusal``.

    ``GetUnitFileState``: both are loaded (``ListUnits`` lists them) and the enablement lookup of
    the first is refused. ``Description``: both are unloaded unit files and reading the first's
    description is refused.
    """
    loaded = refusal == "GetUnitFileState"
    props: dict[tuple[str, str, str], Any] = {}
    for base in ("denied", "good"):
        props[(f"{base}.timer", systemd_dbus.UNIT_IFACE, "ActiveState")] = "active"
        props[(f"{base}.timer", systemd_dbus.UNIT_IFACE, "Description")] = base
        props[(f"{base}.timer", _TIMER, "Unit")] = f"{base}.service"
    return FakeConn(
        [("denied.timer", "denied", "active"), ("good.timer", "good", "active")] if loaded else [],
        props,
        frozenset({"denied.timer", "good.timer"}),
        unloaded=frozenset() if loaded else frozenset({"denied.timer", "good.timer"}),
        denied_calls=frozenset({("GetUnitFileState", "denied.timer")}) if loaded else frozenset(),
        refused_reads=frozenset() if loaded else frozenset({("denied.timer", "Description")}),
    )


_REFUSALS = ["GetUnitFileState", "Description"]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("refusal", _REFUSALS)
def test_lenient_skips_a_timer_systemd_refuses_to_read_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, refusal: str
) -> None:
    """[250] A refused read skips that timer with a WARNING naming the refusal; the next is listed.

    Before, the refused read answered as an absent value: a refused enablement lookup listed the
    timer as disabled, a refused description as an empty one.
    """
    use_bus(monkeypatch, _two_timer_conn(refusal))
    with caplog.at_level(logging.WARNING, logger=systemd_timers.__name__):
        assert [t.task_name for t in iter_timers()] == ["good"]
    (warning,) = caplog.records
    assert "'denied.timer'" in warning.getMessage()
    assert ACCESS_DENIED in warning.getMessage()


@pytest.mark.os_agnostic
@pytest.mark.parametrize("refusal", _REFUSALS)
def test_strict_raises_on_a_timer_systemd_refuses_to_read(monkeypatch: pytest.MonkeyPatch, refusal: str) -> None:
    """[250] strict=True: the refused read raises a NativeAccessDeniedError naming the timer."""
    conn = _two_timer_conn(refusal)
    use_bus(monkeypatch, conn)
    with pytest.raises(NativeAccessDeniedError, match=r"'denied\.timer'"):
        list(iter_timers(strict=True))
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_load_unit_raises_on_an_unreadable_load_state_and_load_unit_path_stays_fail_soft() -> None:
    """A failed LoadState read is "could not ask", not "not-found"; the fail-soft wrapper still reads None.

    load_unit_path's callers (field read-backs, the controllers, the services listing) keep their
    behaviour: before, the failed read defaulted to not-found and they got None too. A read systemd
    REFUSES is the exception: load_unit_path raises it ([250], ``test_systemd_dbus_errors``).
    """
    conn = FakeConn(
        [],
        {("real.service", _SERVICE, "User"): "1000"},
        broken_reads=frozenset({("real.service", "LoadState")}),
    )
    bus = systemd_dbus.Bus(conn=conn, core=FakeCore(), mgr=Address("/org/freedesktop/systemd1"))
    with pytest.raises(NativeCallError, match=r"LoadState of 'real\.service'"):
        systemd_dbus.load_unit(bus, "real.service")
    assert systemd_dbus.load_unit_path(bus, "real.service") is None
    assert systemd_dbus.load_unit(bus, "ghost.service") is None  # systemd's own not-found answer


@pytest.mark.os_agnostic
def test_strict_lists_a_readable_box_like_lenient_and_keeps_the_deliberate_filtering(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[54] strict changes only read FAILURES: not-found, masked, alias and template units stay unlisted.

    The listing box holds every kind of unit the listing filters on purpose; strict must neither
    raise on them nor list them, and must yield what the default yields, with no warning.
    """
    use_bus(monkeypatch, _listing_conn())
    with caplog.at_level(logging.WARNING, logger=systemd_timers.__name__):
        lenient = [t.to_dict() for t in iter_timers()]
        use_bus(monkeypatch, _listing_conn())
        strict = [t.to_dict() for t in iter_timers(strict=True)]
    assert strict == lenient
    assert [t["task_name"] for t in strict] == ["loaded", "off", "vendor"]
    assert caplog.records == []


@pytest.mark.os_agnostic
def test_iter_timers_lists_unit_files_only_when_the_loaded_timers_are_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lazy: taking the first (loaded) timer costs no unit-file listing and no LoadUnit of another timer."""
    conn = _listing_conn()
    use_bus(monkeypatch, conn)
    first = next(iter_timers())
    assert first.task_name == "loaded"
    assert "ListUnitFilesByPatterns" not in conn.methods
    assert "ListUnitFiles" not in conn.methods


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("last_trigger", "service_started", "expected"),
    [
        (0, _T0, _T0),  # an on-demand start (run / systemctl start) moves last_run_time, as on Windows
        (_T1, _T0, _T1),  # the timer fired later than the last manual start
        (_T1, 0, _T1),  # Persistent= trigger survived a reboot that cleared the service's start time
        (0, 0, None),  # never ran
    ],
)
def test_timer_last_run_time_is_the_later_of_trigger_and_service_start(
    monkeypatch: pytest.MonkeyPatch, last_trigger: int, service_started: int, expected: int | None
) -> None:
    """last_run_time is the later of the timer's LastTriggerUSec and the service's ExecMainStartTimestamp."""
    props: dict[tuple[str, str, str], Any] = {
        ("job.timer", _TIMER, "Unit"): "job.service",
        ("job.timer", _TIMER, "LastTriggerUSec"): last_trigger,
        **_service_props("job.service", code=1 if service_started else 0, status=0, started=service_started),
    }
    conn = FakeConn([("job.timer", "", "active")], props, frozenset({"job.timer"}))
    use_bus(monkeypatch, conn)
    (job,) = list(iter_timers())
    assert job.last_run_time == (None if expected is None else usec_to_datetime(expected))


@pytest.mark.os_agnostic
def test_load_unit_path_reads_a_not_found_unit_as_unreadable() -> None:
    """systemd answers LoadUnit for a name with no unit file (LoadState not-found): that is None.

    Otherwise every property reads systemd's defaults - User "" would claim the task runs as root.
    """
    conn = FakeConn([], {("real.service", _SERVICE, "User"): "1000"})
    bus = systemd_dbus.Bus(conn=conn, core=FakeCore(), mgr=Address("/org/freedesktop/systemd1"))
    assert systemd_dbus.load_unit_path(bus, "ghost.service") is None
    assert systemd_dbus.load_unit_path(bus, "real.service") == "/u/real.service"
    assert conn.methods == ["LoadUnit", "Get", "LoadUnit", "Get"]  # one LoadState read per lookup


@pytest.mark.os_agnostic
@pytest.mark.parametrize("verb", ["enable", "disable"])
def test_enable_and_disable_refuse_a_static_timer(monkeypatch: pytest.MonkeyPatch, verb: str) -> None:
    """A static timer has no [Install] section: systemd's enable/disable change nothing, so refuse.

    Otherwise the call returns a task still reading enabled, as if it had worked - and disable
    would also have stopped the timer on the way. Nothing may be sent before the refusal.
    """
    props = _enable_props("tmpfiles", [("OnBootUSec", 900_000_000, 0)])
    conn = FakeConn([], props, frozenset({"tmpfiles.timer"}), file_states={"tmpfiles.timer": "static"})
    use_bus(monkeypatch, conn)
    with pytest.raises(NativeCallError, match=r"static.*no \[Install\]") as refused:
        getattr(SystemdScheduledTaskController(), verb)("tmpfiles")
    assert conn.verbs == []
    # [C4]: masking only ever stops a timer, so it is offered for disable and never for enable.
    assert ("mask" in str(refused.value)) is (verb == "disable"), str(refused.value)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("verb", ["enable", "disable"])
def test_schedule_refusals_name_the_verb_as_a_plain_word(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verb: str
) -> None:
    """The refusals spell the verb as the word itself on every Python (never an enum member's repr)."""
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)  # filesystem edge: no adhoc.timer under /etc
    props = _enable_props("tmpfiles", [("OnBootUSec", 900_000_000, 0)])
    static = FakeConn([], props, frozenset({"tmpfiles.timer"}), file_states={"tmpfiles.timer": "static"})
    use_bus(monkeypatch, static)
    with pytest.raises(NativeCallError) as refused:
        getattr(SystemdScheduledTaskController(), verb)("tmpfiles")
    assert f"so systemd has nothing to {verb} - " in str(refused.value)
    use_bus(monkeypatch, FakeConn([], {}))
    with pytest.raises(NativeCallError) as on_demand:
        getattr(SystemdScheduledTaskController(), verb)("adhoc")
    assert str(on_demand.value) == (
        f"task 'adhoc' is on-demand (no .timer); nothing to {verb} - run it directly, or unregister it."
    )


@pytest.mark.os_agnostic
def test_iter_timers_lists_a_timer_whose_states_this_version_does_not_know(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown ListUnits and unit-file states keep a timer listed: state Unknown, not enabled.

    The unknown LoadState matches no skip rule and the unknown UnitFileState counts as not enabled,
    as when the states were compared as text.
    """
    conn = FakeConn(
        [("new.timer", "New job", "from-the-future")],
        {("new.timer", _TIMER, "Unit"): "new.service"},
        frozenset({"new.timer"}),
        file_states={"new.timer": "from-the-future"},
        load_states={"new.timer": "from-the-future"},
    )
    use_bus(monkeypatch, conn)
    assert [(t.task_name, t.state, t.enabled) for t in iter_timers()] == [("new", TaskState.UNKNOWN, False)]


_RUNNING_STATES = ["active", "activating", "deactivating", "reloading"]


def _orphan_timer_conn(load_state: str, active_state: str) -> FakeConn:
    """A loaded ``orphan.timer`` whose ListUnits LoadState is ``load_state`` while it is ``active_state``.

    not-found: its unit file was deleted under it (no file, GetUnitFileState refuses). masked: a
    ``systemctl mask`` without ``--now`` left it running (its file state reads masked).
    """
    masked = load_state == "masked"
    return FakeConn(
        [("orphan.timer", "Orphan job", active_state)],
        {("orphan.timer", _TIMER, "Unit"): "orphan.service"},
        frozenset({"orphan.timer"}) if masked else frozenset(),
        file_states={"orphan.timer": "masked"} if masked else None,
        load_states={"orphan.timer": load_state},
    )


@pytest.mark.os_agnostic
@pytest.mark.parametrize("load_state", ["not-found", "masked"])
@pytest.mark.parametrize("active_state", _RUNNING_STATES)
def test_iter_timers_lists_a_running_timer_whose_unit_is_gone_or_masked(
    monkeypatch: pytest.MonkeyPatch, load_state: str, active_state: str
) -> None:
    """A not-found or masked timer that is still running is a live task: listed once, never enabled."""
    conn = _orphan_timer_conn(load_state, active_state)
    use_bus(monkeypatch, conn)
    tasks = list(iter_timers())
    assert [(t.task_name, t.description, t.enabled) for t in tasks] == [("orphan", "Orphan job", False)]
    assert tasks[0].state is timer_to_task_state(ActiveState(active_state))


@pytest.mark.os_agnostic
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("load_state", ["not-found", "masked"])
@pytest.mark.parametrize("active_state", ["inactive", "failed"])
def test_iter_timers_skips_a_stopped_timer_whose_unit_is_gone_or_masked(
    monkeypatch: pytest.MonkeyPatch, load_state: str, active_state: str, strict: bool
) -> None:
    """Stopped, a not-found or masked timer is no task: only a reference or an unstartable name is left.

    Deliberate filtering, not a read failure, so ``strict`` skips it the same way.
    """
    conn = _orphan_timer_conn(load_state, active_state)
    use_bus(monkeypatch, conn)
    assert list(iter_timers(strict=strict)) == []


@pytest.mark.os_agnostic
def test_iter_timers_lists_a_cached_not_found_timer_whose_file_appeared(monkeypatch: pytest.MonkeyPatch) -> None:
    """systemd cached the name as not-found, then its file was written without a daemon-reload.

    ListUnits still reports the cached not-found, so that pass skips it; the unit-file pass must
    still list it, read through LoadUnit, which reloads it from the new file.
    """
    props: dict[tuple[str, str, str], Any] = {
        ("late.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "inactive",
        ("late.timer", systemd_dbus.UNIT_IFACE, "Description"): "Late job",
        ("late.timer", _TIMER, "Unit"): "late.service",
    }
    conn = FakeConn(
        [("late.timer", "late.timer", "inactive")],
        props,
        frozenset({"late.timer"}),
        file_states={"late.timer": "disabled"},
        load_states={"late.timer": "not-found"},
        appeared=frozenset({"late.timer"}),
    )
    use_bus(monkeypatch, conn)
    assert [(t.task_name, t.description, t.enabled) for t in iter_timers()] == [("late", "Late job", False)]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("cached", [False, True])
def test_iter_timers_skips_a_stale_timer_file(monkeypatch: pytest.MonkeyPatch, cached: bool, strict: bool) -> None:
    """A listed timer file whose LoadUnit still reads not-found (a broken or dangling file) is no task.

    The services listing skips such a file too; listing it would show a task with every field a
    default. ``cached``: ListUnits also carries the name as a stopped not-found entry, which no
    longer marks it seen, so the file pass is what must skip it. ``strict`` does not turn this
    deliberate filtering into an error: systemd answered, and its answer is "no such unit".
    """
    conn = FakeConn(
        [("stale.timer", "stale.timer", "inactive")] if cached else [],
        {},
        frozenset({"stale.timer"}),
        file_states={"stale.timer": "disabled"},
        load_states={"stale.timer": "not-found"},
    )
    use_bus(monkeypatch, conn)
    assert list(iter_timers(strict=strict)) == []


# --- [C1] unit names systemd accepts, and units pwshpy owns -------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "task_path",
    ["My Backup", "/", "", "job@instance", "j" + chr(0xF6) + "b", "a" * 248],
    ids=["space", "empty-after-the-folder", "empty", "template-or-instance", "non-ascii", "one-past-the-limit"],
)
@pytest.mark.parametrize("verb", ["register", "unregister"])
def test_a_name_systemd_would_not_accept_is_refused_before_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, task_path: str, verb: str
) -> None:
    """[C1]: an invalid unit name was written and read back as a plausible task; now it is refused
    before a file is written or the bus is opened (systemd 259 refuses "My Backup.service", and a
    name of 256 characters with its suffix)."""
    conn = FakeConn([], {})
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    controller = SystemdScheduledTaskController()
    with pytest.raises(ConfigurationError, match="unit name"):
        if verb == "register":
            controller.register(task_path, program="/bin/true")
        else:
            controller.unregister(task_path)
    assert list(tmp_path.iterdir()) == []
    assert conn.methods == []


def _long_paths_enabled() -> bool:
    """Whether this host lets a process use a path longer than MAX_PATH (260): always off Windows."""
    if sys.platform != "win32":
        return True
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            value, _kind = winreg.QueryValueEx(key, "LongPathsEnabled")
    except OSError:
        return False
    return bool(value == 1)


@pytest.mark.os_agnostic
@pytest.mark.skipif(
    not _long_paths_enabled(),
    reason="HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem LongPathsEnabled is not 1, so Windows refuses "
    "the 255-character unit file name under the temp directory (past MAX_PATH)",
)
def test_the_longest_name_systemd_accepts_registers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[C1]: 247 characters plus ".service" is systemd's 255 (UNIT_NAME_MAX 256 includes the NUL)."""
    base = "a" * 247
    props = {
        **_service_props(f"{base}.service", code=0, status=0, started=0),
        (f"{base}.service", _SERVICE, "User"): "",
    }
    conn = FakeConn([], props, pending=frozenset({f"{base}.service"}))
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    SystemdScheduledTaskController().register(base, program="/bin/true")
    assert (tmp_path / f"{base}.service").is_file()


def _unit_section(text: str) -> list[str]:
    """The lines of a unit file's [Unit] section."""
    section = text.split("\n\n", 1)[0].splitlines()
    assert section[0] == "[Unit]", text
    return section[1:]


@pytest.mark.os_agnostic
def test_register_stamps_both_units_with_the_ownership_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[C1]: every unit pwshpy writes carries X-PwshPy= in [Unit], which systemd ignores (an X- key,
    systemd.unit(5); measured silent with systemd-analyze verify on 259) and pwshpy reads back."""
    conn = FakeConn([], _job_props("job", user=""), frozenset({"job.timer"}), pending=_NEW_JOB)
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    SystemdScheduledTaskController().register("job", program="/bin/true", triggers=[OnceTrigger(at=_AT)])
    for unit in ("job.service", "job.timer"):
        lines = _unit_section((tmp_path / unit).read_text(encoding="utf-8"))
        assert any(line.startswith(f"{systemd_timers.OWNERSHIP_KEY}=") for line in lines), (unit, lines)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("unit", ["job.service", "job.timer"])
def test_register_never_overwrites_a_unit_it_did_not_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unit: str
) -> None:
    """[C1]: an admin's /etc/systemd/system/<name> without the ownership key is left exactly as it
    was, and nothing is sent to systemd."""
    foreign = "[Unit]\nDescription=the admin's own\n\n[Service]\nExecStart=/usr/sbin/nginx\n"
    (tmp_path / unit).write_text(foreign, encoding="utf-8")
    conn = FakeConn([], {}, pending=_NEW_JOB - {unit})
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(NativeCallError, match="not written by pwshpy"):
        SystemdScheduledTaskController().register("job", program="/bin/true", triggers=[OnceTrigger(at=_AT)])
    assert (tmp_path / unit).read_text(encoding="utf-8") == foreign
    assert sorted(path.name for path in tmp_path.iterdir()) == [unit]
    assert conn.verbs == []


@pytest.mark.os_agnostic
@pytest.mark.parametrize("unit", ["nginx.service", "nginx.timer"])
def test_register_never_shadows_a_unit_systemd_already_has(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unit: str
) -> None:
    """[C1]: a vendor unit under /lib has no file in /etc to judge; systemd knowing the name is enough
    to refuse, since a file in /etc would shadow it (and a vendor .timer would trigger our service)."""
    conn = FakeConn([], {}, frozenset({unit}), file_dirs={unit: "/lib/systemd/system"})
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(NativeCallError, match="not written by pwshpy"):
        SystemdScheduledTaskController().register("nginx", program="/bin/true")
    assert list(tmp_path.iterdir()) == []
    assert conn.verbs == []


@pytest.mark.os_agnostic
def test_register_refuses_when_systemd_cannot_say_whether_the_name_is_taken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[C1]: a probe that gets no answer takes the strict branch: nothing is written."""
    conn = FakeConn([], {}, refused_loads=frozenset({"job.service"}))
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(NativeCallError):
        SystemdScheduledTaskController().register("job", program="/bin/true")
    assert list(tmp_path.iterdir()) == []
    assert conn.verbs == []


@pytest.mark.os_agnostic
@pytest.mark.parametrize("unit", ["job.service", "job.timer"])
def test_unregister_never_deletes_a_unit_it_did_not_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unit: str
) -> None:
    """[C1]: an /etc unit without the ownership key is neither stopped, disabled nor deleted."""
    foreign = "[Unit]\nDescription=the admin's own\n\n[Service]\nExecStart=/usr/sbin/nginx\n"
    (tmp_path / unit).write_text(foreign, encoding="utf-8")
    conn = FakeConn([], {})
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(NativeCallError, match="not written by pwshpy"):
        SystemdScheduledTaskController().unregister("job")
    assert (tmp_path / unit).read_text(encoding="utf-8") == foreign
    assert conn.verbs == []


@pytest.mark.os_agnostic
def test_unregister_refuses_a_unit_file_it_cannot_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[C1]: ownership that cannot be read is not ownership: nothing is touched."""
    (tmp_path / "job.service").mkdir()  # reading a directory fails
    conn = FakeConn([], {})
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(NativeCallError, match="cannot read"):
        SystemdScheduledTaskController().unregister("job")
    assert (tmp_path / "job.service").is_dir()
    assert conn.verbs == []


@pytest.mark.os_agnostic
def test_unregister_removes_an_on_demand_task_it_wrote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[C1] control: a marked .service alone (an on-demand task) is stopped and deleted."""
    (tmp_path / "job.service").write_text(_OWNED + "[Service]\nType=oneshot\nExecStart=/bin/true\n", encoding="utf-8")
    conn = FakeConn([], {})
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    SystemdScheduledTaskController().unregister("job")
    assert list(tmp_path.iterdir()) == []
    assert conn.verbs == ["StopUnit", "Reload"]


# --- [X21 L1] every ExecStart= prefix systemd honours, and control characters in the program -----------


@pytest.mark.os_agnostic
def test_service_unit_refuses_the_shell_prefix_on_the_program() -> None:
    """[X21 L1]: systemd 259 reads a leading '|' as "spawn through the shell", inside quotes too
    (``ExecStart="|a/bin/true"`` verifies clean), so it is refused like the other prefixes."""
    with pytest.raises(ConfigurationError, match="prefix"):
        service_unit("|/bin/true", "", "", user="1000")


@pytest.mark.os_agnostic
def test_service_unit_refuses_a_lone_semicolon_as_the_program() -> None:
    """[X21 L1]: a standalone ';' separates command lines in ExecStart=, so it is no executable."""
    with pytest.raises(ConfigurationError, match="';'"):
        service_unit(";", "/bin/true", "")


@pytest.mark.os_agnostic
@pytest.mark.parametrize("char", ["\t", "\x01", "\x1b", "\x7f"], ids=["tab", "soh", "escape", "delete"])
def test_service_unit_refuses_a_control_character_in_the_program(char: str) -> None:
    """[X21 L1]: systemd.service(5) does not allow control characters in a command line."""
    with pytest.raises(ConfigurationError, match="control character"):
        service_unit(f"/bin/tr{char}ue", "", "")


# --- [X21 L2] instance units: the verbs that write nothing take systemd's full unit-name grammar ------


def _instance_conn(name: str) -> FakeConn:
    """A box holding one listed, enabled instance timer ``name`` (with its calendar schedule)."""
    base = name[: -len(".timer")]
    props = {
        **_job_props(base, user=""),
        (name, systemd_dbus.UNIT_IFACE, "ActiveState"): "inactive",
        (name, _TIMER, "TimersMonotonic"): [("OnUnitActiveUSec", 86_400_000_000, 0)],
    }
    return FakeConn([(name, "Scrub", "inactive")], props, frozenset({name}))


@pytest.mark.os_agnostic
@pytest.mark.parametrize("timer", ["zfs-scrub-monthly@rpool.timer", "btrfs-scrub@-.timer"])
def test_a_listed_instance_timer_can_be_run_stopped_enabled_and_disabled(
    monkeypatch: pytest.MonkeyPatch, timer: str
) -> None:
    """[X21 L2]: 1.5.2 ran the instance timers get_scheduled_task lists; the strict register-time
    name check must not refuse them for the verbs that write nothing."""
    conn = _instance_conn(timer)
    use_bus(monkeypatch, conn)
    (listed,) = list(iter_timers())
    controller = SystemdScheduledTaskController()
    controller.run(listed.task_name)
    controller.stop(listed.task_name)
    assert controller.enable(listed.task_name).task_name == listed.task_name
    assert controller.disable(listed.task_name).task_name == listed.task_name
    assert conn.verbs == [
        "StartUnit",
        "StopUnit",
        "EnableUnitFiles",
        "Reload",
        "StartUnit",
        "StopUnit",
        "DisableUnitFiles",
        "Reload",
    ]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("verb", ["run", "stop", "enable", "disable"])
@pytest.mark.parametrize(
    ("task_path", "match"),
    [
        ("scrub@", "template"),
        ("scrub@.timer", "template"),
        ("@rpool", "unit name"),
        ("a b@c", "unit name"),
        ("a@b c", "instance"),
        ("a@b%c", "instance"),
    ],
    ids=[
        "bare-template",
        "template-timer",
        "empty-prefix",
        "space-in-prefix",
        "space-in-instance",
        "percent-in-instance",
    ],
)
def test_a_template_or_invalid_instance_name_is_refused_before_the_bus(
    monkeypatch: pytest.MonkeyPatch, verb: str, task_path: str, match: str
) -> None:
    """[X21 L2]: a bare template (empty instance) is no unit to act on; an empty prefix is no name.
    [X22 T1]: a character systemd does not allow in an instance is refused too (the instance check)."""
    conn = FakeConn([], {})
    use_bus(monkeypatch, conn)
    with pytest.raises(ConfigurationError, match=match):
        getattr(SystemdScheduledTaskController(), verb)(task_path)
    assert conn.methods == []


# --- [X22 T1] systemd's \xNN escapes are part of a unit name, not a folder separator ----------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "timer",
    [r"zfs-scrub-monthly@my\x2dpool.timer", r"backup\x2dnightly.timer", r"scrub@a\x5cb\x2dc.timer"],
    ids=["escaped-instance", "escaped-prefix", "escaped-backslash-in-instance"],
)
def test_a_listed_escaped_timer_is_addressed_by_its_exact_unit_name(
    monkeypatch: pytest.MonkeyPatch, timer: str
) -> None:
    """[X22 T1]: base_name read the backslash of systemd's \\xNN escape as a folder separator, so the
    verbs sent a listed ``zfs-scrub-monthly@my\\x2dpool.timer`` to ``x2dpool``. Every unit name on the
    bus is now the listed one, escape included."""
    conn = _instance_conn(timer)
    use_bus(monkeypatch, conn)
    (listed,) = list(iter_timers())
    base = timer[: -len(".timer")]
    assert listed.task_name == base
    controller = SystemdScheduledTaskController()
    controller.run(listed.task_name)
    controller.stop(listed.task_name)
    controller.enable(listed.task_name)
    controller.disable(listed.task_name)
    service, armed = f"{base}.service", f"{base}.timer"
    assert conn.calls == [
        ("StartUnit", (service, "replace")),
        ("StopUnit", (service, "replace")),
        ("EnableUnitFiles", ([armed], False, False)),
        ("Reload", ()),
        ("StartUnit", (armed, "replace")),
        ("StopUnit", (armed, "replace")),
        ("DisableUnitFiles", ([armed], False)),
        ("Reload", ()),
    ]


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("task_path", "expected"),
    [
        (r"\tasks\backup.timer", "backup"),
        ("/tasks/backup", "backup"),
        (r"\tasks\x2dname", "x2dname"),
        (r"tasks\x2dname", r"tasks\x2dname"),
        (r"/tasks/a\x2dname", r"a\x2dname"),
    ],
    ids=["windows-folder", "posix-folder", "windows-folder-leaf-like-escape", "listed-escape", "escape-after-slash"],
)
def test_a_folder_is_dropped_at_a_slash_or_a_backslash_that_starts_no_escape(task_path: str, expected: str) -> None:
    """[X22 T1]: a listed name keeps systemd's ``\\xNN`` escape; a backslash starting none is a folder.
    [X23 R2]: a path starting with a backslash is the Windows form ``\\Folder\\Name``, where every
    backslash ends a folder, so a leaf that looks like an escape (``x2dname``) is the name, as in 1.5.2."""
    assert base_name(task_path) == expected


@pytest.mark.os_agnostic
def test_register_still_refuses_an_escaped_name() -> None:
    """[X22 T1]: register and unregister keep the strict grammar: an escape is no name pwshpy writes.
    [X23 C1]: the refusal says how the backslash was read."""
    with pytest.raises(ConfigurationError, match=r"not a systemd unit name.*starts a systemd \\xNN escape"):
        unit_base(r"backup\x2dnightly")


@pytest.mark.os_agnostic
def test_a_refused_windows_form_name_says_every_backslash_ends_a_folder() -> None:
    """[X23 C1]: a refused name from a path starting with a backslash says it was read as a folder path."""
    with pytest.raises(ConfigurationError, match=r"'my job' is not a systemd unit name.*every backslash ends a folder"):
        unit_base("\\Tasks\\my job")


_WINDOWS_FORM_LEAF_LIKE_AN_ESCAPE = pytest.mark.parametrize(
    ("task_path", "base"),
    [(r"\Tasks\xfer", "xfer"), (r"\Jobs\xbackup", "xbackup"), (r"\Maintenance\xfer_logs", "xfer_logs")],
    ids=["xfer", "xbackup", "xfer_logs"],
)


@pytest.mark.os_agnostic
@_WINDOWS_FORM_LEAF_LIKE_AN_ESCAPE
def test_a_windows_form_path_whose_leaf_starts_like_an_escape_registers_its_leaf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, task_path: str, base: str
) -> None:
    """[X23 R2]: X22 read ``\\Tasks\\xfer``'s last backslash as the start of a ``\\xfe`` escape and
    refused the name; 1.5.2 registered the leaf, and so does 1.6 again."""
    units = frozenset({f"{base}.service", f"{base}.timer"})
    conn = FakeConn([], _job_props(base, user=""), frozenset({f"{base}.timer"}), pending=units)
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    SystemdScheduledTaskController().register(task_path, program="/bin/true", triggers=[OnceTrigger(at=_AT)])
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(units)


@pytest.mark.os_agnostic
@_WINDOWS_FORM_LEAF_LIKE_AN_ESCAPE
def test_a_windows_form_path_whose_leaf_starts_like_an_escape_is_addressed_by_its_leaf(
    monkeypatch: pytest.MonkeyPatch, task_path: str, base: str
) -> None:
    """[X23 R2]: X22 sent run, stop, enable and disable on ``\\Maintenance\\xfer_logs`` to
    ``Maintenance\\xfer_logs.service``; each now reaches the leaf's units."""
    conn = _instance_conn(f"{base}.timer")
    use_bus(monkeypatch, conn)
    controller = SystemdScheduledTaskController()
    controller.run(task_path)
    controller.stop(task_path)
    controller.enable(task_path)
    controller.disable(task_path)
    service, armed = f"{base}.service", f"{base}.timer"
    assert conn.calls == [
        ("StartUnit", (service, "replace")),
        ("StopUnit", (service, "replace")),
        ("EnableUnitFiles", ([armed], False, False)),
        ("Reload", ()),
        ("StartUnit", (armed, "replace")),
        ("StopUnit", (armed, "replace")),
        ("DisableUnitFiles", ([armed], False)),
        ("Reload", ()),
    ]


# --- [X22 M-L1] control characters in the arguments and the description ---------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize("field", ["arguments", "description"])
@pytest.mark.parametrize("char", ["\t", "\x01", "\x1b", "\x7f"], ids=["tab", "soh", "escape", "delete"])
def test_service_unit_refuses_a_control_character_in_the_arguments_and_the_description(field: str, char: str) -> None:
    """[X22 M-L1]: systemd.service(5) allows no control character in a command line, and the
    description is written in the same line format; only line breaks and NUL were refused."""
    values = {"arguments": "", "description": "", field: f"a{char}b"}
    with pytest.raises(ConfigurationError, match=f"{field} must not contain a control character"):
        service_unit("/bin/true", values["arguments"], values["description"])


@pytest.mark.os_agnostic
def test_timer_unit_refuses_a_control_character_in_the_description() -> None:
    """[X22 M-L1]: the timer's description is refused like the service's."""
    with pytest.raises(ConfigurationError, match="description must not contain a control character"):
        timer_unit("job", "d\x1bx", [OnceTrigger(at=_AT)])


# --- [50] iter_timers(task_name=...): one task by name, timer first, then the on-demand service ----------

#: The listing calls a by-name lookup must never send: it reads the one task, it walks nothing.
_LISTING_CALLS = frozenset({"ListUnits", "ListUnitFilesByPatterns", "ListUnitFiles"})


def _write_owned(directory: Path, *units: str) -> None:
    """Write each unit as the file pwshpy's register writes (its [Unit] carries the ownership key)."""
    for unit in units:
        (directory / unit).write_text(service_unit("/bin/true", "", "pwshpy task"), encoding="utf-8")


def _lookup_conn(
    *,
    fragments: Path | None = None,
    extra_props: dict[tuple[str, str, str], Any] | None = None,
    files: frozenset[str] = frozenset({"job.timer", "job.service", "adhoc.service"}),
    **kwargs: Any,
) -> FakeConn:
    """A box with a disabled timer task ``job`` (unloaded) and an on-demand service ``adhoc``.

    The listing would show ``job`` only: ``adhoc`` has no ``.timer``. With ``fragments`` each
    service's ``FragmentPath`` (its unit file) is ``fragments/<unit>``; the caller writes the file
    (:func:`_write_owned` for one pwshpy wrote). ``extra_props`` adds or overrides properties,
    ``files`` are the units with a unit file (GetUnitFileState fails for the others), and the
    remaining keyword arguments override the FakeConn defaults (file states, refused loads).
    """
    fragment_props: dict[tuple[str, str, str], Any] = (
        {}
        if fragments is None
        else {
            (unit, systemd_dbus.UNIT_IFACE, "FragmentPath"): str(fragments / unit)
            for unit in ("job.service", "adhoc.service")
        }
    )
    props: dict[tuple[str, str, str], Any] = {
        **fragment_props,
        ("job.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "inactive",
        ("job.timer", systemd_dbus.UNIT_IFACE, "Description"): "Nightly job",
        ("job.timer", _TIMER, "NextElapseUSecRealtime"): _T1,
        ("job.timer", _TIMER, "Unit"): "job.service",
        **_service_props("job.service", code=1, status=0, started=_T0),
        ("job.service", _SERVICE, "User"): "1000",
        ("adhoc.service", systemd_dbus.UNIT_IFACE, "ActiveState"): "inactive",
        ("adhoc.service", systemd_dbus.UNIT_IFACE, "Description"): "Ad hoc",
        **_service_props("adhoc.service", code=1, status=4, started=_T0),
        ("adhoc.service", _SERVICE, "User"): "",
        **(extra_props or {}),
    }
    defaults: dict[str, Any] = {
        "file_states": {"job.timer": "disabled", "job.service": "static", "adhoc.service": "static"},
        "unloaded": frozenset({"job.timer", "job.service", "adhoc.service"}),
    }
    return FakeConn([], props, files, **{**defaults, **kwargs})


@pytest.mark.os_agnostic
@pytest.mark.parametrize("strict", [False, True])
def test_lookup_by_name_reads_the_timer_task_without_listing(monkeypatch: pytest.MonkeyPatch, strict: bool) -> None:
    """[50] A timer task by name: the same record the listing gives, read with no listing call."""
    conn = _lookup_conn()
    use_bus(monkeypatch, conn)
    (job,) = iter_timers(task_name="job", strict=strict)
    assert (job.task_name, job.task_path, job.description, job.user_id) == ("job", "/", "Nightly job", "1000")
    assert (job.enabled, job.state) == (False, TaskState.DISABLED)
    assert job.next_run_time == datetime(2023, 11, 14, 23, 13, 20, tzinfo=timezone.utc)
    assert not _LISTING_CALLS & set(conn.methods)
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_lookup_by_name_finds_an_on_demand_service_the_listing_leaves_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[50] A task with no .timer is found by name although the listing (timers only) omits it."""
    _write_owned(tmp_path, "adhoc.service")
    use_bus(monkeypatch, _lookup_conn(fragments=tmp_path))
    assert "adhoc" not in [t.task_name for t in iter_timers()]
    conn = _lookup_conn(fragments=tmp_path)
    use_bus(monkeypatch, conn)
    (adhoc,) = iter_timers(task_name="adhoc", strict=True)
    assert (adhoc.task_name, adhoc.task_path, adhoc.description, adhoc.user_id) == ("adhoc", "/", "Ad hoc", "0")
    assert (adhoc.enabled, adhoc.next_run_time, adhoc.last_task_result) == (True, None, 4)
    assert adhoc.last_run_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert not _LISTING_CALLS & set(conn.methods)


@pytest.mark.os_agnostic
def test_lookup_by_name_agrees_with_the_controller_read_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[50] The lookup answers what the controller reads back: disable for a timer, register for a service."""
    controller = SystemdScheduledTaskController()
    use_bus(monkeypatch, _lookup_conn())
    disabled = controller.disable("job")
    use_bus(monkeypatch, _lookup_conn())
    assert list(iter_timers(task_name="job")) == [disabled]
    # adhoc.service is unknown to systemd until register writes it and reloads (the ownership check).
    # Its FragmentPath is the file register writes, so the lookup reads the ownership key register wrote.
    use_bus(monkeypatch, _lookup_conn(fragments=tmp_path, pending=frozenset({"adhoc.service"})))
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)  # filesystem edge: never write /etc here
    registered = controller.register("adhoc", program="/bin/true")
    assert list(iter_timers(task_name="adhoc")) == [registered]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("state", sorted(s.value for s in systemd_dbus.UNLISTED_FILE_STATES))
def test_lookup_by_name_treats_a_masked_or_alias_timer_as_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    """[50] A stopped masked or alias .timer is no task, as in the listing: the lookup falls to the service."""
    file_states = {"job.timer": state, "job.service": "static", "adhoc.service": "static"}
    _write_owned(tmp_path, "job.service")
    conn = _lookup_conn(fragments=tmp_path, file_states=file_states)
    use_bus(monkeypatch, conn)
    (job,) = iter_timers(task_name="job", strict=True)
    assert (job.next_run_time, job.description) == (None, "")  # the on-demand record of job.service
    assert "LoadUnit" in conn.methods
    assert conn.calls == []  # no verb: a read


@pytest.mark.os_agnostic
@pytest.mark.parametrize("strict", [False, True])
def test_lookup_by_name_counts_a_lone_service_only_when_pwshpy_wrote_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path, strict: bool
) -> None:
    """[50] USER decision: a .service with no .timer is a task only when its unit file carries X-PwshPy.

    Without the gate every system daemon (ssh, cron, dbus) answered as a running task. A service
    pwshpy did not write is no task: an empty answer, silently, under strict too.
    """
    (tmp_path / "ssh.service").write_text(
        "[Unit]\nDescription=OpenBSD Secure Shell server\n\n[Service]\nExecStart=/usr/sbin/sshd -D\n",
        encoding="utf-8",
    )
    _write_owned(tmp_path, "adhoc.service")
    extra = {
        ("ssh.service", systemd_dbus.UNIT_IFACE, "ActiveState"): "active",
        ("ssh.service", systemd_dbus.UNIT_IFACE, "Description"): "OpenBSD Secure Shell server",
        ("ssh.service", systemd_dbus.UNIT_IFACE, "FragmentPath"): str(tmp_path / "ssh.service"),
    }
    files = frozenset({"job.timer", "job.service", "adhoc.service", "ssh.service"})
    conn = _lookup_conn(fragments=tmp_path, extra_props=extra, files=files)
    use_bus(monkeypatch, conn)
    with caplog.at_level(logging.WARNING, logger=systemd_timers.__name__):
        assert list(iter_timers(task_name="ssh", strict=strict)) == []
        assert [t.task_name for t in iter_timers(task_name="adhoc", strict=strict)] == ["adhoc"]
    assert caplog.records == []


@pytest.mark.os_agnostic
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("unreadable", ["file-read-fails", "fragmentpath-read-refused"])
def test_lookup_by_name_applies_the_strict_rule_to_a_service_file_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path, strict: bool, unreadable: str
) -> None:
    """[50] Ownership that cannot be read is no answer: strict raises naming the unit, lenient warns, empty.

    A directory stands in for an unreadable file: reading it fails for root too (a 0000 mode does not).
    """
    (tmp_path / "adhoc.service").mkdir()
    refused: frozenset[tuple[str, str]] = (
        frozenset({("adhoc.service", "FragmentPath")}) if unreadable == "fragmentpath-read-refused" else frozenset()
    )
    conn = _lookup_conn(fragments=tmp_path, refused_reads=refused)
    use_bus(monkeypatch, conn)
    if strict:
        with pytest.raises(NativeCallError, match=r"'adhoc\.service'"):
            list(iter_timers(task_name="adhoc", strict=True))
        assert conn.closed is True
        return
    with caplog.at_level(logging.WARNING, logger=systemd_timers.__name__):
        assert list(iter_timers(task_name="adhoc")) == []
    (warning,) = caplog.records
    assert "'adhoc.service'" in warning.getMessage()


@pytest.mark.os_agnostic
def test_lookup_by_name_finds_a_masked_timer_while_it_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[50] As in the listing, a masked timer that is still running is listed: the lookup finds the timer."""
    _write_owned(tmp_path, "job.service")
    file_states = {"job.timer": "masked", "job.service": "static", "adhoc.service": "static"}
    running = {("job.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "active"}
    conn = _lookup_conn(
        fragments=tmp_path, extra_props=running, file_states=file_states, load_states={"job.timer": "masked"}
    )
    use_bus(monkeypatch, conn)
    (job,) = iter_timers(task_name="job", strict=True)
    assert (job.description, job.next_run_time) == (
        "Nightly job",
        datetime(2023, 11, 14, 23, 13, 20, tzinfo=timezone.utc),
    )
    assert job.enabled is False  # a masked file is not enabled, as the listing reports it


@pytest.mark.os_agnostic
def test_lookup_by_name_reads_masked_from_load_state_when_the_file_state_read_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[50] GetUnitFileState failing must not let a stopped masked timer through: LoadState says masked."""
    _write_owned(tmp_path, "job.service")
    # job.timer has no listed unit file, so GetUnitFileState fails for it (a refused or broken read).
    files = frozenset({"job.service", "adhoc.service"})
    conn = _lookup_conn(fragments=tmp_path, files=files, load_states={"job.timer": "masked"})
    use_bus(monkeypatch, conn)
    (job,) = iter_timers(task_name="job", strict=True)
    assert (job.next_run_time, job.description) == (None, "")  # the on-demand record of job.service, not the timer


@pytest.mark.os_agnostic
@pytest.mark.parametrize("strict", [False, True])
def test_lookup_by_name_of_an_alias_is_empty_when_the_file_state_read_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path, strict: bool
) -> None:
    """[50] An alias is never reported under its own name: LoadUnit resolves it to the unit it names.

    GetUnitFileState fails for both alias names (no listed unit file), so the file state cannot say
    "alias"; the loaded object's Id (``job.timer``, ``job.service``) differs from the name asked.
    """
    _write_owned(tmp_path, "job.service")
    aliases = {"nightly.timer": "job.timer", "nightly.service": "job.service"}
    conn = _lookup_conn(fragments=tmp_path, aliases=aliases)
    use_bus(monkeypatch, conn)
    with caplog.at_level(logging.WARNING, logger=systemd_timers.__name__):
        assert list(iter_timers(task_name="nightly", strict=strict)) == []
    assert caplog.records == []
    assert conn.methods.count("LoadUnit") == 2  # both names were loaded, then judged by their Id


@pytest.mark.os_agnostic
@pytest.mark.parametrize("strict", [False, True])
def test_lookup_by_name_applies_the_strict_rule_to_an_id_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, strict: bool
) -> None:
    """[50] A refused Id read is no answer: strict raises naming the unit, lenient warns; no service stands in."""
    conn = _lookup_conn(refused_reads=frozenset({("job.timer", "Id")}))
    use_bus(monkeypatch, conn)
    if strict:
        with pytest.raises(NativeCallError, match=r"'job\.timer'"):
            list(iter_timers(task_name="job", strict=True))
        assert conn.closed is True
        return
    with caplog.at_level(logging.WARNING, logger=systemd_timers.__name__):
        assert list(iter_timers(task_name="job")) == []
    (warning,) = caplog.records
    assert "'job.timer'" in warning.getMessage()


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "knobs",
    [
        {"refused_reads": frozenset({("job.timer", "Description")})},
        {"denied_calls": frozenset({("GetUnitFileState", "job.timer")})},
    ],
    ids=["description-read-refused", "file-state-refused"],
)
@pytest.mark.parametrize("strict", [False, True])
def test_lookup_by_name_applies_the_strict_rule_to_a_refused_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, knobs: dict[str, Any], strict: bool
) -> None:
    """[250] A read systemd refuses is no answer: strict raises the refusal, lenient warns; no service stands in."""
    conn = _lookup_conn(**knobs)
    use_bus(monkeypatch, conn)
    if strict:
        with pytest.raises(NativeAccessDeniedError, match=r"'job\.timer'"):
            list(iter_timers(task_name="job", strict=True))
        assert conn.closed is True
        return
    with caplog.at_level(logging.WARNING, logger=systemd_timers.__name__):
        assert list(iter_timers(task_name="job")) == []
    (warning,) = caplog.records
    assert "'job.timer'" in warning.getMessage()
    assert ACCESS_DENIED in warning.getMessage()


@pytest.mark.os_agnostic
def test_lookup_by_name_reads_each_load_state_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[50] The LoadState LoadUnit read decides absence too: a second read could fail and default to present."""
    _write_owned(tmp_path, "adhoc.service")
    conn = _lookup_conn(fragments=tmp_path)
    use_bus(monkeypatch, conn)
    assert [t.task_name for t in iter_timers(task_name="job")] == ["job"]
    assert [t.task_name for t in iter_timers(task_name="adhoc")] == ["adhoc"]
    assert conn.reads.count(("job.timer", "LoadState")) == 1
    assert conn.reads.count(("adhoc.service", "LoadState")) == 1


@pytest.mark.os_agnostic
@pytest.mark.parametrize("strict", [False, True])
def test_lookup_by_name_of_no_task_is_empty(monkeypatch: pytest.MonkeyPatch, strict: bool) -> None:
    """[50] A name with neither a .timer nor a .service is an empty answer, under strict too."""
    conn = _lookup_conn()
    use_bus(monkeypatch, conn)
    assert list(iter_timers(task_name="pwshpy-no-such-task", strict=strict)) == []
    assert not _LISTING_CALLS & set(conn.methods)
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_strict_lookup_raises_naming_a_unit_that_cannot_be_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """[50] strict: a refused LoadUnit is no answer, so it raises naming the unit (never the service)."""
    conn = _lookup_conn(refused_loads=frozenset({"job.timer"}))
    use_bus(monkeypatch, conn)
    with pytest.raises(NativeCallError, match=r"'job\.timer'"):
        list(iter_timers(task_name="job", strict=True))
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_lenient_lookup_warns_and_yields_nothing_for_a_unit_that_cannot_be_loaded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[50] Default: the refused timer is skipped with a WARNING; its service does not stand in for it."""
    use_bus(monkeypatch, _lookup_conn(refused_loads=frozenset({"job.timer"})))
    with caplog.at_level(logging.WARNING, logger=systemd_timers.__name__):
        assert list(iter_timers(task_name="job")) == []
    (warning,) = caplog.records
    assert "'job.timer'" in warning.getMessage()


@pytest.mark.os_agnostic
def test_lookup_by_name_reaches_an_escaped_instance_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    """[50] A listed instance name with a systemd \\xNN escape is a name, not a folder: it is found."""
    name = r"zfs-scrub-monthly@my\x2dpool"
    props: dict[tuple[str, str, str], Any] = {
        (f"{name}.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "active",
        (f"{name}.timer", _TIMER, "Unit"): f"{name}.service",
    }
    use_bus(monkeypatch, FakeConn([], props, frozenset({f"{name}.timer"})))
    assert [t.task_name for t in iter_timers(task_name=name, strict=True)] == [name]


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("task_name", "match"),
    [
        ("", "must not be empty"),
        (r"\Maintenance\job", "folder"),
        (r"\job", "folder"),
        (r"tasks\job", "folder"),
        ("tasks/job", "folder"),
        ("/job", "folder"),
        ("bad name with spaces", "not a systemd unit name"),
        ("scrub@", "template"),
    ],
    ids=["empty", "windows-path", "leading-backslash", "backslash", "slash", "leading-slash", "space", "template"],
)
def test_lookup_by_name_refuses_what_names_no_single_task_before_the_bus(
    monkeypatch: pytest.MonkeyPatch, task_name: str, match: str
) -> None:
    """[50] Parity with the Windows lookup: an empty name or a folder part is refused, not stripped.

    ``existing_unit_base`` alone would read ``tasks/job`` as ``job``, so the answer would be about a
    task the caller did not name. Refused before the bus is opened, like an invalid unit name.
    """
    conn = _lookup_conn()
    use_bus(monkeypatch, conn)
    with pytest.raises(ConfigurationError, match=match):
        list(iter_timers(task_name=task_name))
    assert conn.methods == []


# --- [45] TaskSettings: unit keys written on register, refusals, effective values read back

_UINT64_MAX = 2**64 - 1
_UNIT = systemd_dbus.UNIT_IFACE

#: service_unit's output before [45] for these arguments; settings=None / TaskSettings() must keep it.
_PLAIN_SERVICE = (
    f"[Unit]\nDescription=nightly\n{systemd_timers.OWNERSHIP_KEY}={__init__conf__.version}\n\n"
    '[Service]\nType=oneshot\nUser=1000\nExecStart="/usr/bin/backup" --all\n'
)


def _rendered(settings: TaskSettings | None) -> str:
    return service_unit("/usr/bin/backup", "--all", "nightly", user="1000", settings=settings)


def _sections(text: str) -> dict[str, list[str]]:
    """Each [Section] of a unit file with its lines."""
    found: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            found[current] = []
        elif line:
            found[current].append(line)
    return found


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "settings",
    [
        None,
        TaskSettings(),
        TaskSettings(allow_start_if_on_batteries=True),
        TaskSettings(stop_if_going_on_batteries=False),
        TaskSettings(multiple_instances=TaskMultipleInstances.IGNORE_NEW),
        TaskSettings(allow_hard_terminate=True),
        TaskSettings(allow_demand_start=True),
    ],
    ids=["none", "empty", "allow-battery-start", "no-stop-on-battery", "ignore-new", "hard-terminate", "demand-start"],
)
def test_service_unit_writes_nothing_for_unset_or_default_settings(settings: TaskSettings | None) -> None:
    """A None field, and a value systemd already has, add no key: the text is byte-identical to before."""
    assert _rendered(settings) == _PLAIN_SERVICE


@pytest.mark.os_agnostic
def test_service_unit_renders_each_setting_in_its_section() -> None:
    settings = TaskSettings(
        allow_start_if_on_batteries=False,
        stop_if_going_on_batteries=False,
        execution_time_limit=timedelta(minutes=90),
        multiple_instances=TaskMultipleInstances.IGNORE_NEW,
        allow_hard_terminate=False,
        allow_demand_start=False,
    )
    sections = _sections(_rendered(settings))
    assert "ConditionACPower=true" in sections["Unit"]
    assert "RefuseManualStart=yes" in sections["Unit"]
    assert "TimeoutStartSec=5400" in sections["Service"]
    assert "SendSIGKILL=no" in sections["Service"]
    # Never RuntimeMaxSec: it has no effect on Type=oneshot (systemd-analyze verify, 259).
    assert not any(line.startswith("RuntimeMaxSec") for lines in sections.values() for line in lines)
    added = {"ConditionACPower=true", "RefuseManualStart=yes", "TimeoutStartSec=5400", "SendSIGKILL=no"}
    kept = [line for line in _rendered(settings).split("\n") if line not in added]
    assert "\n".join(kept) == _PLAIN_SERVICE  # each key appends to its section; the rest is unchanged


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("limit", "line"),
    [
        (timedelta(0), "TimeoutStartSec=infinity"),
        (timedelta(seconds=2), "TimeoutStartSec=2"),
        (timedelta(days=3), "TimeoutStartSec=259200"),
        (timedelta(seconds=2147483647), "TimeoutStartSec=2147483647"),
    ],
)
def test_service_unit_renders_the_time_limit_in_seconds(limit: timedelta, line: str) -> None:
    """Zero means no limit; systemd reads TimeoutStartSec=0 as infinity too, the word says it."""
    assert _sections(_rendered(TaskSettings(execution_time_limit=limit)))["Service"][-1] == line


@pytest.mark.os_agnostic
def test_service_unit_refuses_a_manual_start_for_allow_demand_start_false() -> None:
    """RefuseManualStart=yes makes StartUnit fail while the timer still starts the service (measured, 259)."""
    text = _rendered(TaskSettings(allow_demand_start=False))
    assert _sections(text)["Unit"][-1] == "RefuseManualStart=yes"
    assert text.replace("RefuseManualStart=yes\n", "") == _PLAIN_SERVICE


_REFUSED_SETTINGS = [
    (TaskSettings(stop_if_going_on_batteries=True), "stop_if_going_on_batteries=True"),
    (TaskSettings(multiple_instances=TaskMultipleInstances.PARALLEL), "multiple_instances=Parallel"),
    (TaskSettings(multiple_instances=TaskMultipleInstances.QUEUE), "multiple_instances=Queue"),
    (TaskSettings(multiple_instances=TaskMultipleInstances.STOP_EXISTING), "multiple_instances=StopExisting"),
]


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("settings", "named"), _REFUSED_SETTINGS)
def test_settings_without_a_systemd_analog_are_refused_naming_the_field(settings: TaskSettings, named: str) -> None:
    with pytest.raises(PlatformUnsupportedError, match=named) as raised:
        check_settings(settings, ())
    assert "systemd" in str(raised.value)
    with pytest.raises(PlatformUnsupportedError, match=named):  # service_unit never drops one silently
        _rendered(settings)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("triggers", "refused"),
    [
        ([], False),
        ([OnceTrigger(at=_AT)], False),
        ([BootTrigger()], False),
        ([DailyTrigger(at=_AT)], True),
        ([OnceTrigger(at=_AT), DailyTrigger(at=_AT)], True),
    ],
    ids=["on-demand", "once", "boot", "daily", "once-and-daily"],
)
def test_ignore_new_is_refused_only_with_a_daily_trigger(triggers: list[TaskTrigger], refused: bool) -> None:
    """A daily timer whose run overruns fires one catch-up run (measured): that is not IgnoreNew."""
    settings = TaskSettings(multiple_instances=TaskMultipleInstances.IGNORE_NEW)
    if refused:
        with pytest.raises(PlatformUnsupportedError, match="multiple_instances=IgnoreNew"):
            check_settings(settings, triggers)
    else:
        check_settings(settings, triggers)  # accepted, and nothing is written for it
        assert _rendered(settings) == _PLAIN_SERVICE


@pytest.mark.os_agnostic
def test_check_settings_accepts_none_and_every_supported_value() -> None:
    check_settings(None, [DailyTrigger(at=_AT)])
    check_settings(TaskSettings(), [DailyTrigger(at=_AT)])
    check_settings(
        TaskSettings(
            allow_start_if_on_batteries=False,
            stop_if_going_on_batteries=False,
            execution_time_limit=timedelta(0),
            allow_hard_terminate=False,
            allow_demand_start=False,
        ),
        [DailyTrigger(at=_AT)],
    )


_NO_TRIGGERS: list[TaskTrigger] = []
_REFUSED_ON_REGISTER: list[tuple[TaskSettings, list[TaskTrigger]]] = [
    *[(settings, _NO_TRIGGERS) for settings, _named in _REFUSED_SETTINGS],
    (TaskSettings(multiple_instances=TaskMultipleInstances.IGNORE_NEW), [DailyTrigger(at=_AT)]),
]


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("settings", "triggers"), _REFUSED_ON_REGISTER)
def test_register_refuses_a_setting_before_writing_or_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: TaskSettings, triggers: list[TaskTrigger]
) -> None:
    conn = FakeConn([], {})
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    with pytest.raises(PlatformUnsupportedError):
        SystemdScheduledTaskController().register("job", program="/bin/true", triggers=triggers, settings=settings)
    assert list(tmp_path.iterdir()) == []
    assert conn.methods == []


def _settings_props(
    unit: str,
    *,
    conditions: list[tuple[str, bool, bool, str, int]] | None = None,
    timeout_usec: int = _UINT64_MAX,
    send_sigkill: bool = True,
    service_type: str = "oneshot",
    refuse_manual_start: bool = False,
) -> dict[tuple[str, str, str], Any]:
    """The properties a service's settings are read from, as systemd 259 reports them (busctl)."""
    return {
        (unit, _UNIT, "Conditions"): conditions or [],
        (unit, _UNIT, "RefuseManualStart"): refuse_manual_start,
        (unit, _SERVICE, "Type"): service_type,
        (unit, _SERVICE, "TimeoutStartUSec"): timeout_usec,
        (unit, _SERVICE, "SendSIGKILL"): send_sigkill,
    }


_AC_ONLY = ("ConditionACPower", False, False, "true", 0)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("triggers", [[], [OnceTrigger(at=_AT)]], ids=["on-demand", "once"])
@pytest.mark.parametrize("settings", [None, TaskSettings()], ids=["none", "empty"])
def test_register_with_unset_settings_writes_the_same_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, triggers: list[TaskTrigger], settings: TaskSettings | None
) -> None:
    written: list[str] = []
    for arm in ("baseline", "settings"):
        arm_dir = tmp_path / arm
        arm_dir.mkdir()
        conn = FakeConn([], _job_props("job", user=""), frozenset({"job.timer"}), pending=_NEW_JOB)
        use_bus(monkeypatch, conn)
        monkeypatch.setattr(systemd_timers, "_UNIT_DIR", arm_dir)
        controller = SystemdScheduledTaskController()
        if arm == "baseline":
            controller.register("job", program="/bin/true", triggers=triggers)
        else:
            controller.register("job", program="/bin/true", triggers=triggers, settings=settings)
        written.append("".join(p.read_text(encoding="utf-8") for p in sorted(arm_dir.iterdir())))
    assert written[0] == written[1]


@pytest.mark.os_agnostic
def test_register_writes_the_settings_and_reads_back_the_effective_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    props = {
        **_job_props("job", user=""),
        **_settings_props(
            "job.service",
            conditions=[_AC_ONLY],
            timeout_usec=7_200_000_000,
            send_sigkill=False,
            refuse_manual_start=True,
        ),
    }
    conn = FakeConn([], props, frozenset({"job.timer"}), pending=_NEW_JOB)
    use_bus(monkeypatch, conn)
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)
    settings = TaskSettings(
        allow_start_if_on_batteries=False,
        stop_if_going_on_batteries=False,
        execution_time_limit=timedelta(hours=2),
        multiple_instances=TaskMultipleInstances.IGNORE_NEW,
        allow_hard_terminate=False,
        allow_demand_start=False,
    )
    info = SystemdScheduledTaskController().register(
        "job", program="/bin/true", triggers=[OnceTrigger(at=_AT)], settings=settings
    )
    service = _sections((tmp_path / "job.service").read_text(encoding="utf-8"))
    assert {"ConditionACPower=true", "RefuseManualStart=yes"} <= set(service["Unit"])
    assert {"TimeoutStartSec=7200", "SendSIGKILL=no"} <= set(service["Service"])
    timer = (tmp_path / "job.timer").read_text(encoding="utf-8")
    assert "ConditionACPower" not in timer  # a timer condition gates arming, not each run
    assert "RefuseManualStart" not in timer  # the timer itself must stay startable: register starts it
    assert info.settings == TaskSettings(
        allow_start_if_on_batteries=False,
        stop_if_going_on_batteries=False,
        execution_time_limit=timedelta(hours=2),
        multiple_instances=None,  # a repeating timer's catch-up run is none of the four policies
        allow_hard_terminate=False,
        allow_demand_start=False,
    )


@pytest.mark.os_agnostic
def test_listing_reads_the_effective_settings_of_each_timer_task(monkeypatch: pytest.MonkeyPatch) -> None:
    props: dict[tuple[str, str, str], Any] = {
        ("limited.timer", _TIMER, "Unit"): "limited.service",
        **_settings_props(
            "limited.service",
            conditions=[_AC_ONLY],
            timeout_usec=30_000_000,
            send_sigkill=False,
            refuse_manual_start=True,
        ),
        ("plain.timer", _TIMER, "Unit"): "plain.service",
        **_settings_props("plain.service"),  # unset: TimeoutStartUSec reads UINT64_MAX on a oneshot
    }
    units = [("limited.timer", "", "active"), ("plain.timer", "", "active")]
    conn = FakeConn(units, props, frozenset({"limited.timer", "plain.timer"}))
    use_bus(monkeypatch, conn)
    limited, plain = list(iter_timers())
    assert limited.settings == TaskSettings(
        allow_start_if_on_batteries=False,
        stop_if_going_on_batteries=False,
        execution_time_limit=timedelta(seconds=30),
        allow_hard_terminate=False,
        allow_demand_start=False,
    )
    assert plain.settings == TaskSettings(
        allow_start_if_on_batteries=True,
        stop_if_going_on_batteries=False,
        execution_time_limit=timedelta(0),  # UINT64_MAX: no limit
        allow_hard_terminate=True,
        allow_demand_start=True,
    )


@pytest.mark.os_agnostic
def test_listing_cost_of_the_settings_is_five_property_reads_per_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """[45] Risks: the settings add Conditions, RefuseManualStart, Type, TimeoutStartUSec and SendSIGKILL.

    One Get each. A timer task cost 10 calls before (GetUnitFileState, the service's LoadUnit and 8
    Gets); now 15.
    """
    props = {("job.timer", _TIMER, "Unit"): "job.service", **_settings_props("job.service")}
    conn = FakeConn([("job.timer", "", "active")], props, frozenset({"job.timer"}))
    use_bus(monkeypatch, conn)
    list(iter_timers())
    service_reads = sorted(prop for unit, prop in conn.reads if unit == "job.service")
    assert {"Conditions", "RefuseManualStart", "Type", "TimeoutStartUSec", "SendSIGKILL"} <= set(service_reads)
    per_task = [m for m in conn.methods if m not in _LISTING_CALLS]
    assert len(per_task) == 15
    assert per_task.count("Get") == 13


@pytest.mark.os_agnostic
@pytest.mark.parametrize("refused", ["Conditions", "RefuseManualStart", "Type", "TimeoutStartUSec", "SendSIGKILL"])
def test_an_unreadable_settings_property_unknowns_only_its_field(monkeypatch: pytest.MonkeyPatch, refused: str) -> None:
    """A settings read that fails for another reason than a refusal leaves only its field None.

    A refused read is no unknown value: it skips the task ([250],
    ``test_lenient_skips_a_timer_systemd_refuses_to_read_and_warns``).
    """
    props = {
        ("job.timer", _TIMER, "Unit"): "job.service",
        **_settings_props(
            "job.service",
            conditions=[_AC_ONLY],
            timeout_usec=5_000_000,
            send_sigkill=False,
            refuse_manual_start=True,
        ),
    }
    conn = FakeConn(
        [("job.timer", "", "active")],
        props,
        frozenset({"job.timer"}),
        broken_reads=frozenset({("job.service", refused)}),
    )
    use_bus(monkeypatch, conn)
    (task,) = iter_timers(strict=True)  # strict too: a property read stays soft, the task is kept
    expected = TaskSettings(
        allow_start_if_on_batteries=False,
        stop_if_going_on_batteries=False,
        execution_time_limit=timedelta(seconds=5),
        allow_hard_terminate=False,
        allow_demand_start=False,
    )
    unknown = {
        "Conditions": "allow_start_if_on_batteries",
        "RefuseManualStart": "allow_demand_start",
        "Type": "execution_time_limit",
        "TimeoutStartUSec": "execution_time_limit",
        "SendSIGKILL": "allow_hard_terminate",
    }[refused]
    assert task.settings == expected.model_copy(update={unknown: None})


@pytest.mark.os_agnostic
def test_a_service_that_cannot_be_read_reports_every_read_setting_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timer whose service has no unit file: nothing is read, only systemd's battery fact stands."""
    conn = FakeConn(
        [("orphan.timer", "", "active")],
        {("orphan.timer", _TIMER, "Unit"): "gone.service"},
        frozenset({"orphan.timer"}),
    )
    use_bus(monkeypatch, conn)
    (task,) = iter_timers()
    assert task.settings == TaskSettings.model_construct(
        allow_start_if_on_batteries=None,
        stop_if_going_on_batteries=False,
        execution_time_limit=None,
        multiple_instances=None,
        allow_hard_terminate=None,
    )


@pytest.mark.os_agnostic
def test_lookup_of_an_on_demand_task_reads_its_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_owned(tmp_path, "adhoc.service")
    extra = _settings_props("adhoc.service", timeout_usec=_UINT64_MAX, send_sigkill=False)
    use_bus(monkeypatch, _lookup_conn(fragments=tmp_path, extra_props=extra))
    (task,) = iter_timers(task_name="adhoc")
    assert task.settings == TaskSettings(
        allow_start_if_on_batteries=True,
        stop_if_going_on_batteries=False,
        execution_time_limit=timedelta(0),
        allow_hard_terminate=False,
        allow_demand_start=True,
    )


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("conditions", "allowed"),
    [
        ([], True),
        ([_AC_ONLY], False),
        ([("ConditionPathExists", False, False, "/x", 0), _AC_ONLY], False),
        ([("ConditionPathExists", False, False, "/x", 0)], True),
        ([("ConditionACPower", False, True, "true", 0)], True),  # ConditionACPower=!true: battery only
        ([("ConditionACPower", True, False, "true", 0)], True),  # |ConditionACPower=true: one of several
        ([("ConditionACPower", False, False, "false", 0)], True),  # battery only
        ([("ConditionACPower", False, False, "true", 1)], False),  # the state field is not read
        ([("ConditionACPower", False, False, "yes", 0)], False),  # systemd's own boolean spellings
        ([("ConditionACPower", False, False, "ON", 0)], False),
        ([("ConditionACPower", False, False, "1", 0)], False),
        ([("ConditionACPower", False, False, "bogus", 0)], True),
        (None, None),  # unreadable
        ("true", None),  # not an a(sbbsi)
        ([("ConditionACPower", False, False)], None),
        ([("ConditionACPower", 0, 0, "true", 0)], None),  # not booleans
    ],
)
def test_battery_start_allowed_reads_only_the_configured_ac_power_condition(
    conditions: Any, allowed: bool | None
) -> None:
    assert battery_start_allowed(conditions) is allowed


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("service_type", "usec", "limit"),
    [
        ("oneshot", _UINT64_MAX, timedelta(0)),
        ("oneshot", 2_000_000, timedelta(seconds=2)),
        ("oneshot", 2147483647 * 1_000_000, timedelta(seconds=2147483647)),
        ("oneshot", 1_500_000, timedelta(seconds=1, microseconds=500_000)),  # what systemd holds, unrounded
        ("oneshot", None, None),
        ("oneshot", True, None),
        ("oneshot", "2s", None),
        ("simple", 90_000_000, None),  # TimeoutStartSec bounds only the start of a non-oneshot
        ("exec", _UINT64_MAX, None),
        (None, 2_000_000, None),
    ],
)
def test_start_time_limit_is_the_oneshot_start_timeout(service_type: Any, usec: Any, limit: timedelta | None) -> None:
    assert start_time_limit(service_type, usec) == limit


@pytest.mark.os_agnostic
@pytest.mark.parametrize("send_sigkill", ["no", 0, 1], ids=["string", "int-zero", "int-one"])
def test_a_send_sigkill_that_is_no_boolean_reads_unknown(monkeypatch: pytest.MonkeyPatch, send_sigkill: Any) -> None:
    """Only a D-Bus ``b`` has a reading; anything else is unknown, never coerced to True or False."""
    props = {("job.timer", _TIMER, "Unit"): "job.service", **_settings_props("job.service")}
    props["job.service", _SERVICE, "SendSIGKILL"] = send_sigkill
    use_bus(monkeypatch, FakeConn([("job.timer", "", "active")], props, frozenset({"job.timer"})))
    (task,) = iter_timers()
    assert task.settings is not None
    assert task.settings.allow_hard_terminate is None
    assert task.settings.execution_time_limit == timedelta(0)  # the other fields still read


@pytest.mark.os_agnostic
@pytest.mark.parametrize("refuse", ["yes", 0, 1, None], ids=["string", "int-zero", "int-one", "null"])
def test_a_refuse_manual_start_that_is_no_boolean_reads_unknown(monkeypatch: pytest.MonkeyPatch, refuse: Any) -> None:
    """Only a D-Bus ``b`` has a reading; anything else leaves allow_demand_start unknown."""
    props = {("job.timer", _TIMER, "Unit"): "job.service", **_settings_props("job.service")}
    props["job.service", _UNIT, "RefuseManualStart"] = refuse
    use_bus(monkeypatch, FakeConn([("job.timer", "", "active")], props, frozenset({"job.timer"})))
    (task,) = iter_timers()
    assert task.settings is not None
    assert task.settings.allow_demand_start is None
    assert task.settings.allow_hard_terminate is True  # the other fields still read


#: systemd 259's answer to StartUnit on a RefuseManualStart=yes service (measured with busctl).
_MANUAL_START_REFUSED = (
    "Operation refused, unit job.service may be requested by dependency only "
    "(it is configured to refuse manual start/stop)."
)


class _RefusingStartConn(FakeConn):
    """A bus whose StartUnit of the service is refused the way systemd refuses a RefuseManualStart unit."""

    def _verb(self, msg: Message) -> Reply:
        if msg.method == "StartUnit" and msg.body[0] == "job.service":
            self.verbs.append(msg.method)
            return Reply((_MANUAL_START_REFUSED,), error=True)
        return super()._verb(msg)


@pytest.mark.os_agnostic
def test_running_a_task_that_refuses_a_demand_start_raises_naming_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """start_scheduled_task on an allow_demand_start=False task fails as NativeCallError with systemd's reason."""
    conn = _RefusingStartConn([], {}, frozenset({"job.service"}))
    use_bus(monkeypatch, conn)
    with pytest.raises(NativeCallError, match="refuse manual start"):
        SystemdScheduledTaskController().run("job")
    assert conn.verbs == ["StartUnit"]
