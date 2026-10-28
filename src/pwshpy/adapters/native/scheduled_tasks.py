"""native scheduled-task source over the Task Scheduler 2.0 COM API (Windows-only), streaming.

Walks the Task Scheduler folder tree via ``Schedule.Service`` (win32com), mirroring
``Get-ScheduledTask``.  The adapter is a GENERATOR that recurses the folders and
yields one :class:`~pwshpy.domain.records.ScheduledTaskInfo` at a time, so the
pipeline stays memory-bounded across a large task tree.  What the caller cannot read (a
folder's task or subfolder list, a subfolder, a task) is skipped with a logged WARNING, or
raised as :class:`~pwshpy.domain.errors.NativeCallError` under ``strict=True``, so an
unreadable task is never silently indistinguishable from an absent one.  Given a task name it
opens that one task directly (``ITaskFolder.GetTask``) instead of walking, so an unreadable
task elsewhere in the tree cannot refuse a question about a different one.  ``win32com`` (from
pywin32) is imported lazily so a portable install stays clean and raises
:class:`~pwshpy.domain.errors.PlatformUnsupportedError`.

Contents:
    * :func:`iter_scheduled_tasks` - stream the registered tasks under a folder, or look up one.
    * :func:`is_not_found` - whether a Task Scheduler COM error means "no such task or folder".
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator
from typing import Any

from ...domain.errors import ConfigurationError, PlatformUnsupportedError
from ...domain.records import ScheduledTaskInfo
from .marshal import com_hresult, native_error, skip_or_raise, to_task_state
from .task_readback import read_task_extras

logger = logging.getLogger(__name__)

_TASK_ENUM_HIDDEN = 1  # include hidden tasks, like Get-ScheduledTask
_ROOT = "\\"
# The one status meaning "absent": GetFolder and GetTask both report a missing task, a missing
# folder and a task in a missing folder as ERROR_FILE_NOT_FOUND (measured 2026-10-23 through
# the COM API). Every other status is a failure to read, never an absence.
_ERROR_FILE_NOT_FOUND = 0x80070002
# Measured 2026-10-23: pywin32's late-bound CDispatch reports every GetFolder/GetTask failure as
# DISP_E_EXCEPTION with the status in the scode, and GetFolder refuses a trailing backslash on a
# non-root folder with ERROR_INVALID_NAME (0x8007007B) whether or not the folder exists.


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
        raise native_error(str(exc) or "cannot connect to the Task Scheduler service", exc) from exc
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
        settings=extras.settings,
    )


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
        skip_or_raise(f"cannot read task {_task_label(task, index, folder_path)}", exc, strict=strict, log=logger)
        return None


def _folder_tasks(folder: Any, folder_path: str, *, strict: bool) -> Iterator[ScheduledTaskInfo]:
    """Yield the tasks directly in ``folder``; an unenumerable task list is skipped or raised."""
    try:
        tasks = folder.GetTasks(_TASK_ENUM_HIDDEN)
        count = int(tasks.Count)
    except Exception as exc:  # the folder denies listing its tasks
        skip_or_raise(f"cannot list the tasks in folder '{folder_path}'", exc, strict=strict, log=logger)
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
        skip_or_raise(f"cannot list the subfolders of folder '{folder_path}'", exc, strict=strict, log=logger)
        return
    for index in range(1, count + 1):
        try:
            child = subfolders.Item(index)
            child_path = _folder_path(child)
        except Exception as exc:  # one subfolder handle cannot be opened
            skip_or_raise(f"cannot open subfolder #{index} of folder '{folder_path}'", exc, strict=strict, log=logger)
            continue
        yield child, child_path


def is_not_found(exc: BaseException) -> bool:
    """Whether a Task Scheduler COM error means the task or its folder does not exist.

    Only HRESULT 0x80070002 (ERROR_FILE_NOT_FOUND) counts, as :func:`~.marshal.com_hresult`
    reads it: reported directly, wrapped in a DISP_E_EXCEPTION's excepinfo, or as Win32 code 2
    on a ``pywintypes.error``. An error that is not pywin32's, or carries no status, is never
    read as absent, whatever its message says.

    Args:
        exc: The error a ``GetFolder`` / ``GetTask`` call raised.

    Returns:
        True only for the not-found status.

    Example:
        >>> is_not_found(OSError("The system cannot find the file specified"))
        False
    """
    return com_hresult(exc) == _ERROR_FILE_NOT_FOUND


def _check_task_name(task_name: str) -> None:
    """Refuse a ``task_name`` that names something other than one task in ``folder_path``.

    An empty name names the folder itself, and a name with a backslash is a relative PATH that
    GetTask resolves into another folder, so the record would carry the wrong ``task_path``.
    """
    if task_name == "":
        raise ConfigurationError("task_name must not be empty: an empty name names the folder, not a task")
    if "\\" in task_name:
        raise ConfigurationError(
            f"task_name {task_name!r} contains a backslash: pass the folder as folder_path "
            "and only the task's own name as task_name"
        )


def _lookup(service: Any, folder_path: str, task_name: str, *, strict: bool) -> Iterator[ScheduledTaskInfo]:
    """Yield the one task ``task_name`` in ``folder_path``, or nothing when it does not exist.

    Only opening the folder or the task can answer "absent": a not-found there is an empty answer
    under ``strict`` too. Any other failure to open them, and ANY failure reading a task GetTask
    found (a not-found code included: a found task is never absent), follows the listing's
    skip-or-raise rule, naming the task's full path. ``folder_path`` may end in a backslash, the
    form a record's ``task_path`` carries: it is stripped, because GetFolder refuses it.
    """
    com_folder = folder_path.rstrip("\\") or _ROOT
    prefix = "" if com_folder == _ROOT else com_folder
    label = f"'{prefix}\\{task_name}'"
    try:
        folder = service.GetFolder(com_folder)
        task = folder.GetTask(task_name)
    except Exception as exc:
        if not is_not_found(exc):
            skip_or_raise(f"cannot read task {label}", exc, strict=strict, log=logger)
        return
    try:
        info = _to_task_info(task, _folder_path(folder))
    except Exception as exc:  # found, but unreadable: never reported as absent
        skip_or_raise(f"cannot read task {label}", exc, strict=strict, log=logger)
        return
    yield info


def _walk(folder: Any, folder_path: str, *, strict: bool) -> Iterator[ScheduledTaskInfo]:
    """Yield the tasks under a folder, its own first, then each subfolder's, depth first.

    What cannot be read - a folder's task list, a task, a folder's subfolder list, a subfolder -
    is skipped with a WARNING, mirroring ``Get-ScheduledTask``; under ``strict`` it raises
    :class:`NativeCallError` instead, when the walk reaches it.
    """
    yield from _folder_tasks(folder, folder_path, strict=strict)
    for child, child_path in _subfolders(folder, folder_path, strict=strict):
        yield from _walk(child, child_path, strict=strict)


def iter_scheduled_tasks(
    folder_path: str = _ROOT, *, strict: bool = False, task_name: str | None = None
) -> Iterator[ScheduledTaskInfo]:
    """Stream the scheduled tasks under a folder, recursing subfolders (like ``Get-ScheduledTask``).

    Yields one record at a time while walking the folder tree, so it stays memory-bounded
    across a large task set. Given ``task_name`` it walks nothing: it opens that one task in
    ``folder_path`` directly and yields it, or nothing when it does not exist.

    Args:
        folder_path: The task folder to recurse, e.g. ``"\\Microsoft\\Windows"``; the root
            by default. Without ``task_name`` a folder that does not exist or cannot be opened
            always raises; with it, a missing folder means the task is absent (empty result),
            and a trailing backslash (the form ``task_path`` carries) is accepted.
        strict: What happens to an item the caller cannot read (a folder whose tasks or
            subfolders it may not list, a protected or corrupt task). False (default) skips it
            and logs a WARNING naming it, so a non-elevated caller can list what it CAN read.
            True raises :class:`NativeCallError` naming its folder or task path instead, so an
            unreadable task never reads like an absent one. The walk is lazy: the error comes
            when iteration reaches the item, after the records before it were yielded. With
            ``task_name`` it decides only a task that exists but cannot be opened or read; a
            task or folder that does not exist is an empty result either way.
        task_name: Look up this one task in ``folder_path`` (no subfolders) instead of walking
            the tree, so an unreadable task elsewhere never refuses the lookup. The task's own
            name only, no backslash. Matched without regard to case, and the record carries the
            spelling passed in ``task_name`` and ``folder_path``: the scheduler echoes the
            caller's spelling in the task's name, path and URI (measured 2026-10-23). None
            (default) walks as before.

    Yields:
        One :class:`ScheduledTaskInfo` per task, folder by folder; with ``task_name``, at
        most one.

    Raises:
        ConfigurationError: ``task_name`` is empty (it would name the folder, not a task) or
            contains a backslash (it would name a path, not a task in ``folder_path``).
        NativeCallError: The scheduler cannot be reached, ``folder_path`` cannot be opened
            (without ``task_name``), or (with ``strict``) an item under it cannot be read.
        PlatformUnsupportedError: Off Windows (no pywin32).

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import ScheduledTaskInfo
        >>> sys.platform != "win32" or isinstance(next(iter_scheduled_tasks()), ScheduledTaskInfo)
        True
        >>> sys.platform != "win32" or list(iter_scheduled_tasks(task_name="pwshpy-no-such-task")) == []
        True
    """
    if task_name is not None:
        _check_task_name(task_name)
    service = _connect_scheduler()
    if task_name is not None:
        yield from _lookup(service, folder_path, task_name, strict=strict)
        return
    try:
        root = service.GetFolder(folder_path)
        root_path = _folder_path(root)
    except Exception as exc:
        raise native_error(f"cannot open task folder '{folder_path}': {exc}", exc) from exc
    yield from _walk(root, root_path, strict=strict)


__all__ = ["is_not_found", "iter_scheduled_tasks"]
