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
from typing import Any

import pytest

from pwshpy.adapters.native import scheduled_task_control as mod
from pwshpy.adapters.native.scheduled_task_control import NativeScheduledTaskController
from pwshpy.domain.errors import NativeCallError, PwshPyError


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


class _FakeSettings:
    def __init__(self) -> None:
        self.Enabled = True
        self.AllowDemandStart = True


class _FakeDefinition:
    def __init__(self) -> None:
        self.RegistrationInfo = _FakeRegInfo()
        self.Actions = _FakeActions()
        self.Settings = _FakeSettings()


class _FakeTask:
    def __init__(self, name: str, definition: _FakeDefinition | None = None) -> None:
        self.Name = name
        self.Enabled = True
        self.State = 3
        self.Definition = definition or _FakeDefinition()
        self.actions: list[str] = []

    def Run(self, params: Any) -> None:
        self.actions.append("run")

    def Stop(self, flags: int) -> None:
        self.actions.append("stop")


class _FakeFolder:
    def __init__(self) -> None:
        self.tasks: dict[str, _FakeTask] = {}
        self.deleted: list[str] = []
        self.registered: list[str] = []

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
        return task


class _FakeService:
    def __init__(self) -> None:
        self.folder = _FakeFolder()

    def GetFolder(self, path: str) -> _FakeFolder:
        return self.folder

    def NewTask(self, flags: int) -> _FakeDefinition:
        return _FakeDefinition()


def _use(monkeypatch: pytest.MonkeyPatch, svc: _FakeService) -> None:
    monkeypatch.setattr(mod, "_connect_scheduler", lambda: svc)


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
