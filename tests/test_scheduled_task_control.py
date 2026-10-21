"""native scheduled-task control (mutating): fake-COM unit tests + real integration.

The unit tests drive :class:`NativeScheduledTaskController` against an in-memory
Task Scheduler COM double (``os_agnostic``): enable/disable toggle the flag, run/stop
call the task, register builds an EXEC action, unregister deletes, and failures wrap.

The ``local_only`` + ``mutating`` test registers a real scratch task, exercises the
full lifecycle, and unregisters it - throwaway VM only.
"""

# The fakes below deliberately mirror the Task Scheduler COM PascalCase API.
# ruff: noqa: N802

from __future__ import annotations

import contextlib
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pwshpy.adapters.native import scheduled_task_control as mod
from pwshpy.adapters.native import task_readback
from pwshpy.adapters.native.scheduled_task_control import NativeScheduledTaskController
from pwshpy.domain.enums import TaskLogonType, TaskRunLevel, WellKnownSid
from pwshpy.domain.errors import ConfigurationError, NativeCallError, PlatformUnsupportedError, PwshPyError
from pwshpy.domain.scheduling import BootTrigger, DailyTrigger, LogonTrigger, OnceTrigger, TaskPrincipal


class _FakeRegInfo:
    def __init__(self) -> None:
        self.Author = ""
        self.Description = ""


class _FakeAction:
    def __init__(self) -> None:
        self.Path = ""
        self.Arguments = ""


class _FakeActions:
    def __init__(self) -> None:
        self.items: list[_FakeAction] = []

    def Create(self, action_type: int) -> _FakeAction:
        action = _FakeAction()
        self.items.append(action)
        return action


class _FakePrincipal:
    def __init__(self) -> None:
        self.UserId = ""
        self.LogonType = 3
        self.RunLevel = 0


class _FakeTrigger:
    def __init__(self, trigger_type: int) -> None:
        self.Type = trigger_type
        self.StartBoundary = ""
        self.DaysInterval = 1
        self.Delay = ""
        self.UserId = ""


class _FakeTriggers:
    def __init__(self) -> None:
        self.items: list[_FakeTrigger] = []

    def Create(self, trigger_type: int) -> _FakeTrigger:
        trigger = _FakeTrigger(trigger_type)
        self.items.append(trigger)
        return trigger


class _FakeSettings:
    def __init__(self) -> None:
        self.Enabled = True
        self.AllowDemandStart = True


class _FakeDefinition:
    def __init__(self) -> None:
        self.RegistrationInfo = _FakeRegInfo()
        self.Actions = _FakeActions()
        self.Settings = _FakeSettings()
        self.Principal = _FakePrincipal()
        self.Triggers = _FakeTriggers()


class _FakeTask:
    def __init__(self, name: str, definition: _FakeDefinition | None = None) -> None:
        self.Name = name
        self.Enabled = True
        self.State = 3
        self.Definition = definition or _FakeDefinition()
        self.actions: list[str] = []
        self.LastRunTime = datetime(1899, 12, 30)  # VT_DATE zero: Task Scheduler's "never ran" (UNVERIFIED, Task 6)
        self.LastTaskResult = 0x41303  # SCHED_S_TASK_HAS_NOT_RUN
        self.NextRunTime = datetime(1899, 12, 30)  # no next run

    def Run(self, params: Any) -> None:
        self.actions.append("run")

    def Stop(self, flags: int) -> None:
        self.actions.append("stop")


class _FakeFolder:
    def __init__(self) -> None:
        self.tasks: dict[str, _FakeTask] = {}
        self.deleted: list[str] = []
        self.registered: list[str] = []
        self.last_user: Any = "<never registered>"
        self.last_logon: int | None = None

    def GetTask(self, name: str) -> _FakeTask:
        return self.tasks[name]

    def DeleteTask(self, name: str, flags: int) -> None:
        self.deleted.append(name)
        self.tasks.pop(name, None)

    def RegisterTaskDefinition(  # noqa: PLR0917 - mirrors the Task Scheduler COM method's positional signature
        self, name: str, definition: _FakeDefinition, flags: int, user: Any, pw: Any, logon: int
    ) -> _FakeTask:
        task = _FakeTask(name, definition)
        self.tasks[name] = task
        self.registered.append(name)
        self.last_user = user
        self.last_logon = logon
        return task


