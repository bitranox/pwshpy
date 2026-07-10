"""Event marshaling: Level ids -> EventLevel, event SystemTime -> datetime (pure, os-agnostic)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pwshpy.adapters.native.marshal import parse_event_time, to_event_level
from pwshpy.domain.enums import EventLevel


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (0, EventLevel.LOG_ALWAYS),
        (1, EventLevel.CRITICAL),
        (2, EventLevel.ERROR),
        (3, EventLevel.WARNING),
        (4, EventLevel.INFORMATION),
        (5, EventLevel.VERBOSE),
        (99, EventLevel.INFORMATION),  # unknown -> INFORMATION
    ],
)
def test_event_level(level: int, expected: EventLevel) -> None:
    """Each win32 event Level maps to its EventLevel; unknown degrades to INFORMATION."""
    assert to_event_level(level) is expected


@pytest.mark.os_agnostic
def test_parse_event_time_truncates_fraction_to_micros() -> None:
    """A 7-digit (100-ns) fraction is truncated to microseconds; Z becomes UTC."""
    assert parse_event_time("2026-07-07T15:03:14.2206901Z") == datetime(
        2026, 7, 7, 15, 3, 14, 220690, tzinfo=timezone.utc
    )


@pytest.mark.os_agnostic
def test_parse_event_time_no_fraction() -> None:
    """A whole-second timestamp parses to an aware UTC datetime."""
    assert parse_event_time("2026-07-07T15:03:14Z") == datetime(2026, 7, 7, 15, 3, 14, tzinfo=timezone.utc)


@pytest.mark.os_agnostic
def test_parse_event_time_preserves_offset() -> None:
    """A non-Z offset is preserved rather than forced to UTC."""
    result = parse_event_time("2026-07-07T17:03:14.220690+02:00")
    assert result is not None
    offset = result.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 7200


@pytest.mark.os_agnostic
@pytest.mark.parametrize("bad", [None, ""])
def test_parse_event_time_empty(bad: str | None) -> None:
    """Missing / empty SystemTime yields None."""
    assert parse_event_time(bad) is None
