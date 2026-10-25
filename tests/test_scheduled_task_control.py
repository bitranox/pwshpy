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
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from pwshpy.adapters.native import scheduled_task_control as mod
from pwshpy.adapters.native import task_readback
from pwshpy.adapters.native.scheduled_task_control import NativeScheduledTaskController
from pwshpy.domain.enums import TaskLogonType, TaskMultipleInstances, TaskRunLevel, TaskState, WellKnownSid
from pwshpy.domain.errors import ConfigurationError, NativeCallError, PlatformUnsupportedError, PwshPyError
from pwshpy.domain.scheduling import (
    BootTrigger,
    DailyTrigger,
    LogonTrigger,
    OnceTrigger,
    TaskPrincipal,
    TaskSettings,
)


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


# What ITaskSettings reads on a fresh definition (measured on Windows 11, 2026-10-25).
_FRESH_SETTINGS: dict[str, Any] = {
    "Enabled": True,
    "AllowDemandStart": True,
    "DisallowStartIfOnBatteries": True,
    "StopIfGoingOnBatteries": True,
    "ExecutionTimeLimit": "PT72H",
    "MultipleInstances": 2,
    "AllowHardTerminate": True,
}


class _FakeSettings:
    """ITaskSettings with a fresh definition's values; ``writes`` records every property set after that."""

    writes: list[tuple[str, Any]]
    Enabled: bool
    AllowDemandStart: bool
    DisallowStartIfOnBatteries: bool
    StopIfGoingOnBatteries: bool
    ExecutionTimeLimit: str
    MultipleInstances: int
    AllowHardTerminate: bool

    def __init__(self) -> None:
        object.__setattr__(self, "writes", [])
        for name, value in _FRESH_SETTINGS.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: Any) -> None:
        self.writes.append((name, value))
        object.__setattr__(self, name, value)