class _FakeService:
    def __init__(self) -> None:
        self.folder = _FakeFolder()

    def GetFolder(self, path: str) -> _FakeFolder:
        return self.folder

    def NewTask(self, flags: int) -> _FakeDefinition:
        return _FakeDefinition()


class _FailingTriggers(_FakeTriggers):
    def Create(self, trigger_type: int) -> _FakeTrigger:
        raise RuntimeError("Triggers.Create failed")


class _TriggerFailingService(_FakeService):
    """A service whose new definitions refuse to create a trigger (a COM fault mid-build)."""

    def NewTask(self, flags: int) -> _FakeDefinition:
        definition = _FakeDefinition()
        definition.Triggers = _FailingTriggers()
        return definition


def _use(monkeypatch: pytest.MonkeyPatch, svc: _FakeService) -> None:
    monkeypatch.setattr(mod, "_connect_scheduler", lambda: svc)


def _sid_as_given(value: str) -> str:
    """Stand-in for sid_string (an external edge: LookupAccountName) that returns its input."""
    return value


@pytest.mark.os_agnostic
def test_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    """enable sets Enabled True and returns the updated record."""
    svc = _FakeService()
    task = _FakeTask("MyTask")
    task.Enabled = False
    svc.folder.tasks["MyTask"] = task
    _use(monkeypatch, svc)
    result = NativeScheduledTaskController().enable("\\MyTask")
    assert task.Enabled is True
    assert result.enabled is True
    assert result.task_name == "MyTask"


@pytest.mark.os_agnostic
def test_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    """disable sets Enabled False."""
    svc = _FakeService()
    svc.folder.tasks["MyTask"] = _FakeTask("MyTask")
    _use(monkeypatch, svc)
    assert NativeScheduledTaskController().disable("\\MyTask").enabled is False


@pytest.mark.os_agnostic
def test_run_and_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """run and stop call the task's Run/Stop."""
    svc = _FakeService()
    task = _FakeTask("MyTask")
    svc.folder.tasks["MyTask"] = task
    _use(monkeypatch, svc)
    controller = NativeScheduledTaskController()
    controller.run("\\MyTask")
    controller.stop("\\MyTask")
    assert task.actions == ["run", "stop"]


@pytest.mark.os_agnostic
def test_unregister(monkeypatch: pytest.MonkeyPatch) -> None:
    """unregister deletes the task from its folder."""
    svc = _FakeService()
    svc.folder.tasks["MyTask"] = _FakeTask("MyTask")
    _use(monkeypatch, svc)
    NativeScheduledTaskController().unregister("\\MyTask")
    assert svc.folder.deleted == ["MyTask"]


@pytest.mark.os_agnostic
def test_register_builds_exec_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """register creates an EXEC action with the program + arguments and returns the task."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    result = NativeScheduledTaskController().register(
        "\\NewTask", program="cmd.exe", arguments="/c exit", description="d"
    )
    assert result.task_name == "NewTask"
    assert svc.folder.registered == ["NewTask"]
    action = svc.folder.tasks["NewTask"].Definition.Actions.items[0]
    assert action.Path == "cmd.exe"
    assert action.Arguments == "/c exit"


@pytest.mark.os_agnostic
def test_missing_task_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A COM failure (missing task) surfaces as NativeCallError."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    with pytest.raises(NativeCallError):
        NativeScheduledTaskController().enable("\\Missing")


@pytest.mark.os_agnostic
def test_task_info_reads_principal_sid_and_run_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task's principal reads back as a SID and its run level as an enum (never a localized name)."""
    svc = _FakeService()
    task = _FakeTask("MyTask")
    task.Definition.Principal.UserId = "S-1-5-18"
    task.Definition.Principal.RunLevel = 1
    svc.folder.tasks["MyTask"] = task
    _use(monkeypatch, svc)
    monkeypatch.setattr(task_readback, "sid_string", _sid_as_given)  # external edge: LookupAccountName
    info = NativeScheduledTaskController().enable("\\MyTask")
    assert info.user_id == "S-1-5-18"
    assert info.run_level is TaskRunLevel.HIGHEST


