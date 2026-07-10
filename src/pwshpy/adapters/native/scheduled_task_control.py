"""native scheduled-task CONTROL over the Task Scheduler 2.0 COM API (Windows-only, **mutating**).

Register / Unregister a task, Enable / Disable it, and Run / Stop it - the mutating
counterpart of the read-only :mod:`~pwshpy.adapters.native.scheduled_tasks` source.
Tasks are addressed by their full path (``\\Folder\\Name``; ``\\Name`` at the root).
``register`` creates a simple run-on-demand EXEC task (a program + arguments, no
trigger).  ``win32com`` is imported lazily; every COM call is wrapped in
:class:`~pwshpy.domain.errors.NativeCallError`.

**MUTATING** - see CLAUDE.md "Development Safety": real tests use a scratch task
they register and unregister on the disposable throwaway VM.

Contents:
    * :class:`NativeScheduledTaskController` - register/unregister/enable/disable/run/stop.
"""

from __future__ import annotations

import importlib
from typing import Any

from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import ScheduledTaskInfo
from .marshal import to_task_state

_TASK_ACTION_EXEC = 0
_TASK_CREATE_OR_UPDATE = 6
_TASK_LOGON_INTERACTIVE_TOKEN = 3
_ROOT = "\\"


def _connect_scheduler() -> Any:
    """Connect to the Task Scheduler service via COM (lazy pywin32 import)."""
    try:
        win32com_client: Any = importlib.import_module("win32com.client")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "Scheduled-task control requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc
    try:
        service = win32com_client.Dispatch("Schedule.Service")
        service.Connect()
    except Exception as exc:
        raise NativeCallError(str(exc) or "cannot connect to the Task Scheduler service") from exc
    return service


def _split_path(task_path: str) -> tuple[str, str]:
    """Split ``\\Folder\\Name`` into ``(folder_path, name)`` (root folder for a bare name)."""
    folder_path, _, name = task_path.rstrip("\\").rpartition("\\")
    return (folder_path or _ROOT), name


def _task_info(task: Any, folder_path: str) -> ScheduledTaskInfo:
    """Marshal an IRegisteredTask into a :class:`ScheduledTaskInfo` snapshot."""
    registration = task.Definition.RegistrationInfo
    return ScheduledTaskInfo.model_construct(
        task_name=str(task.Name),
        task_path=folder_path if folder_path.endswith("\\") else folder_path + "\\",
        state=to_task_state(int(task.State)),
        enabled=bool(task.Enabled),
        author=str(registration.Author or ""),
        description=str(registration.Description or ""),
    )


class NativeScheduledTaskController:
    """Mutating scheduled-task control over the Task Scheduler COM API."""

    def _fetch(self, service: Any, task_path: str) -> tuple[str, Any]:
        folder_path, name = _split_path(task_path)
        folder = service.GetFolder(folder_path)
        return folder_path, folder.GetTask(name)

    def enable(self, task_path: str) -> ScheduledTaskInfo:
        """Enable a scheduled task; return its new state."""
        service = _connect_scheduler()
        try:
            folder_path, task = self._fetch(service, task_path)
            task.Enabled = True
            return _task_info(task, folder_path)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot enable task {task_path!r}") from exc

    def disable(self, task_path: str) -> ScheduledTaskInfo:
        """Disable a scheduled task; return its new state."""
        service = _connect_scheduler()
        try:
            folder_path, task = self._fetch(service, task_path)
            task.Enabled = False
            return _task_info(task, folder_path)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot disable task {task_path!r}") from exc

    def run(self, task_path: str) -> None:
        """Start a scheduled task now (on demand)."""
        service = _connect_scheduler()
        try:
            _folder_path, task = self._fetch(service, task_path)
            task.Run(None)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot run task {task_path!r}") from exc

    def stop(self, task_path: str) -> None:
        """Stop a running scheduled task."""
        service = _connect_scheduler()
        try:
            _folder_path, task = self._fetch(service, task_path)
            task.Stop(0)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot stop task {task_path!r}") from exc

    def unregister(self, task_path: str) -> None:
        """Delete (unregister) a scheduled task."""
        service = _connect_scheduler()
        try:
            folder_path, name = _split_path(task_path)
            service.GetFolder(folder_path).DeleteTask(name, 0)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot unregister task {task_path!r}") from exc

    def register(
        self, task_path: str, *, program: str, arguments: str = "", description: str = ""
    ) -> ScheduledTaskInfo:
        """Register a run-on-demand EXEC task (``program`` + ``arguments``); return it."""
        service = _connect_scheduler()
        try:
            folder_path, name = _split_path(task_path)
            folder = service.GetFolder(folder_path)
            definition = service.NewTask(0)
            definition.RegistrationInfo.Description = description
            definition.Settings.Enabled = True
            definition.Settings.AllowDemandStart = True
            action = definition.Actions.Create(_TASK_ACTION_EXEC)
            action.Path = program
            action.Arguments = arguments
            task = folder.RegisterTaskDefinition(
                name, definition, _TASK_CREATE_OR_UPDATE, None, None, _TASK_LOGON_INTERACTIVE_TOKEN
            )
            return _task_info(task, folder_path)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot register task {task_path!r}") from exc


__all__ = ["NativeScheduledTaskController"]