class _ExecutionTimeLimitRefused(_FakeSettings):
    """A COM fault writing ExecutionTimeLimit (the scheduler refusing the value)."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "ExecutionTimeLimit":
            raise RuntimeError("ExecutionTimeLimit refused")
        super().__setattr__(name, value)


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
        self.LastRunTime = datetime(1999, 11, 30, tzinfo=timezone.utc)  # never ran (measured; pywin32's UTC label)
        self.LastTaskResult = 0x41303  # SCHED_S_TASK_HAS_NOT_RUN
        self.NextRunTime = datetime(1899, 12, 30, tzinfo=timezone.utc)  # no next run: the VT_DATE zero

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
    task.LastRunTime = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)  # local wall time, pywin32's UTC label
    task.LastTaskResult = 0
    task.NextRunTime = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)
    svc.folder.tasks["MyTask"] = task
    _use(monkeypatch, svc)
    monkeypatch.setattr(task_readback, "sid_string", _sid_as_given)  # external edge: LookupAccountName
    info = NativeScheduledTaskController().enable("\\MyTask")
    assert info.last_run_time == datetime(2026, 10, 21, 12, 0).astimezone(timezone.utc)  # read as local
    assert info.last_task_result == 0  # 0 is a real result ("ran, succeeded"), never folded into None
    assert info.next_run_time == datetime(2026, 10, 22, 3, 0).astimezone(timezone.utc)


@pytest.mark.os_agnostic
def test_task_info_never_run_reads_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """[120]: the never-run sentinels (1999-11-30, 0x41303, 1899-12-30) read back as None, not as a date or a code."""
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
    """No principal registers for the interactive caller with an interactive token."""
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
    """No triggers registers a run-on-demand task."""
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
@pytest.mark.parametrize(
    ("user_id", "logon_type"),
    [
        pytest.param(WellKnownSid.EVERYONE.value, None, id="everyone-implied-logon"),
        pytest.param(
            WellKnownSid.AUTHENTICATED_USERS.value, TaskLogonType.INTERACTIVE_TOKEN, id="auth-users-interactive"
        ),
        pytest.param(WellKnownSid.ADMINISTRATORS.value, TaskLogonType.S4U, id="builtin-admins-s4u"),
        pytest.param(WellKnownSid.REMOTE_DESKTOP_USERS.value, None, id="builtin-rdp-users-implied-logon"),
        pytest.param(WellKnownSid.USERS.value, TaskLogonType.SERVICE_ACCOUNT, id="builtin-users-service-logon"),
    ],
)
def test_register_refuses_a_group_principal_before_com(
    monkeypatch: pytest.MonkeyPatch, user_id: str, logon_type: TaskLogonType | None
) -> None:
    """A well-known group SID fits no logon type: refused before COM instead of an opaque HRESULT."""
    _no_com(monkeypatch)
    principal = TaskPrincipal(user_id=user_id, logon_type=logon_type)
    # [B3]: Task Scheduler itself takes a group principal; the refusal is pwshpy's own choice.
    with pytest.raises(ConfigurationError, match="group SID; pwshpy registers a single account"):
        NativeScheduledTaskController().register("\\T", program="cmd.exe", principal=principal)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "user_id",
    [
        pytest.param(WellKnownSid.EVERYONE.value, id="everyone"),
        pytest.param(WellKnownSid.AUTHENTICATED_USERS.value, id="auth-users"),
        pytest.param(WellKnownSid.USERS.value, id="builtin-users"),
    ],
)
def test_register_refuses_a_group_logon_trigger_before_com(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    """A logon trigger names one user; a well-known group SID is refused like a group principal."""
    _no_com(monkeypatch)
    with pytest.raises(ConfigurationError, match="group"):
        NativeScheduledTaskController().register("\\T", program="cmd.exe", triggers=[LogonTrigger(user_id=user_id)])


@pytest.mark.os_agnostic
def test_register_passes_a_domain_relative_sid_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A domain-relative SID (user or domain group alike) is not refused: only an LSA lookup tells them apart."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    domain_users = "S-1-5-21-1-2-3-513"
    NativeScheduledTaskController().register(
        "\\T",
        program="cmd.exe",
        principal=TaskPrincipal(user_id=domain_users),
        triggers=[LogonTrigger(user_id=domain_users)],
    )
    assert svc.folder.last_user == domain_users


@pytest.mark.os_agnostic
def test_register_sub_second_boot_delay_writes_no_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """A boot delay under one second rounds to zero whole seconds, so the COM default stays."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    NativeScheduledTaskController().register(
        "\\T", program="cmd.exe", triggers=[BootTrigger(delay=timedelta(milliseconds=500))]
    )
    (boot,) = svc.folder.tasks["T"].Definition.Triggers.items
    assert boot.Delay == ""


@pytest.mark.os_agnostic
def test_add_trigger_refuses_an_unknown_trigger_kind() -> None:
    """A value outside the TaskTrigger union is a programming error, never silently a logon trigger."""
    definition = _FakeDefinition()
    with pytest.raises(TypeError, match="object"):
        mod._add_trigger(definition, cast(Any, object()))  # pyright: ignore[reportPrivateUsage] - the guard has no public path
    assert definition.Triggers.items == []


@pytest.mark.os_agnostic
def test_register_wraps_a_com_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A COM failure while building the definition surfaces as NativeCallError."""
    svc = _TriggerFailingService()
    _use(monkeypatch, svc)
    with pytest.raises(NativeCallError, match=r"Triggers\.Create failed"):
        NativeScheduledTaskController().register("\\T", program="cmd.exe", triggers=[BootTrigger()])
    assert svc.folder.registered == []


# What register writes itself, whatever settings it is given (the pre-[45] behaviour).
_BASE_SETTINGS_WRITES = [("Enabled", True), ("AllowDemandStart", True)]


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="omitted"),
        pytest.param({"settings": None}, id="none"),
        pytest.param({"settings": TaskSettings()}, id="all-fields-unset"),
    ],
)
def test_register_without_settings_writes_no_setting(monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]) -> None:
    """No settings, None, and a TaskSettings with every field unset all leave the definition as before."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    NativeScheduledTaskController().register("\\T", program="cmd.exe", **kwargs)
    settings = svc.folder.tasks["T"].Definition.Settings
    assert settings.writes == _BASE_SETTINGS_WRITES
    assert {name: getattr(settings, name) for name in _FRESH_SETTINGS} == _FRESH_SETTINGS


