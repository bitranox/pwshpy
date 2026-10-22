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
from systemd_fake_bus import Address, FakeConn, FakeCore, use_bus

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
    unit_base,
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
    [
        ("enabled", True),
        ("enabled-runtime", True),
        ("static", True),  # no [Install]: cannot be disabled, and it runs (systemd-tmpfiles-clean)
        ("disabled", False),
        ("masked", False),
    ],
)
def test_timer_enabled_maps(unit_file_state: str, enabled: bool) -> None:
    """timer_enabled is True for the enabled UnitFileStates and for a static timer."""
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
def test_enable_does_not_start_a_timer_it_cannot_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreadable TimersMonotonic: fail safe toward not running the job; it still arms at the next boot."""
    conn = FakeConn([], _enable_props("odd", None), frozenset({"odd.timer"}))
    use_bus(monkeypatch, conn)
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


@pytest.mark.os_agnostic
def test_load_unit_raises_on_an_unreadable_load_state_and_load_unit_path_stays_fail_soft() -> None:
    """A failed LoadState read is "could not ask", not "not-found"; the fail-soft wrapper still reads None.

    load_unit_path's callers (field read-backs, the controllers, the services listing) keep their
    behaviour: before, the failed read defaulted to not-found and they got None too.
    """
    conn = FakeConn(
        [],
        {("real.service", _SERVICE, "User"): "1000"},
        refused_reads=frozenset({("real.service", "LoadState")}),
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
    assert tasks[0].state is timer_to_task_state(active_state)


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


@pytest.mark.os_agnostic
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
    ids=["bare-template", "template-timer", "empty-prefix", "space-in-prefix", "space-in-instance", "percent-in-instance"],
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
def test_a_listed_escaped_timer_is_addressed_by_its_exact_unit_name(monkeypatch: pytest.MonkeyPatch, timer: str) -> None:
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
    [(r"\tasks\backup.timer", "backup"), ("/tasks/backup", "backup"), (r"\tasks\x2dname", r"tasks\x2dname")],
    ids=["windows-folder", "posix-folder", "escape-after-a-folder"],
)
def test_a_folder_is_dropped_at_a_slash_or_a_backslash_that_starts_no_escape(task_path: str, expected: str) -> None:
    """[X22 T1]: the Windows folder form ``\\Folder\\Name`` stays a folder; only ``\\xNN`` is kept."""
    assert base_name(task_path) == expected


@pytest.mark.os_agnostic
def test_register_still_refuses_an_escaped_name() -> None:
    """[X22 T1]: register and unregister keep the strict grammar: an escape is no name pwshpy writes."""
    with pytest.raises(ConfigurationError, match="not a systemd unit name"):
        unit_base(r"backup\x2dnightly")


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