@pytest.mark.os_agnostic
def test_task_info_resolves_an_account_name_through_sid_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """A principal registered by account name is normalised through sid_string, not passed through."""
    svc = _FakeService()
    task = _FakeTask("MyTask")
    task.Definition.Principal.UserId = "SYSTEM"
    svc.folder.tasks["MyTask"] = task
    _use(monkeypatch, svc)
    monkeypatch.setattr(
        task_readback, "sid_string", {"SYSTEM": "S-1-5-18"}.__getitem__
    )  # external edge: LookupAccountName
    info = NativeScheduledTaskController().enable("\\MyTask")
    assert info.user_id == "S-1-5-18"
    assert info.run_level is TaskRunLevel.LIMITED


@pytest.mark.os_agnostic
def test_task_info_reads_run_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """[120]: last run, last result and next run read back from the IRegisteredTask as aware UTC / int."""
    svc = _FakeService()
    task = _FakeTask("MyTask")
    task.LastRunTime = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    task.LastTaskResult = 0
    task.NextRunTime = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)
    svc.folder.tasks["MyTask"] = task
    _use(monkeypatch, svc)
    monkeypatch.setattr(task_readback, "sid_string", _sid_as_given)  # external edge: LookupAccountName
    info = NativeScheduledTaskController().enable("\\MyTask")
    assert info.last_run_time == datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    assert info.last_task_result == 0  # 0 is a real result ("ran, succeeded"), never folded into None
    assert info.next_run_time == datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)


@pytest.mark.os_agnostic
def test_task_info_never_run_reads_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """[120]: the never-run sentinels (1899-12-30, 0x41303) read back as None, not as a date or a code."""
    svc = _FakeService()
    svc.folder.tasks["MyTask"] = _FakeTask("MyTask")
    _use(monkeypatch, svc)
    monkeypatch.setattr(task_readback, "sid_string", _sid_as_given)
    info = NativeScheduledTaskController().enable("\\MyTask")
    assert (info.last_run_time, info.last_task_result, info.next_run_time) == (None, None, None)


# An account name that resolves nowhere: off Windows sid_string cannot resolve any name
# (no pywin32), on Windows LookupAccountName finds no such account - both raise.
_UNRESOLVABLE = "pwshpy-test-no-such-account-7f3a"


@pytest.mark.os_agnostic
def test_task_info_unresolvable_principal_reads_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verb that succeeded is not turned into a failure by a principal that no longer resolves."""
    svc = _FakeService()
    task = _FakeTask("MyTask")
    task.Definition.Principal.UserId = _UNRESOLVABLE
    task.Definition.Principal.RunLevel = 1
    svc.folder.tasks["MyTask"] = task
    _use(monkeypatch, svc)
    info = NativeScheduledTaskController().enable("\\MyTask")
    assert task.Enabled is True
    assert (info.user_id, info.run_level) == ("", TaskRunLevel.HIGHEST)


_AT = datetime(2026, 10, 21, 23, 59, tzinfo=timezone(timedelta(hours=2)))
_USER_SID = "S-1-5-21-1-2-3-1001"


def _no_com(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any Task Scheduler connection fail the test: a refusal must come before COM."""

    def _refuse() -> Any:
        pytest.fail("register touched COM before refusing its input")

    monkeypatch.setattr(mod, "_connect_scheduler", _refuse)


