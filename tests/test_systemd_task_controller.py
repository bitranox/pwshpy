"""Live systemd scheduled-task controller test - on SCRATCH units, root only.

Two tasks, two mechanisms:
  * an on-demand ``.service`` created by ``register`` (run/stop/unregister), and
  * a scratch ``.timer`` (enable arms it, disable disarms it).
Neither touches a real task. local_only + Linux + root, so it runs on the dev box, never in CI.
"""
# subprocess writes/reloads the scratch .timer unit files and is the belt-and-braces teardown.

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pwshpy.adapters.native.systemd_timers import SystemdScheduledTaskController, iter_timers
from pwshpy.composition import build_ps
from pwshpy.domain.enums import TaskMultipleInstances, TaskState
from pwshpy.domain.errors import NativeCallError, PlatformUnsupportedError
from pwshpy.domain.scheduling import BootTrigger, DailyTrigger, OnceTrigger, TaskSettings, TaskTrigger

_UNIT_DIR = Path("/etc/systemd/system")
_ONDEMAND = "pwshpy-pytest-task"
_TIMER = "pwshpy-pytest-timer"
_TIMER_SERVICE_UNIT = "[Unit]\nDescription=pwshpy pytest timer job\n[Service]\nType=oneshot\nExecStart=/bin/true\n"
_TIMER_UNIT = (
    "[Unit]\nDescription=pwshpy pytest timer\n[Timer]\nOnCalendar=*-*-* 03:00:00\n[Install]\nWantedBy=timers.target\n"
)

pytestmark = [
    pytest.mark.local_only,
    pytest.mark.mutating,
    pytest.mark.os_linux,
    pytest.mark.skipif(
        not sys.platform.startswith("linux") or os.geteuid() != 0,  # type: ignore[attr-defined]
        reason="mutating systemd task controller needs Linux + root",
    ),
]


def _reload() -> None:
    subprocess.run(["systemctl", "daemon-reload"], check=False)  # noqa: S607 - fixed argv


def _reset_failed(name: str) -> None:
    """Forget a failed state of the scratch units.

    systemd keeps a unit that failed (a run killed by its time limit, say) listed as ``failed``
    even after its file is gone, until ``reset-failed``; without it the next run of this module
    starts beside a stale failed unit of the same name.
    """
    subprocess.run(  # noqa: S603 - fixed argv plus scratch unit names
        ["systemctl", "reset-failed", f"{name}.service", f"{name}.timer"],  # noqa: S607
        check=False,
        capture_output=True,
    )


@pytest.fixture
def ondemand_task() -> Iterator[str]:
    """Yield a task name; on teardown disarm and remove any .timer and the .service, then reload."""
    try:
        yield _ONDEMAND
    finally:
        subprocess.run(["systemctl", "stop", f"{_ONDEMAND}.timer"], check=False)  # noqa: S603, S607
        subprocess.run(["systemctl", "disable", f"{_ONDEMAND}.timer"], check=False)  # noqa: S603, S607
        (_UNIT_DIR / f"{_ONDEMAND}.timer").unlink(missing_ok=True)
        (_UNIT_DIR / f"{_ONDEMAND}.service").unlink(missing_ok=True)
        _reload()
        _reset_failed(_ONDEMAND)


@pytest.fixture
def timer_task() -> Iterator[str]:
    """Install a scratch .timer + .service pair; on teardown stop, disable and remove them."""
    (_UNIT_DIR / f"{_TIMER}.service").write_text(_TIMER_SERVICE_UNIT)
    (_UNIT_DIR / f"{_TIMER}.timer").write_text(_TIMER_UNIT)
    _reload()
    try:
        yield _TIMER
    finally:
        subprocess.run(["systemctl", "stop", f"{_TIMER}.timer"], check=False)  # noqa: S603, S607
        subprocess.run(["systemctl", "disable", f"{_TIMER}.timer"], check=False)  # noqa: S603, S607
        (_UNIT_DIR / f"{_TIMER}.timer").unlink(missing_ok=True)
        (_UNIT_DIR / f"{_TIMER}.service").unlink(missing_ok=True)
        _reload()
        _reset_failed(_TIMER)