# pve-winrepair's registration: runs on battery, never stopped by one, a 72 h ceiling, an
# overlapping start ignored, a hung run killable, and an on-demand start allowed.
_PVE_WINREPAIR = TaskSettings(
    allow_start_if_on_batteries=True,
    stop_if_going_on_batteries=False,
    execution_time_limit=timedelta(hours=72),
    multiple_instances=TaskMultipleInstances.IGNORE_NEW,
    allow_hard_terminate=True,
    allow_demand_start=True,
)


@pytest.mark.os_agnostic
def test_register_writes_pve_winrepairs_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every field set reaches ITaskSettings; the limit as PT{n}S, which Windows stores as PT72H, not P3D (measured)."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    NativeScheduledTaskController().register("\\T", program="cmd.exe", settings=_PVE_WINREPAIR)
    settings = svc.folder.tasks["T"].Definition.Settings
    assert settings.Enabled is True
    assert settings.AllowDemandStart is True
    assert settings.AllowHardTerminate is True
    assert settings.ExecutionTimeLimit == "PT259200S"
    assert settings.MultipleInstances == 2
    assert settings.DisallowStartIfOnBatteries is False
    assert settings.StopIfGoingOnBatteries is False
    assert [name for name, _value in settings.writes] == [
        "Enabled",
        "AllowDemandStart",
        "DisallowStartIfOnBatteries",
        "StopIfGoingOnBatteries",
        "ExecutionTimeLimit",
        "MultipleInstances",
        "AllowHardTerminate",
    ]


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("settings", "write"),
    [
        pytest.param(
            TaskSettings(allow_start_if_on_batteries=False), ("DisallowStartIfOnBatteries", True), id="ac-only"
        ),
        pytest.param(
            TaskSettings(allow_start_if_on_batteries=True), ("DisallowStartIfOnBatteries", False), id="battery"
        ),
        pytest.param(TaskSettings(stop_if_going_on_batteries=True), ("StopIfGoingOnBatteries", True), id="stop"),
        pytest.param(TaskSettings(execution_time_limit=timedelta(0)), ("ExecutionTimeLimit", "PT0S"), id="no-limit"),
        pytest.param(
            TaskSettings(execution_time_limit=timedelta(seconds=2147483647)),
            ("ExecutionTimeLimit", "PT2147483647S"),
            id="largest-limit",
        ),
        pytest.param(
            TaskSettings(multiple_instances=TaskMultipleInstances.PARALLEL), ("MultipleInstances", 0), id="par"
        ),
        pytest.param(
            TaskSettings(multiple_instances=TaskMultipleInstances.QUEUE), ("MultipleInstances", 1), id="queue"
        ),
        pytest.param(
            TaskSettings(multiple_instances=TaskMultipleInstances.STOP_EXISTING),
            ("MultipleInstances", 3),
            id="stop-old",
        ),
        pytest.param(TaskSettings(allow_hard_terminate=False), ("AllowHardTerminate", False), id="no-kill"),
    ],
)
def test_register_writes_only_the_fields_set(
    monkeypatch: pytest.MonkeyPatch, settings: TaskSettings, write: tuple[str, Any]
) -> None:
    """One field set writes exactly that one ITaskSettings property, with its COM value."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    NativeScheduledTaskController().register("\\T", program="cmd.exe", settings=settings)
    assert svc.folder.tasks["T"].Definition.Settings.writes == [*_BASE_SETTINGS_WRITES, write]


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("allowed", "written"),
    [pytest.param(None, True, id="unset-keeps-true"), (True, True), (False, False)],
)
def test_register_writes_allow_demand_start_once(
    monkeypatch: pytest.MonkeyPatch, allowed: bool | None, written: bool
) -> None:
    """AllowDemandStart is the one setting register always writes: True unless the caller says False."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    NativeScheduledTaskController().register(
        "\\T", program="cmd.exe", settings=TaskSettings(allow_demand_start=allowed)
    )
    assert svc.folder.tasks["T"].Definition.Settings.writes == [("Enabled", True), ("AllowDemandStart", written)]