@pytest.mark.os_agnostic
def test_register_default_principal_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """No principal keeps the interactive-token registration of every release before 1.6."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    NativeScheduledTaskController().register("\\T", program="cmd.exe")
    assert svc.folder.last_user is None
    assert svc.folder.last_logon == 3


@pytest.mark.os_agnostic
def test_register_as_system_highest(monkeypatch: pytest.MonkeyPatch) -> None:
    """TaskPrincipal.system() registers as S-1-5-18, service-account logon, highest run level."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    NativeScheduledTaskController().register("\\T", program="cmd.exe", principal=TaskPrincipal.system())
    definition = svc.folder.tasks["T"].Definition
    assert definition.Principal.UserId == "S-1-5-18"
    assert definition.Principal.LogonType == 5
    assert definition.Principal.RunLevel == 1
    assert svc.folder.last_user == "S-1-5-18"
    assert svc.folder.last_logon == 5


@pytest.mark.os_agnostic
def test_register_as_user_sid_defaults_to_s4u(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain user SID registers with S4U (no stored password) at the limited run level."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    NativeScheduledTaskController().register("\\T", program="cmd.exe", principal=TaskPrincipal(user_id=_USER_SID))
    principal = svc.folder.tasks["T"].Definition.Principal
    assert (principal.UserId, principal.LogonType, principal.RunLevel) == (_USER_SID, 2, 0)
    assert (svc.folder.last_user, svc.folder.last_logon) == (_USER_SID, 2)


@pytest.mark.os_agnostic
def test_register_user_sid_with_explicit_interactive_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit interactive token for a user SID is a valid shape (runs only while that user is logged on)."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    principal = TaskPrincipal(user_id=_USER_SID, logon_type=TaskLogonType.INTERACTIVE_TOKEN)
    NativeScheduledTaskController().register("\\T", program="cmd.exe", principal=principal)
    assert (svc.folder.last_user, svc.folder.last_logon) == (_USER_SID, 3)


@pytest.mark.os_agnostic
def test_register_builds_each_trigger_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each trigger kind maps to its TASK_TRIGGER_TYPE2 and the fields Task Scheduler reads."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    triggers = [
        OnceTrigger(at=_AT),
        DailyTrigger(at=_AT, every_days=3),
        BootTrigger(delay=timedelta(seconds=90)),
        LogonTrigger(user_id=_USER_SID),
    ]
    NativeScheduledTaskController().register("\\T", program="cmd.exe", triggers=triggers)
    once, daily, boot, logon = svc.folder.tasks["T"].Definition.Triggers.items
    assert (once.Type, once.StartBoundary) == (1, "2026-10-21T23:59:00+02:00")
    assert (daily.Type, daily.StartBoundary, daily.DaysInterval) == (2, "2026-10-21T23:59:00+02:00", 3)
    assert (boot.Type, boot.Delay) == (8, "PT90S")
    assert (logon.Type, logon.UserId) == (9, _USER_SID)


@pytest.mark.os_agnostic
def test_register_boot_without_delay_and_logon_for_any_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero boot delay and an any-user logon trigger leave the COM defaults untouched."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    NativeScheduledTaskController().register("\\T", program="cmd.exe", triggers=[BootTrigger(), LogonTrigger()])
    boot, logon = svc.folder.tasks["T"].Definition.Triggers.items
    assert (boot.Type, boot.Delay) == (8, "")
    assert (logon.Type, logon.UserId) == (9, "")


@pytest.mark.os_agnostic
def test_register_without_triggers_creates_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No triggers keeps the run-on-demand task of every release before 1.6."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    NativeScheduledTaskController().register("\\T", program="cmd.exe")
    assert svc.folder.tasks["T"].Definition.Triggers.items == []