def test_register_run_unregister_ondemand(ondemand_task: str) -> None:
    """register -> Ready+enabled, run (oneshot exits), enable/disable rejected, unregister -> gone."""
    controller = SystemdScheduledTaskController()

    registered = controller.register(ondemand_task, program="/bin/true", description="pwshpy pytest task")
    assert registered.task_name == ondemand_task
    assert registered.state is TaskState.READY
    assert registered.enabled is True
    assert (_UNIT_DIR / f"{ondemand_task}.service").exists()

    controller.run(ondemand_task)  # oneshot /bin/true starts and exits; no exception

    # an on-demand task has no schedule, so enable/disable are a clear error, not a silent no-op
    with pytest.raises(NativeCallError, match="on-demand"):
        controller.disable(ondemand_task)

    controller.unregister(ondemand_task)
    assert not (_UNIT_DIR / f"{ondemand_task}.service").exists()


def test_enable_disable_timer(timer_task: str) -> None:
    """disable disarms the .timer (enabled False), enable arms it (enabled True, Ready)."""
    controller = SystemdScheduledTaskController()

    enabled = controller.enable(timer_task)
    assert enabled.enabled is True
    assert enabled.state is TaskState.READY  # armed, waiting to fire

    disabled = controller.disable(timer_task)
    assert disabled.enabled is False
    # A stopped, disabled timer is unloaded: the read-back and the listing must still reach it.
    assert (disabled.description, disabled.user_id) == ("pwshpy pytest timer", "0")
    listed = [t for t in iter_timers() if t.task_name == timer_task]
    assert [(t.enabled, t.description) for t in listed] == [(False, "pwshpy pytest timer")]


def test_register_with_boot_trigger_writes_and_arms_a_timer(ondemand_task: str) -> None:
    """A boot trigger becomes an enabled .timer (armed for the next boot), and unregister removes it.

    Driven through the production facade, so it also proves build_ps() wires the Linux
    controller and the facade forwards the triggers.
    """
    facade = build_ps()
    info = facade.register_scheduled_task(
        ondemand_task, program="/bin/true", triggers=[BootTrigger(delay=timedelta(seconds=600))]
    )
    assert info.enabled is True
    assert info.state is TaskState.READY  # armed for the next boot
    assert info.last_run_time is None  # the elapsed OnBootSec= did not run the job at registration
    assert info.user_id == "0"  # no principal: a system unit with no User= runs as root
    assert info.description == "pwshpy task"  # read from the unloaded timer through LoadUnit
    assert [t.task_name for t in iter_timers() if t.task_name == ondemand_task] == [ondemand_task]
    assert (_UNIT_DIR / f"{ondemand_task}.timer").read_text(encoding="utf-8").count("OnBootSec=600s") == 1
    facade.unregister_scheduled_task(ondemand_task)
    assert not (_UNIT_DIR / f"{ondemand_task}.timer").exists()
    assert not (_UNIT_DIR / "timers.target.wants" / f"{ondemand_task}.timer").exists()


def test_a_unit_pwshpy_did_not_write_is_never_overwritten_or_deleted(ondemand_task: str) -> None:
    """[C1] on real systemd: an /etc unit without the ownership key survives register and unregister."""
    controller = SystemdScheduledTaskController()
    path = _UNIT_DIR / f"{ondemand_task}.service"
    path.write_text(_TIMER_SERVICE_UNIT)
    _reload()
    with pytest.raises(NativeCallError, match="not written by pwshpy"):
        controller.register(ondemand_task, program="/bin/true")
    with pytest.raises(NativeCallError, match="not written by pwshpy"):
        controller.unregister(ondemand_task)
    assert path.read_text() == _TIMER_SERVICE_UNIT


def test_a_unit_systemd_already_has_is_never_shadowed() -> None:
    """[C1] on real systemd: a vendor unit (here systemd's own) is refused before /etc is written."""
    controller = SystemdScheduledTaskController()
    with pytest.raises(NativeCallError, match="not written by pwshpy"):
        controller.register("systemd-journald", program="/bin/true")
    assert not (_UNIT_DIR / "systemd-journald.service").exists()
    with pytest.raises(NativeCallError, match="no such task"):
        controller.unregister("systemd-journald")


