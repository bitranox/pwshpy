"""Typed records: field defaults, immutability, JSON serialization, model_construct."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pwshpy.domain.enums import ProcessStatus
from pwshpy.domain.records import ProcessInfo, PSRecord


@pytest.mark.os_agnostic
def test_process_info_defaults() -> None:
    """Optional fields default to None / UNKNOWN."""
    proc = ProcessInfo(pid=1, name="init")
    assert proc.ppid is None
    assert proc.status is ProcessStatus.UNKNOWN
    assert proc.username is None
    assert proc.memory_rss is None
    assert proc.create_time is None


@pytest.mark.os_agnostic
def test_process_info_is_frozen() -> None:
    """Records are immutable value objects."""
    proc = ProcessInfo(pid=1, name="init")
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError on frozen setattr
        proc.pid = 2  # type: ignore[misc]


@pytest.mark.os_agnostic
def test_to_dict_is_json_serializable() -> None:
    """to_dict renders datetime as ISO string and enum as its value."""
    created = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    proc = ProcessInfo(pid=9, name="svc", status=ProcessStatus.RUNNING, create_time=created)
    data = proc.to_dict()
    assert data["status"] == "running"
    assert data["create_time"] == "2020-01-02T03:04:05Z"
    assert data["pid"] == 9


@pytest.mark.os_agnostic
def test_model_construct_skips_validation() -> None:
    """model_construct builds a record on the hot path without validation."""
    proc = ProcessInfo.model_construct(pid=5, name="fast", status=ProcessStatus.SLEEPING)
    assert proc.pid == 5
    assert proc.status is ProcessStatus.SLEEPING
    # Fields not supplied fall back to their declared defaults.
    assert proc.username is None


@pytest.mark.os_agnostic
def test_psrecord_base_to_dict() -> None:
    """Arbitrary PSRecord subclasses serialize their own fields."""

    class Point(PSRecord):
        x: int
        y: int

    assert Point(x=1, y=2).to_dict() == {"x": 1, "y": 2}
