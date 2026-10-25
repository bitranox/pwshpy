"""Fail-soft read-back of a Task Scheduler task's principal, run info and settings (Windows; COM).

Shared by the read-only listing and the mutating controller so both report the same
fields the same way. Each field is read on its own: a principal that no longer resolves
to a SID (a deleted account, an unmappable name), a run-info property COM refuses, or a
setting it refuses or hands back in a form with no domain meaning degrades THAT field to
its "unknown" value. It never drops the task from a listing, and it never turns a verb
that already succeeded into a failure.

Contents:
    * :class:`TaskExtras` - the principal, [120] run-info and [45] settings fields of one task.
    * :func:`read_task_extras` - read them from an ``IRegisteredTask``, field by field.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, NamedTuple, TypeVar

from ...domain.enums import TaskRunLevel
from ...domain.scheduling import TaskSettings
from .marshal import (
    com_date_to_datetime,
    to_task_duration,
    to_task_multiple_instances,
    to_task_result,
    to_task_run_level,
)
from .sid import sid_string

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class TaskExtras(NamedTuple):
    """The principal, [120] run-info and [45] settings fields of one task, as they land on ``ScheduledTaskInfo``."""

    user_id: str
    run_level: TaskRunLevel | None
    last_run_time: datetime | None
    last_task_result: int | None
    next_run_time: datetime | None
    settings: TaskSettings


def _soft(field: str, read: Callable[[], _T], default: _T) -> _T:
    """Return ``read()``, or ``default`` when it raises (logged at debug, so it is never silent)."""
    try:
        return read()
    except Exception as exc:  # COM errors and NativeCallError alike: one field must not cost the task
        logger.debug("scheduled task %s unreadable, reporting it unknown: %s", field, exc)
        return default


def _com_bool(value: object) -> bool:
    """``value`` when COM handed back a real bool; anything else has no reading and raises."""
    if not isinstance(value, bool):
        raise TypeError(f"expected a COM bool, got {type(value).__name__}")
    return value


def _read_settings(definition: Any) -> TaskSettings:
    """Read the effective ITaskSettings values of a task definition, each field failing soft on its own.

    When ``definition`` (None for an unreadable one) or its ``Settings`` cannot be read, every
    field reads None.
    """
    settings: Any = _soft("settings", lambda: definition.Settings, None)
    # model_construct: the values come from the scheduler, not a caller, and the input
    # validator's bounds describe what can be WRITTEN, not every value a task can hold.
    return TaskSettings.model_construct(
        allow_start_if_on_batteries=_soft(
            "DisallowStartIfOnBatteries", lambda: not _com_bool(settings.DisallowStartIfOnBatteries), None
        ),
        stop_if_going_on_batteries=_soft(
            "StopIfGoingOnBatteries", lambda: _com_bool(settings.StopIfGoingOnBatteries), None
        ),
        execution_time_limit=_soft(
            "ExecutionTimeLimit", lambda: to_task_duration(str(settings.ExecutionTimeLimit)), None
        ),
        multiple_instances=_soft(
            "MultipleInstances", lambda: to_task_multiple_instances(int(settings.MultipleInstances)), None
        ),
        allow_hard_terminate=_soft("AllowHardTerminate", lambda: _com_bool(settings.AllowHardTerminate), None),
        allow_demand_start=_soft("AllowDemandStart", lambda: _com_bool(settings.AllowDemandStart), None),
    )


def read_task_extras(task: Any) -> TaskExtras:
    """Read a task's principal SID, run level, run info and settings, each field failing soft on its own.

    Args:
        task: An ``IRegisteredTask`` COM object (or a double with the same attributes).

    Returns:
        The fields; ``user_id`` is ``""`` and the others None where a value cannot be read.
        ``settings`` is always a :class:`TaskSettings`, with None in each field that cannot be
        read or has no domain meaning (an unknown policy number, a duration in years).

    Example:
        >>> class _Unreadable:
        ...     @property
        ...     def Definition(self):
        ...         raise OSError("access denied")
        >>> extras = read_task_extras(_Unreadable())
        >>> extras.user_id, extras.settings == TaskSettings()
        ('', True)
    """
    # One fetch: each Definition access is a COM call that hands back a freshly parsed task
    # definition, costing far more than every property read after it (measured on Windows 11,
    # 2026-10-25). None when it cannot be read, so each field below degrades on its own.
    definition: Any = _soft("definition", lambda: task.Definition, None)
    # Group principals (GroupId set, UserId empty) read "" too: user_id names a user only.
    return TaskExtras(
        user_id=_soft("principal", lambda: sid_string(str(definition.Principal.UserId or "")), ""),
        run_level=_soft("run level", lambda: to_task_run_level(int(definition.Principal.RunLevel)), None),
        last_run_time=_soft("LastRunTime", lambda: com_date_to_datetime(task.LastRunTime), None),
        last_task_result=_soft("LastTaskResult", lambda: to_task_result(int(task.LastTaskResult)), None),
        next_run_time=_soft("NextRunTime", lambda: com_date_to_datetime(task.NextRunTime), None),
        settings=_read_settings(definition),
    )


__all__ = ["TaskExtras", "read_task_extras"]