@pytest.mark.os_agnostic
def test_register_reads_back_the_effective_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The returned record carries what the definition holds, the untouched OS defaults included."""
    svc = _FakeService()
    _use(monkeypatch, svc)
    info = NativeScheduledTaskController().register(
        "\\T", program="cmd.exe", settings=TaskSettings(multiple_instances=TaskMultipleInstances.QUEUE)
    )
    assert info.settings == TaskSettings(
        allow_start_if_on_batteries=False,  # DisallowStartIfOnBatteries True: the OS default
        stop_if_going_on_batteries=True,
        execution_time_limit=timedelta(hours=72),
        multiple_instances=TaskMultipleInstances.QUEUE,
        allow_hard_terminate=True,
        allow_demand_start=True,
    )


class _SettingsRefusingService(_FakeService):
    """A service whose new definitions refuse an ExecutionTimeLimit write."""

    def NewTask(self, flags: int) -> _FakeDefinition:
        definition = _FakeDefinition()
        definition.Settings = _ExecutionTimeLimitRefused()
        return definition


@pytest.mark.os_agnostic
def test_register_wraps_a_settings_write_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A COM fault writing a setting surfaces as NativeCallError, and nothing is registered."""
    svc = _SettingsRefusingService()
    _use(monkeypatch, svc)
    with pytest.raises(NativeCallError, match="ExecutionTimeLimit refused"):
        NativeScheduledTaskController().register(
            "\\T", program="cmd.exe", settings=TaskSettings(execution_time_limit=timedelta(minutes=5))
        )
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


