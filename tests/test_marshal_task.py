"""Task Scheduler marshaling: TASK_STATE, TASK_RUNLEVEL, COM dates, LastTaskResult and settings (pure, os-agnostic)."""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from pwshpy.adapters.native.marshal import (
    com_date_to_datetime,
    from_task_multiple_instances,
    to_task_duration,
    to_task_multiple_instances,
    to_task_result,
    to_task_run_level,
    to_task_state,
)
from pwshpy.domain.enums import TaskMultipleInstances, TaskRunLevel, TaskState


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


# What a real Task Scheduler hands back through pywin32 (measured on Windows 11 25H2, a throwaway Windows VM,
# 2026-10-21, host zone CEST): a VT_DATE carries LOCAL wall time, and pywin32 labels it with a
# UTC-like TimeZoneInfo('GMT Standard Time'), so the label must be ignored. A never-run task reads
# LastRunTime 1999-11-30 00:00 (Get-ScheduledTaskInfo shows the same date); "no next run" is the
# VT_DATE zero 1899-12-30.
@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "value",
    [
        None,
        datetime(1899, 12, 30),  # NextRunTime: no next run (VT_DATE zero)
        datetime(1899, 12, 30, tzinfo=timezone.utc),  # the same, as pywin32 labels it
        datetime(1999, 11, 30),  # LastRunTime of a task that never ran
        datetime(1999, 11, 30, tzinfo=timezone.utc),  # the same, as pywin32 labels it
    ],
)
def test_com_date_never_sentinels_read_none(value: datetime | None) -> None:
    """The measured "never" dates map to None, whatever tzinfo pywin32 attached."""
    assert com_date_to_datetime(value) is None


@pytest.mark.os_agnostic
def test_com_date_is_local_wall_time_whatever_the_label() -> None:
    """The tzinfo pywin32 attaches is discarded: the wall time is read as local time.

    Two different labels on the same wall time must give the same instant; with the label
    honoured they would differ by two hours on every host.
    """
    as_utc = com_date_to_datetime(datetime(2026, 10, 21, 18, 53, 12, tzinfo=timezone.utc))
    as_plus2 = com_date_to_datetime(datetime(2026, 10, 21, 18, 53, 12, tzinfo=timezone(timedelta(hours=2))))
    assert as_utc == as_plus2 == datetime(2026, 10, 21, 18, 53, 12).astimezone(timezone.utc)


@pytest.fixture
def vienna_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the process zone to Europe/Vienna, restoring the previous zone afterwards."""
    # A sys.platform guard, not hasattr: pyright narrows on sys.platform, so the Windows
    # type-check run does not see a call to a function its stubs do not define there.
    if sys.platform == "win32":
        pytest.skip("time.tzset is POSIX-only")
    monkeypatch.setenv("TZ", "Europe/Vienna")
    time.tzset()
    yield
    monkeypatch.undo()  # restore TZ before re-reading it, or tzset would keep Vienna
    time.tzset()


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="time.tzset is POSIX-only")
@pytest.mark.usefixtures("vienna_tz")
@pytest.mark.parametrize(
    ("wall", "expected"),
    [
        (datetime(2026, 10, 21, 18, 53, 12), datetime(2026, 10, 21, 16, 53, 12, tzinfo=timezone.utc)),  # CEST
        (datetime(2026, 1, 15, 12, 0), datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)),  # CET
    ],
)
def test_com_date_reads_the_wall_time_in_the_host_zone(wall: datetime, expected: datetime) -> None:
    """Fixed instants under a fixed zone, so a UTC CI host still catches a read-the-wall-time-as-UTC regression."""
    assert com_date_to_datetime(wall.replace(tzinfo=timezone.utc)) == expected


@pytest.mark.os_agnostic
def test_com_date_to_datetime_labels_the_result_utc() -> None:
    """The result is labelled UTC (equality alone would accept any offset naming the same instant)."""
    result = com_date_to_datetime(datetime(2026, 10, 21, 14, 0, tzinfo=timezone(timedelta(hours=2))))
    assert result is not None
    assert result.tzinfo is timezone.utc


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("value", "expected"),
    # last: a negative HRESULT read unsigned
    [(0, 0), (1, 1), (0x41303, None), (0x41301, None), (-2147216609, 0x8004131F)],
)
def test_to_task_result(value: int, expected: int | None) -> None:
    """LastTaskResult: has-not-run and still-running -> None, an HRESULT -> its unsigned value, 0 stays 0.

    0x41301 (SCHED_S_TASK_RUNNING) is what a real Task Scheduler reports while the task runs
    (measured on a throwaway Windows VM): no result exists yet, so it is not a result code.
    """
    assert to_task_result(value) == expected


# What Task Scheduler hands back for ExecutionTimeLimit (measured on Windows 11, 2026-10-25): it
# normalises what it stores, so PT90S reads back PT1M30S, 2147483647 s reads back
# P24855DT3H14M7S, an empty string reads back PT72H, and PT0S ("no limit") round-trips.
@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PT0S", timedelta(0)),
        ("PT1M30S", timedelta(seconds=90)),
        ("P24855DT3H14M7S", timedelta(seconds=2147483647)),
        ("PT72H", timedelta(hours=72)),
        ("P3D", timedelta(days=3)),
        ("PT259200S", timedelta(hours=72)),  # the un-normalised form pwshpy writes
        ("P1DT2H", timedelta(days=1, hours=2)),
    ],
)
def test_to_task_duration_reads_the_normalised_forms(value: str, expected: timedelta) -> None:
    """Days, hours, minutes and whole seconds in any combination Task Scheduler stores."""
    assert to_task_duration(value) == expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("P", id="no-component"),
        pytest.param("PT", id="time-designator-only"),
        pytest.param("P1DT", id="dangling-time-designator"),
        pytest.param("PT1.5S", id="fractional-seconds"),
        pytest.param("P1Y", id="years-are-ambiguous"),
        pytest.param("P1M", id="months-are-ambiguous"),
        pytest.param("P1W", id="weeks"),
        pytest.param("-PT1S", id="negative"),
        pytest.param("PT1S\n", id="trailing-newline"),
        pytest.param("pt1s", id="lowercase"),
        pytest.param("PT" + chr(0x0662) + "S", id="non-ascii-digit"),
        pytest.param("P9999999999D", id="beyond-timedelta"),
        pytest.param("PT" + "9" * 5000 + "S", id="huge-digit-run"),
        pytest.param("None", id="com-null-rendered"),
    ],
)
def test_to_task_duration_degrades_an_unparseable_value_to_none(value: str) -> None:
    """A form the parser does not know reads None (unknown), never an exception and never a guess."""
    assert to_task_duration(value) is None


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, TaskMultipleInstances.PARALLEL),
        (1, TaskMultipleInstances.QUEUE),
        (2, TaskMultipleInstances.IGNORE_NEW),
        (3, TaskMultipleInstances.STOP_EXISTING),
        (4, None),
        (-1, None),
    ],
)
def test_to_task_multiple_instances(value: int, expected: TaskMultipleInstances | None) -> None:
    """TASK_INSTANCES_POLICY 0-3 maps to its member; any other number is unknown (None)."""
    assert to_task_multiple_instances(value) is expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize("policy", list(TaskMultipleInstances))
def test_from_task_multiple_instances_round_trips(policy: TaskMultipleInstances) -> None:
    """Every member writes the number it reads back from."""
    assert to_task_multiple_instances(from_task_multiple_instances(policy)) is policy