@pytest.mark.os_agnostic
def test_register_refuses_a_uid_principal_before_com(monkeypatch: pytest.MonkeyPatch) -> None:
    """A numeric uid is POSIX-only: Windows refuses it rather than registering a nonsense principal."""
    _no_com(monkeypatch)
    with pytest.raises(PlatformUnsupportedError, match="uid"):
        NativeScheduledTaskController().register("\\T", program="cmd.exe", principal=TaskPrincipal(user_id="1000"))


@pytest.mark.os_agnostic
def test_register_refuses_a_uid_logon_trigger_before_com(monkeypatch: pytest.MonkeyPatch) -> None:
    """A logon trigger naming a numeric uid is refused like a uid principal."""
    _no_com(monkeypatch)
    with pytest.raises(PlatformUnsupportedError, match="uid"):
        NativeScheduledTaskController().register("\\T", program="cmd.exe", triggers=[LogonTrigger(user_id="1000")])


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("user_id", "logon_type"),
    [
        pytest.param(_USER_SID, TaskLogonType.SERVICE_ACCOUNT, id="service-logon-for-a-user-sid"),
        pytest.param(None, TaskLogonType.SERVICE_ACCOUNT, id="service-logon-for-the-caller"),
        pytest.param(WellKnownSid.LOCAL_SYSTEM.value, TaskLogonType.INTERACTIVE_TOKEN, id="interactive-system"),
        pytest.param(WellKnownSid.LOCAL_SERVICE.value, TaskLogonType.S4U, id="s4u-local-service"),
        pytest.param(WellKnownSid.NETWORK_SERVICE.value, TaskLogonType.INTERACTIVE_TOKEN, id="interactive-netsvc"),
    ],
)
def test_register_refuses_a_logon_type_the_principal_cannot_use(
    monkeypatch: pytest.MonkeyPatch, user_id: str | None, logon_type: TaskLogonType
) -> None:
    """A service SID logs on only as a service account, and only a service SID can: refused before COM."""
    _no_com(monkeypatch)
    principal = TaskPrincipal(user_id=user_id, logon_type=logon_type)
    with pytest.raises(ConfigurationError, match=logon_type.value):
        NativeScheduledTaskController().register("\\T", program="cmd.exe", principal=principal)


@pytest.mark.os_agnostic
def test_register_wraps_a_com_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A COM failure while building the definition surfaces as NativeCallError."""
    svc = _TriggerFailingService()
    _use(monkeypatch, svc)
    with pytest.raises(NativeCallError, match=r"Triggers\.Create failed"):
        NativeScheduledTaskController().register("\\T", program="cmd.exe", triggers=[BootTrigger()])
    assert svc.folder.registered == []


# --- Real integration: run only on the disposable throwaway VM ---------------

_TASK = "\\pwshpy_test_task"


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only scheduled-task mutation; skip off Windows")
def test_scheduled_task_lifecycle() -> None:
    """Register a scratch task, toggle/run it, then unregister - throwaway VM only."""
    from pwshpy.composition import build_ps

    ps = build_ps()
    with contextlib.suppress(PwshPyError):
        ps.unregister_scheduled_task(_TASK)
    try:
        created = ps.register_scheduled_task(
            _TASK, program="cmd.exe", arguments="/c exit", description="pwshpy scratch"
        )
        assert created.task_name == "pwshpy_test_task"
        assert ps.get_scheduled_task().where(lambda t: t.task_name == "pwshpy_test_task").first() is not None
        assert ps.disable_scheduled_task(_TASK).enabled is False
        assert ps.enable_scheduled_task(_TASK).enabled is True
        ps.start_scheduled_task(_TASK)
        with contextlib.suppress(PwshPyError):
            ps.stop_scheduled_task(_TASK)
    finally:
        with contextlib.suppress(PwshPyError):
            ps.unregister_scheduled_task(_TASK)
    assert ps.get_scheduled_task().where(lambda t: t.task_name == "pwshpy_test_task").first() is None
