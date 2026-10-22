"""native scheduled-task source over the Task Scheduler 2.0 COM API (Windows-only), streaming.

Walks the Task Scheduler folder tree via ``Schedule.Service`` (win32com), mirroring
``Get-ScheduledTask``.  The adapter is a GENERATOR that recurses the folders and
yields one :class:`~pwshpy.domain.records.ScheduledTaskInfo` at a time, so the
pipeline stays memory-bounded across a large task tree.  What the caller cannot read (a
folder's task or subfolder list, a subfolder, a task) is skipped with a logged WARNING, or
raised as :class:`~pwshpy.domain.errors.NativeCallError` under ``strict=True``, so an
unreadable task is never silently indistinguishable from an absent one.  ``win32com`` (from
pywin32) is imported lazily so a portable install stays clean and raises
:class:`~pwshpy.domain.errors.PlatformUnsupportedError`.

Contents:
    * :func:`iter_scheduled_tasks` - stream the registered tasks under a folder.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator
from typing import Any

from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import ScheduledTaskInfo
from .marshal import to_task_state
from .task_readback import read_task_extras

logger = logging.getLogger(__name__)

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
    extras = read_task_extras(task)  # fail-soft per field: never costs the task
    return ScheduledTaskInfo.model_construct(
        task_name=str(task.Name),
        task_path=folder_path,
        state=to_task_state(int(task.State)),
        enabled=bool(task.Enabled),
        author=str(registration.Author or ""),
        description=str(registration.Description or ""),
        user_id=extras.user_id,
        run_level=extras.run_level,
        last_run_time=extras.last_run_time,
        last_task_result=extras.last_task_result,
        next_run_time=extras.next_run_time,
    )


def _skip_or_raise(what: str, exc: Exception, *, strict: bool) -> None:
    """Raise NativeCallError naming ``what`` under ``strict``; otherwise log a WARNING and return.

    The lenient walk still skips (a non-elevated caller must be able to list the tree it CAN
    read), but never silently: a skipped item reads exactly like "no such task" to a caller
    that looks a task up, so the log has to say what was not read and why.
    """
    if strict:
        raise NativeCallError(f"{what}: {exc}") from exc
    logger.warning("skipped (pass strict=True to raise instead) - %s: %s", what, exc)


def _task_label(task: Any, index: int, folder_path: str) -> str:
    """The task's full path for a message, or its position in the folder when even its name is unreadable."""
    position = f"#{index} in folder '{folder_path}'"
    if task is None:  # the collection would not hand the task out at all
        return position
    try:
        return f"'{folder_path}{task.Name}'"
    except Exception:  # the name itself is unreadable: fall back to the position COM gave it
        return position


def _read_task(tasks: Any, index: int, folder_path: str, *, strict: bool) -> ScheduledTaskInfo | None:
    """Marshal one task; a protected/corrupt one is skipped with a warning (None) or raised under ``strict``."""
    task: Any = None
    try:
        task = tasks.Item(index)
        return _to_task_info(task, folder_path)
    except Exception as exc:  # a protected/corrupt task definition
        _skip_or_raise(f"cannot read task {_task_label(task, index, folder_path)}", exc, strict=strict)
        return None


def _folder_tasks(folder: Any, folder_path: str, *, strict: bool) -> Iterator[ScheduledTaskInfo]:
    """Yield the tasks directly in ``folder``; an unenumerable task list is skipped or raised."""
    try:
        tasks = folder.GetTasks(_TASK_ENUM_HIDDEN)
        count = int(tasks.Count)
    except Exception as exc:  # the folder denies listing its tasks
        _skip_or_raise(f"cannot list the tasks in folder '{folder_path}'", exc, strict=strict)
        return
    for index in range(1, count + 1):
        info = _read_task(tasks, index, folder_path, strict=strict)
        if info is not None:
            yield info


def _subfolders(folder: Any, folder_path: str, *, strict: bool) -> Iterator[tuple[Any, str]]:
    """Yield each subfolder of ``folder`` with its path; an unreadable list or child is skipped or raised."""
    try:
        subfolders = folder.GetFolders(0)
        count = int(subfolders.Count)
    except Exception as exc:  # the folder denies listing its subfolders
        _skip_or_raise(f"cannot list the subfolders of folder '{folder_path}'", exc, strict=strict)
        return
    for index in range(1, count + 1):
        try:
            child = subfolders.Item(index)
            child_path = _folder_path(child)
        except Exception as exc:  # one subfolder handle cannot be opened
            _skip_or_raise(f"cannot open subfolder #{index} of folder '{folder_path}'", exc, strict=strict)
            continue
        yield child, child_path


def _walk(folder: Any, folder_path: str, *, strict: bool) -> Iterator[ScheduledTaskInfo]:
    """Yield the tasks under a folder, its own first, then each subfolder's, depth first.

    What cannot be read - a folder's task list, a task, a folder's subfolder list, a subfolder -
    is skipped with a WARNING, mirroring ``Get-ScheduledTask``; under ``strict`` it raises
    :class:`NativeCallError` instead, when the walk reaches it.
    """
    yield from _folder_tasks(folder, folder_path, strict=strict)
    for child, child_path in _subfolders(folder, folder_path, strict=strict):
        yield from _walk(child, child_path, strict=strict)


def iter_scheduled_tasks(folder_path: str = _ROOT, *, strict: bool = False) -> Iterator[ScheduledTaskInfo]:
    """Stream the scheduled tasks under a folder, recursing subfolders (like ``Get-ScheduledTask``).

    Yields one record at a time while walking the folder tree, so it stays memory-bounded
    across a large task set.

    Args:
        folder_path: The task folder to recurse, e.g. ``"\\Microsoft\\Windows\\"``; the root
            by default. A folder that does not exist or cannot be opened always raises.
        strict: What happens to an item the caller cannot read (a folder whose tasks or
            subfolders it may not list, a protected or corrupt task). False (default) skips it
            and logs a WARNING naming it, so a non-elevated caller can list what it CAN read.
            True raises :class:`NativeCallError` naming its folder or task path instead, so an
            unreadable task never reads like an absent one. The walk is lazy: the error comes
            when iteration reaches the item, after the records before it were yielded.

    Yields:
        One :class:`ScheduledTaskInfo` per task, folder by folder.

    Raises:
        NativeCallError: The scheduler cannot be reached, ``folder_path`` cannot be opened, or
            (with ``strict``) an item under it cannot be read.
        PlatformUnsupportedError: Off Windows (no pywin32).

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import ScheduledTaskInfo
        >>> sys.platform != "win32" or isinstance(next(iter_scheduled_tasks()), ScheduledTaskInfo)
        True
    """
    service = _connect_scheduler()
    try:
        root = service.GetFolder(folder_path)
        root_path = _folder_path(root)
    except Exception as exc:
        raise NativeCallError(f"cannot open task folder '{folder_path}': {exc}") from exc
    yield from _walk(root, root_path, strict=strict)


__all__ = ["iter_scheduled_tasks"]
