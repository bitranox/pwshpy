"""Task Scheduler marshaling: TASK_STATE, TASK_RUNLEVEL, COM dates and LastTaskResult (pure, os-agnostic)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pwshpy.adapters.native.marshal import com_date_to_datetime, to_task_result, to_task_run_level, to_task_state
from pwshpy.domain.enums import TaskRunLevel, TaskState


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


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, TaskRunLevel.LIMITED), (1, TaskRunLevel.HIGHEST)],
)
def test_to_task_run_level(value: int, expected: TaskRunLevel) -> None:
    """TASK_RUNLEVEL 0 is the filtered token (LIMITED), 1 the elevated one (HIGHEST)."""
    assert to_task_run_level(value) is expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (datetime(1899, 12, 30), None),  # VT_DATE zero: never
        (datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc), datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)),
        (
            datetime(2026, 10, 21, 14, 0, tzinfo=timezone(timedelta(hours=2))),
            datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_com_date_to_datetime(value: datetime | None, expected: datetime | None) -> None:
    """A COM date maps to aware UTC; the VT_DATE zero maps to None."""
    assert com_date_to_datetime(value) == expected


@pytest.mark.os_agnostic
def test_com_date_to_datetime_labels_the_result_utc() -> None:
    """The result is labelled UTC (equality alone would accept any offset naming the same instant)."""
    result = com_date_to_datetime(datetime(2026, 10, 21, 14, 0, tzinfo=timezone(timedelta(hours=2))))
    assert result is not None
    assert result.tzinfo is timezone.utc


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (1, 1), (0x41303, None), (-2147216609, 0x8004131F)],  # last: a negative HRESULT read unsigned
)
def test_to_task_result(value: int, expected: int | None) -> None:
    """LastTaskResult: has-not-run -> None, an HRESULT -> its unsigned 32-bit value, 0 stays 0."""
    assert to_task_result(value) == expected