def test_systemd_loads_the_ownership_key_without_a_warning(ondemand_task: str) -> None:
    """[C1] on real systemd: X-PwshPy= in [Unit] is ignored silently (an unknown key without X- warns)."""
    SystemdScheduledTaskController().register(ondemand_task, program="/bin/true")
    path = _UNIT_DIR / f"{ondemand_task}.service"
    verify = subprocess.run(  # noqa: S603 - fixed argv plus the unit pwshpy just wrote
        ["systemd-analyze", "verify", "--man=no", str(path)],  # noqa: S607
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert "X-PwshPy" not in verify.stdout + verify.stderr, verify.stdout + verify.stderr
    assert "X-PwshPy=" in path.read_text(encoding="utf-8")


def test_lookup_by_name_reads_a_timer_task(ondemand_task: str) -> None:
    """[50] A task with a daily trigger is found by name as its timer: one record, a next run set."""
    at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    SystemdScheduledTaskController().register(ondemand_task, program="/bin/true", triggers=[DailyTrigger(at=at)])
    (found,) = iter_timers(task_name=ondemand_task, strict=True)
    assert found.task_name == ondemand_task
    assert found.next_run_time is not None


def test_lookup_by_name_reads_an_on_demand_task_the_listing_leaves_out(ondemand_task: str) -> None:
    """[50] A task with no trigger is found by name, although the listing (timers only) omits it."""
    SystemdScheduledTaskController().register(ondemand_task, program="/bin/true", description="pwshpy pytest task")
    (found,) = iter_timers(task_name=ondemand_task, strict=True)
    assert (found.task_name, found.enabled, found.next_run_time) == (ondemand_task, True, None)
    assert found.description == "pwshpy pytest task"
    assert ondemand_task not in [t.task_name for t in iter_timers()]


#: System daemons a systemd host commonly runs as a .service with no .timer; none is a pwshpy task.
_SYSTEM_SERVICES = ("ssh", "cron", "dbus", "systemd-journald")


def _loaded_system_services() -> list[str]:
    """The :data:`_SYSTEM_SERVICES` this host has loaded (``systemctl show`` reads LoadState=loaded)."""
    loaded: list[str] = []
    for name in _SYSTEM_SERVICES:
        shown = subprocess.run(  # noqa: S603 - fixed argv
            ["systemctl", "show", "-p", "LoadState", "--value", f"{name}.service"],  # noqa: S607
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if shown.stdout.strip() == "loaded":
            loaded.append(name)
    return loaded


def test_lookup_by_name_skips_a_system_service_but_finds_a_pwshpy_on_demand_task(ondemand_task: str) -> None:
    """[50] USER decision: a lone .service is a task only when pwshpy wrote it, so no daemon answers as one."""
    SystemdScheduledTaskController().register(ondemand_task, program="/bin/true")
    services = _loaded_system_services()
    assert services, f"none of {_SYSTEM_SERVICES} is loaded here, so the gate is not exercised"
    for name in services:
        assert list(iter_timers(task_name=name, strict=True)) == [], name
    assert [t.task_name for t in iter_timers(task_name=ondemand_task, strict=True)] == [ondemand_task]


def test_lookup_by_name_of_no_task_is_empty() -> None:
    """[50] On real systemd, a name with no unit file is an empty answer (LoadUnit's not-found), strict too."""
    assert list(iter_timers(task_name="pwshpy-pytest-no-such-task", strict=True)) == []


# --- [45] TaskSettings on real systemd -----------------------------------------

#: The unit keys a TaskSettings can add; any other line of the unit is pwshpy's usual text.
_SETTINGS_KEYS = ("ConditionACPower=", "RefuseManualStart=", "TimeoutStartSec=", "SendSIGKILL=")
#: What an on-demand task reads back with no key written: systemd's own behaviour. An unset
#: oneshot's TimeoutStartUSec is infinity (no limit); systemd never stops a run on battery and
#: has no multiple-instances policy to report.
_SYSTEMD_DEFAULTS = TaskSettings.model_construct(
    allow_start_if_on_batteries=True,
    stop_if_going_on_batteries=False,
    execution_time_limit=timedelta(0),
    multiple_instances=None,
    allow_hard_terminate=True,
    allow_demand_start=True,
)
#: A marker file the task's program creates, to see from outside that a run happened.
_MARKER = Path("/run/pwshpy-pytest-marker")


def _systemctl(*args: str) -> str:
    """Run ``systemctl`` with fixed arguments; return its stdout."""
    done = subprocess.run(  # noqa: S603 - fixed argv plus scratch unit names
        ["systemctl", *args],  # noqa: S607
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return done.stdout


def _settings_lines(unit: str) -> list[str]:
    """The TaskSettings keys of ``unit`` as systemd itself shows the loaded file (``systemctl cat``).

    A failed ``systemctl cat`` prints nothing, which would read as "no settings written" and let
    an equality with an empty expectation pass; so the call must succeed and show the unit's
    ``ExecStart=`` before its lines count as evidence.
    """
    done = subprocess.run(  # noqa: S603 - fixed argv plus scratch unit names
        ["systemctl", "cat", unit],  # noqa: S607
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert done.returncode == 0, f"systemctl cat {unit} failed: {done.stderr.strip()}"
    lines = done.stdout.splitlines()
    assert any(line.startswith("ExecStart=") for line in lines), f"systemctl cat {unit} shows no ExecStart="
    return [line for line in lines if line.startswith(_SETTINGS_KEYS)]


def _show(unit: str, prop: str) -> str:
    return _systemctl("show", "-p", prop, "--value", unit).strip()


@pytest.fixture
def marker() -> Iterator[Path]:
    """Yield the marker path, absent before and after the test."""
    _MARKER.unlink(missing_ok=True)
    try:
        yield _MARKER
    finally:
        _MARKER.unlink(missing_ok=True)


def _wait_for(predicate: Callable[[], bool], timeout: float) -> float:
    """Poll ``predicate`` until it is true or ``timeout`` seconds pass; return the elapsed time."""
    started = time.monotonic()
    while not predicate() and time.monotonic() - started < timeout:
        time.sleep(0.2)
    return time.monotonic() - started


@pytest.mark.parametrize(
    ("settings", "keys", "effective"),
    [
        pytest.param(None, [], _SYSTEMD_DEFAULTS, id="none"),
        pytest.param(TaskSettings(), [], _SYSTEMD_DEFAULTS, id="all-unset"),
        pytest.param(
            TaskSettings(
                allow_start_if_on_batteries=True,
                stop_if_going_on_batteries=False,
                multiple_instances=TaskMultipleInstances.IGNORE_NEW,
                allow_hard_terminate=True,
                allow_demand_start=True,
            ),
            [],
            _SYSTEMD_DEFAULTS,
            id="systemd-already-behaves-so",
        ),
        pytest.param(
            TaskSettings(
                allow_start_if_on_batteries=False,
                stop_if_going_on_batteries=False,
                execution_time_limit=timedelta(seconds=90),
                multiple_instances=TaskMultipleInstances.IGNORE_NEW,
                allow_hard_terminate=False,
                allow_demand_start=False,
            ),
            ["ConditionACPower=true", "RefuseManualStart=yes", "TimeoutStartSec=90", "SendSIGKILL=no"],
            _SYSTEMD_DEFAULTS.model_copy(
                update={
                    "allow_start_if_on_batteries": False,
                    "execution_time_limit": timedelta(seconds=90),
                    "allow_hard_terminate": False,
                    "allow_demand_start": False,
                }
            ),
            id="every-key",
        ),
        pytest.param(
            TaskSettings(execution_time_limit=timedelta(0)),
            ["TimeoutStartSec=infinity"],
            _SYSTEMD_DEFAULTS,
            id="no-limit",
        ),
    ],
)
def test_register_settings_writes_exactly_their_keys_and_reads_them_back(
    ondemand_task: str, settings: TaskSettings | None, keys: list[str], effective: TaskSettings
) -> None:
    """[45] systemd loads exactly the keys a setting needs (none when unset), and the task reads them back."""
    info = build_ps().register_scheduled_task(ondemand_task, program="/bin/true", settings=settings)
    assert _settings_lines(f"{ondemand_task}.service") == keys
    assert info.settings == effective
    looked_up = build_ps().get_scheduled_task(task_name=ondemand_task, strict=True).first()
    assert looked_up is not None
    assert looked_up.settings == effective


def test_a_start_timeout_ends_a_longer_run(ondemand_task: str) -> None:
    """[45] execution_time_limit=2 s ends a 30 s run after about 2 s, with Result=timeout."""
    controller = SystemdScheduledTaskController()
    controller.register(
        ondemand_task,
        program="/bin/sleep",
        arguments="30",
        settings=TaskSettings(execution_time_limit=timedelta(seconds=2)),
    )
    unit = f"{ondemand_task}.service"
    controller.run(ondemand_task)
    elapsed = _wait_for(lambda: _show(unit, "ActiveState") in {"failed", "inactive"}, 20)
    assert (_show(unit, "ActiveState"), _show(unit, "Result")) == ("failed", "timeout")
    assert elapsed < 10, elapsed  # the sleep alone runs 30 s


def test_a_refused_manual_start_raises_while_the_timer_still_runs_the_task(ondemand_task: str, marker: Path) -> None:
    """[45] allow_demand_start=False: start_scheduled_task raises NativeCallError; the trigger still fires."""
    facade = build_ps()
    at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=8)
    facade.register_scheduled_task(
        ondemand_task,
        program="/usr/bin/touch",
        arguments=str(marker),
        triggers=[OnceTrigger(at=at)],
        settings=TaskSettings(allow_demand_start=False),
    )
    with pytest.raises(NativeCallError, match="refused"):
        facade.start_scheduled_task(ondemand_task)
    assert not marker.exists()  # the refused start ran nothing (the trigger is seconds away)
    _wait_for(marker.exists, 30)
    assert marker.exists(), "the once trigger did not run the task"


def test_a_start_on_ac_power_runs_in_a_host_without_a_battery(ondemand_task: str, marker: Path) -> None:
    """[45] allow_start_if_on_batteries=False (ConditionACPower=true) still runs where no battery is present."""
    controller = SystemdScheduledTaskController()
    controller.register(
        ondemand_task,
        program="/usr/bin/touch",
        arguments=str(marker),
        settings=TaskSettings(allow_start_if_on_batteries=False),
    )
    assert _settings_lines(f"{ondemand_task}.service") == ["ConditionACPower=true"]  # the condition is in force
    controller.run(ondemand_task)
    _wait_for(marker.exists, 15)
    # The marker is the evidence the condition passed: ConditionResult is no witness, as a finished
    # oneshot is unloaded and a fresh load reads it "no" (measured on a throwaway systemd container).
    assert marker.exists()


_IN_AN_HOUR = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)


@pytest.mark.parametrize(
    ("settings", "triggers"),
    [
        pytest.param(TaskSettings(stop_if_going_on_batteries=True), [], id="stop-on-battery"),
        pytest.param(TaskSettings(multiple_instances=TaskMultipleInstances.QUEUE), [], id="queue"),
        pytest.param(TaskSettings(multiple_instances=TaskMultipleInstances.PARALLEL), [], id="parallel"),
        pytest.param(TaskSettings(multiple_instances=TaskMultipleInstances.STOP_EXISTING), [], id="stop-existing"),
        pytest.param(
            TaskSettings(multiple_instances=TaskMultipleInstances.IGNORE_NEW),
            [DailyTrigger(at=_IN_AN_HOUR)],
            id="ignore-new-daily",
        ),
    ],
)
def test_a_setting_without_a_systemd_analog_is_refused_and_writes_nothing(
    ondemand_task: str, settings: TaskSettings, triggers: list[TaskTrigger]
) -> None:
    """[45] A value systemd has no key for raises PlatformUnsupportedError and leaves no unit behind."""
    with pytest.raises(PlatformUnsupportedError):
        build_ps().register_scheduled_task(ondemand_task, program="/bin/true", triggers=triggers, settings=settings)
    for suffix in ("service", "timer"):
        assert not (_UNIT_DIR / f"{ondemand_task}.{suffix}").exists()
        assert _show(f"{ondemand_task}.{suffix}", "LoadState") == "not-found"