def _pwsh_json(script: str) -> Any:
    """Run ``script`` in pwsh and parse its JSON output (the oracle module skips itself off Windows)."""
    pwsh = r"C:\Program Files\PowerShell\7\pwsh.exe"
    if not Path(pwsh).exists():
        pytest.skip("PowerShell 7 (pwsh.exe) is not installed; the cross-check needs it")
    completed = subprocess.run(  # noqa: S603 - fixed pwsh argv with a constant script; test oracle only
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return json.loads(completed.stdout)


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only scheduled-task mutation; skip off Windows")
def test_register_as_system_with_every_trigger_kind() -> None:
    """The item's acceptance test: SYSTEM/HIGHEST reads back by SID, and all four trigger kinds register."""
    from pwshpy.composition import build_ps

    ps = build_ps()
    at = datetime.now(timezone.utc) + timedelta(days=1)
    with contextlib.suppress(PwshPyError):
        ps.unregister_scheduled_task(_TASK)
    try:
        info = ps.register_scheduled_task(
            _TASK,
            program="cmd.exe",
            arguments="/c exit",
            principal=TaskPrincipal.system(),
            triggers=[OnceTrigger(at=at), DailyTrigger(at=at), BootTrigger(delay=timedelta(minutes=5)), LogonTrigger()],
        )
        assert info.user_id == WellKnownSid.LOCAL_SYSTEM.value
        assert info.run_level is TaskRunLevel.HIGHEST
        # The CIM class names are locale-independent, unlike any display text pwsh prints.
        kinds = _pwsh_json(
            "ConvertTo-Json -Compress -InputObject @((Get-ScheduledTask -TaskName pwshpy_test_task).Triggers"
            " | ForEach-Object { $_.CimClass.CimClassName })"
        )
        assert sorted(kinds) == sorted(
            ["MSFT_TaskTimeTrigger", "MSFT_TaskDailyTrigger", "MSFT_TaskBootTrigger", "MSFT_TaskLogonTrigger"]
        )
        ps.start_scheduled_task(_TASK)  # on-demand start still works with triggers present
    finally:
        with contextlib.suppress(PwshPyError):
            ps.unregister_scheduled_task(_TASK)


_RAN = "pwshpy_test_ran"
_NEVER = "pwshpy_test_never"


def _pwsh_task_info(name: str) -> Any:
    """Get-ScheduledTaskInfo as JSON: LastRunTime in UTC (invariant format), LastTaskResult as a number."""
    return _pwsh_json(
        f"$i = Get-ScheduledTaskInfo -TaskName {name}; [pscustomobject]@{{"
        " LastRunUtc = $i.LastRunTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss',"
        " [cultureinfo]::InvariantCulture); LastTaskResult = [int64]$i.LastTaskResult } | ConvertTo-Json -Compress"
    )


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only scheduled-task mutation; skip off Windows")
def test_run_info_for_a_just_run_and_a_never_run_task() -> None:
    """[120] on a real Task Scheduler: a finished run reads its UTC time and result; a never-run task reads None.

    The pwsh cross-check catches a run time off by the host's UTC offset (pywin32 labels a
    local VT_DATE as UTC), and shows the never-run values are the service's own sentinels.
    """
    from pwshpy.composition import build_ps

    ps = build_ps()
    for name in (_RAN, _NEVER):
        with contextlib.suppress(PwshPyError):
            ps.unregister_scheduled_task("\\" + name)
    try:
        ps.register_scheduled_task("\\" + _RAN, program="cmd.exe", arguments="/c exit 0")
        ps.register_scheduled_task("\\" + _NEVER, program="cmd.exe", arguments="/c exit 0")
        ps.start_scheduled_task("\\" + _RAN)
        deadline = time.monotonic() + 60
        ran = ps.get_scheduled_task().first(lambda t: t.task_name == _RAN)
        # Wait for the result too: the state can flip to Ready before LastTaskResult is written.
        while (
            ran is not None
            and (ran.state is TaskState.RUNNING or ran.last_task_result is None)
            and time.monotonic() < deadline
        ):
            time.sleep(1)
            ran = ps.get_scheduled_task().first(lambda t: t.task_name == _RAN)
        never = ps.get_scheduled_task().first(lambda t: t.task_name == _NEVER)
        assert ran is not None
        assert never is not None
        assert ran.state is not TaskState.RUNNING
        assert ran.last_task_result == 0
        assert ran.last_run_time is not None
        assert ran.last_run_time.tzinfo is timezone.utc
        assert timedelta(0) <= datetime.now(timezone.utc) - ran.last_run_time < timedelta(minutes=5)
        oracle = _pwsh_task_info(_RAN)
        oracle_utc = datetime.fromisoformat(oracle["LastRunUtc"] + "+00:00")
        assert abs(ran.last_run_time - oracle_utc) <= timedelta(seconds=2)
        assert oracle["LastTaskResult"] == ran.last_task_result
        assert (never.last_run_time, never.last_task_result, never.next_run_time) == (None, None, None)
        never_oracle = _pwsh_task_info(_NEVER)
        assert never_oracle["LastTaskResult"] == 0x41303  # SCHED_S_TASK_HAS_NOT_RUN, read as None above
        assert never_oracle["LastRunUtc"].startswith("1999-11-")  # 1999-11-30 local: the never-ran date
    finally:
        for name in (_RAN, _NEVER):
            with contextlib.suppress(PwshPyError):
                ps.unregister_scheduled_task("\\" + name)


# --- [45] TaskSettings on a real Task Scheduler: throwaway VM only ------------

# Each written TaskSettings, with what Task Scheduler then holds (``stored``, all measured on VM
# 24000): the ExecutionTimeLimit text it reports, the task XML's ExecutionTimeLimit element, the
# MultipleInstances COM integer and the XML's MultipleInstancesPolicy element. "" is an absent
# element: a written PT259200S is stored as PT72H, the default, and the XML then holds no limit,
# whereas a written IgnoreNew (the default policy too) keeps its element. The CIM enum behind
# Get-ScheduledTask's MultipleInstances lacks StopExisting (it reads $null there), so the policy is
# compared through COM and the XML instead.
_SETTINGS_CASES = [
    pytest.param(_PVE_WINREPAIR, ("PT72H", "", 2, "IgnoreNew"), id="pve-winrepair"),
    pytest.param(
        TaskSettings(
            allow_start_if_on_batteries=False,
            stop_if_going_on_batteries=True,
            execution_time_limit=timedelta(0),
            multiple_instances=TaskMultipleInstances.STOP_EXISTING,
            allow_hard_terminate=False,
            allow_demand_start=False,
        ),
        ("PT0S", "PT0S", 3, "StopExisting"),
        id="opposite",
    ),
    pytest.param(
        TaskSettings(
            allow_start_if_on_batteries=True,
            stop_if_going_on_batteries=True,
            execution_time_limit=timedelta(seconds=90),
            multiple_instances=TaskMultipleInstances.PARALLEL,
            allow_hard_terminate=False,
            allow_demand_start=True,
        ),
        ("PT1M30S", "PT1M30S", 0, "Parallel"),
        id="parallel",
    ),
    pytest.param(
        TaskSettings(
            allow_start_if_on_batteries=False,
            stop_if_going_on_batteries=False,
            execution_time_limit=timedelta(days=3, seconds=1),
            multiple_instances=TaskMultipleInstances.QUEUE,
            allow_hard_terminate=True,
            allow_demand_start=False,
        ),
        ("P3DT1S", "P3DT1S", 1, "Queue"),
        id="queue",
    ),
]

# Get-ScheduledTask's Settings plus the task's own COM definition and XML, all locale-independent:
# booleans, integers, the ISO 8601 limit text and .NET's own parse of it (XmlConvert), and the XML
# element names and values, which the task schema fixes in English.
_SETTINGS_ORACLE = (
    "$s = (Get-ScheduledTask -TaskName {name}).Settings;"
    " $svc = New-Object -ComObject Schedule.Service; $svc.Connect();"
    " $com = $svc.GetFolder('\\').GetTask('{name}').Definition.Settings;"
    " $xml = ([xml](Export-ScheduledTask -TaskName {name})).Task.Settings;"
    " [pscustomobject]@{{ Disallow = [bool]$s.DisallowStartIfOnBatteries; Stop = [bool]$s.StopIfGoingOnBatteries;"
    " Kill = [bool]$s.AllowHardTerminate; Demand = [bool]$s.AllowDemandStart;"
    " LimitText = [string]$s.ExecutionTimeLimit;"
    " LimitSeconds = [System.Xml.XmlConvert]::ToTimeSpan($s.ExecutionTimeLimit).TotalSeconds;"
    " Policy = [int]$com.MultipleInstances; XmlLimit = [string]$xml.ExecutionTimeLimit;"
    " XmlPolicy = [string]$xml.MultipleInstancesPolicy }} | ConvertTo-Json -Compress"
)


@pytest.fixture
def scratch_task() -> Iterator[str]:
    """Yield the scratch task path, removed before and after the test (throwaway VM only)."""
    from pwshpy.composition import build_ps

    ps = build_ps()
    with contextlib.suppress(PwshPyError):
        ps.unregister_scheduled_task(_TASK)
    try:
        yield _TASK
    finally:
        with contextlib.suppress(PwshPyError):
            ps.unregister_scheduled_task(_TASK)


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only scheduled-task mutation; skip off Windows")
@pytest.mark.parametrize(("settings", "stored"), _SETTINGS_CASES)
def test_register_settings_read_back_like_get_scheduledtask(
    scratch_task: str, settings: TaskSettings, stored: tuple[str, str, int, str]
) -> None:
    """[45] Every field written reads back unchanged, through pwshpy and through pwsh's own view of the task.

    The pve-winrepair row (SYSTEM, no trigger) is the registration that caller makes today; its
    PT259200S is stored as PT72H - Task Scheduler's default, so the task XML then carries no
    ExecutionTimeLimit element at all.
    """
    from pwshpy.composition import build_ps

    ps = build_ps()
    info = ps.register_scheduled_task(
        scratch_task, program="cmd.exe", arguments="/c exit", principal=TaskPrincipal.system(), settings=settings
    )
    assert info.settings == settings
    looked_up = ps.get_scheduled_task("\\", task_name="pwshpy_test_task", strict=True).first()
    assert looked_up is not None
    assert looked_up.settings == settings
    oracle = _pwsh_json(_SETTINGS_ORACLE.format(name="pwshpy_test_task"))
    assert (not oracle["Disallow"], oracle["Stop"], oracle["Kill"], oracle["Demand"]) == (
        settings.allow_start_if_on_batteries,
        settings.stop_if_going_on_batteries,
        settings.allow_hard_terminate,
        settings.allow_demand_start,
    )
    assert timedelta(seconds=float(oracle["LimitSeconds"])) == settings.execution_time_limit
    assert (oracle["LimitText"], oracle["XmlLimit"], oracle["Policy"], oracle["XmlPolicy"]) == stored


# SCHED_E_START_ON_DEMAND ("the task's settings do not allow the task to start on demand"; the
# name as certutil -error prints it), the scode IRegisteredTask.Run reports inside DISP_E_EXCEPTION.
_SCHED_E_START_ON_DEMAND = 0x80041328


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only scheduled-task mutation; skip off Windows")
def test_start_is_refused_when_demand_start_is_disallowed(scratch_task: str) -> None:
    """[45] allow_demand_start=False: start_scheduled_task raises NativeCallError and the task never runs."""
    from pwshpy.composition import build_ps

    ps = build_ps()
    ps.register_scheduled_task(
        scratch_task,
        program="cmd.exe",
        arguments="/c exit",
        principal=TaskPrincipal.system(),
        settings=TaskSettings(allow_demand_start=False),
    )
    with pytest.raises(NativeCallError) as refused:
        ps.start_scheduled_task(scratch_task)
    excepinfo = cast("tuple[object, ...]", getattr(refused.value.__cause__, "excepinfo", ()))
    assert len(excepinfo) == 6
    assert cast("int", excepinfo[5]) & 0xFFFFFFFF == _SCHED_E_START_ON_DEMAND
    time.sleep(2)  # a run that slipped through would have a last-run time by now
    task = ps.get_scheduled_task("\\", task_name="pwshpy_test_task", strict=True).first()
    assert task is not None
    assert (task.last_run_time, task.last_task_result) == (None, None)
    assert task.settings is not None
    assert task.settings.allow_demand_start is False


# SCHED_S_TASK_TERMINATED: the LastTaskResult of a run the scheduler ended (here at its time limit).
_SCHED_S_TASK_TERMINATED = 0x00041306


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only scheduled-task mutation; skip off Windows")
def test_execution_time_limit_ends_an_on_demand_run(scratch_task: str) -> None:
    """[45] A 10 s limit ends a ~29 s on-demand run after about 10 s, with LastTaskResult 0x41306."""
    from pwshpy.composition import build_ps

    ps = build_ps()
    ps.register_scheduled_task(
        scratch_task,
        program="cmd.exe",
        arguments="/c ping -n 30 127.0.0.1",
        principal=TaskPrincipal.system(),
        settings=TaskSettings(execution_time_limit=timedelta(seconds=10)),
    )
    started = time.monotonic()
    ps.start_scheduled_task(scratch_task)
    task = ps.get_scheduled_task("\\", task_name="pwshpy_test_task", strict=True).first()
    # Wait for the result too: the state can flip to Ready before LastTaskResult is written.
    while (
        task is not None
        and (task.state is TaskState.RUNNING or task.last_task_result is None)
        and time.monotonic() - started < 60
    ):
        time.sleep(0.5)
        task = ps.get_scheduled_task("\\", task_name="pwshpy_test_task", strict=True).first()
    elapsed = time.monotonic() - started
    assert task is not None
    assert task.state is not TaskState.RUNNING
    assert task.last_task_result == _SCHED_S_TASK_TERMINATED
    assert 9 <= elapsed < 20, elapsed  # the ping alone runs ~29 s
    assert _pwsh_task_info("pwshpy_test_task")["LastTaskResult"] == _SCHED_S_TASK_TERMINATED
