"""Task-state marshaling: Task Scheduler ``TASK_STATE`` ids -> TaskState (pure, os-agnostic)."""

from __future__ import annotations

import pytest

from pwshpy.adapters.native.marshal import to_task_state
from pwshpy.domain.enums import TaskState


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (0, TaskState.UNKNOWN),
        (1, TaskState.DISABLED),
        (2, TaskState.QUEUED),
        (3, TaskState.READY),
        (4, TaskState.RUNNING),
        (99, TaskState.UNKNOWN),  # unknown -> UNKNOWN
    ],
)
def test_task_state(state: int, expected: TaskState) -> None:
    """Each TASK_STATE id maps to its TaskState; unknown degrades to UNKNOWN."""
    assert to_task_state(state) is expected
