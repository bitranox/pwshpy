"""native scheduled-task source over the Task Scheduler 2.0 COM API (Windows-only), streaming.

Walks the Task Scheduler folder tree via ``Schedule.Service`` (win32com), mirroring
``Get-ScheduledTask``.  The adapter is a GENERATOR that recurses the folders and
yields one :class:`~pwshpy.domain.records.ScheduledTaskInfo` at a time, so the
pipeline stays memory-bounded across a large task tree.  ``win32com`` (from
pywin32) is imported lazily so a portable install stays clean and raises
:class:`~pwshpy.domain.errors.PlatformUnsupportedError`.

Contents:
    * :func:`iter_scheduled_tasks` - stream the registered tasks under a folder.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import ScheduledTaskInfo
from .marshal import to_task_state

_TASK_ENUM_HIDDEN = 1  # include hidden tasks, like Get-ScheduledTask
_ROOT = "\\"


def _connect_scheduler() -> Any:
    """Connect to the Task Scheduler service via COM (lazy pywin32 import)."""
    try:
        win32com_client: Any = importlib.import_module("win32com.client")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "The scheduled-tasks subsystem requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc
    try:
        service = win32com_client.Dispatch("Schedule.Service")
        service.Connect()
    except Exception as exc:
        raise NativeCallError(str(exc) or "cannot connect to the Task Scheduler service") from exc
    return service


def _folder_path(folder: Any) -> str:
    path = str(folder.Path)
    return path if path.endswith("\\") else path + "\\"


def _to_task_info(task: Any, folder_path: str) -> ScheduledTaskInfo:
    registration = task.Definition.RegistrationInfo
    return ScheduledTaskInfo.model_construct(
        task_name=str(task.Name),
        task_path=folder_path,
        state=to_task_state(int(task.State)),
        enabled=bool(task.Enabled),
        author=str(registration.Author or ""),
        description=str(registration.Description or ""),
    )


def _safe_task(tasks: Any, index: int, folder_path: str) -> ScheduledTaskInfo | None:
    """Marshal one task, returning None if it is protected/corrupt (so the walk skips it)."""
    try:
        return _to_task_info(tasks.Item(index), folder_path)
    except Exception:  # a protected/corrupt task definition - skip it, like Get-ScheduledTask
        return None


def _safe_child(subfolders: Any, index: int) -> Any:
    """Return one subfolder handle, or None if it cannot be accessed."""
    try:
        return subfolders.Item(index)
    except Exception:  # one bad subfolder handle - skip it
        return None


def _walk(folder: Any) -> Iterator[ScheduledTaskInfo]:
    """Yield tasks under a folder, recursing subfolders and skipping any that deny access.

    A protected/corrupt task or an inaccessible subfolder is skipped rather than
    aborting the whole enumeration, mirroring ``Get-ScheduledTask``.
    """
    folder_path = _folder_path(folder)
    try:
        tasks = folder.GetTasks(_TASK_ENUM_HIDDEN)
    except Exception:  # inaccessible folder - skip its tasks
        tasks = None
    if tasks is not None:
        for index in range(1, tasks.Count + 1):
            info = _safe_task(tasks, index, folder_path)
            if info is not None:
                yield info
    try:
        subfolders = folder.GetFolders(0)
        subfolder_count = subfolders.Count
    except Exception:  # inaccessible subfolders - stop descending
        return
    for index in range(1, subfolder_count + 1):
        child = _safe_child(subfolders, index)
        if child is not None:
            yield from _walk(child)


def iter_scheduled_tasks(folder_path: str = _ROOT) -> Iterator[ScheduledTaskInfo]:
    """Stream the scheduled tasks under a folder, recursing subfolders (like ``Get-ScheduledTask``).

    Yields one record at a time while walking the folder tree, so it stays
    memory-bounded across a large task set.

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import ScheduledTaskInfo
        >>> sys.platform != "win32" or isinstance(next(iter_scheduled_tasks()), ScheduledTaskInfo)
        True
    """
    service = _connect_scheduler()
    try:
        root = service.GetFolder(folder_path)
    except Exception as exc:
        raise NativeCallError(str(exc) or f"cannot open task folder {folder_path!r}") from exc
    yield from _walk(root)


__all__ = ["iter_scheduled_tasks"]
