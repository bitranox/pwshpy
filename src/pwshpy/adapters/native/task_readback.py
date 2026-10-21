"""Fail-soft read-back of a Task Scheduler task's principal and run info (Windows; COM).

Shared by the read-only listing and the mutating controller so both report the same
fields the same way. Each field is read on its own: a principal that no longer resolves
to a SID (a deleted account, an unmappable name) or a run-info property COM refuses
degrades THAT field to its "unknown" value. It never drops the task from a listing, and
it never turns a verb that already succeeded into a failure.

Contents:
    * :class:`TaskExtras` - the principal and [120] run-info fields of one task.
    * :func:`read_task_extras` - read them from an ``IRegisteredTask``, field by field.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, NamedTuple, TypeVar

from ...domain.enums import TaskRunLevel
from .marshal import com_date_to_datetime, to_task_result, to_task_run_level
from .sid import sid_string

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class TaskExtras(NamedTuple):
    """The principal and [120] run-info fields of one task, as they land on ``ScheduledTaskInfo``."""

    user_id: str
    run_level: TaskRunLevel | None
    last_run_time: datetime | None
    last_task_result: int | None
    next_run_time: datetime | None


def _soft(field: str, read: Callable[[], _T], default: _T) -> _T:
    """Return ``read()``, or ``default`` when it raises (logged at debug, so it is never silent)."""
    try:
        return read()
    except Exception as exc:  # COM errors and NativeCallError alike: one field must not cost the task
        logger.debug("scheduled task %s unreadable, reporting it unknown: %s", field, exc)
        return default


def read_task_extras(task: Any) -> TaskExtras:
    """Read a task's principal SID, run level and run info, each field failing soft on its own.

    Args:
        task: An ``IRegisteredTask`` COM object (or a double with the same attributes).

    Returns:
        The fields; ``user_id`` is ``""`` and the others None where a value cannot be read.

    Example:
        >>> class _Unreadable:
        ...     @property
        ...     def Definition(self):
        ...         raise OSError("access denied")
        >>> read_task_extras(_Unreadable()).user_id
        ''
    """
    # Group principals (GroupId set, UserId empty) read "" too: user_id names a user only.
    return TaskExtras(
        user_id=_soft("principal", lambda: sid_string(str(task.Definition.Principal.UserId or "")), ""),
        run_level=_soft("run level", lambda: to_task_run_level(int(task.Definition.Principal.RunLevel)), None),
        last_run_time=_soft("LastRunTime", lambda: com_date_to_datetime(task.LastRunTime), None),
        last_task_result=_soft("LastTaskResult", lambda: to_task_result(int(task.LastTaskResult)), None),
        next_run_time=_soft("NextRunTime", lambda: com_date_to_datetime(task.NextRunTime), None),
    )


__all__ = ["TaskExtras", "read_task_extras"]
