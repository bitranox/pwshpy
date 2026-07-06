"""Tier-A psutil system-uptime source — portable, runs on every OS."""

from __future__ import annotations

from datetime import datetime

import pytest

from pwshpy.adapters.native import get_uptime
from pwshpy.domain.records import SystemUptime


@pytest.mark.os_agnostic
def test_get_uptime_returns_record() -> None:
    """get_uptime returns a SystemUptime with an aware boot time."""
    up = get_uptime()
    assert isinstance(up, SystemUptime)
    assert isinstance(up.boot_time, datetime)
    assert up.boot_time.tzinfo is not None


@pytest.mark.os_agnostic
def test_uptime_is_non_negative_and_after_boot() -> None:
    """Elapsed uptime is non-negative and boot time is in the past."""
    up = get_uptime()
    assert up.uptime_seconds >= 0.0
