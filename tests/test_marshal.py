"""Tier-A marshaling seam: epoch conversion and process-status mapping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pwshpy.adapters.native.marshal import epoch_to_datetime, to_process_status
from pwshpy.domain.enums import ProcessStatus


@pytest.mark.os_agnostic
def test_epoch_to_datetime_is_utc_aware() -> None:
    """A POSIX timestamp becomes an aware UTC datetime."""
    result = epoch_to_datetime(0)
    assert result == datetime(1970, 1, 1, tzinfo=UTC)
    assert result is not None and result.tzinfo is UTC


@pytest.mark.os_agnostic
def test_epoch_to_datetime_passes_none_through() -> None:
    """None marshals to None."""
    assert epoch_to_datetime(None) is None


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("running", ProcessStatus.RUNNING),
        ("sleeping", ProcessStatus.SLEEPING),
        ("disk-sleep", ProcessStatus.DISK_SLEEP),
    ],
)
def test_to_process_status_maps_known(raw: str, expected: ProcessStatus) -> None:
    """Known status strings map to the matching enum member."""
    assert to_process_status(raw) is expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize("raw", ["totally-new-state", "", None])
def test_to_process_status_falls_back_to_unknown(raw: str | None) -> None:
    """Unknown or missing statuses fall back to UNKNOWN, never raising."""
    assert to_process_status(raw) is ProcessStatus.UNKNOWN
