"""native psutil disk-usage source — portable, runs on every OS."""

from __future__ import annotations

import pytest

from pwshpy.adapters.native import iter_disks
from pwshpy.domain.records import DiskUsage


@pytest.mark.os_agnostic
def test_iter_disks_yields_disk_usage_records() -> None:
    """Each yielded item is a typed DiskUsage with coherent numbers."""
    disks = list(iter_disks())
    assert disks, "expected at least one mounted filesystem"
    for disk in disks:
        assert isinstance(disk, DiskUsage)
        assert disk.total >= 0
        assert 0.0 <= disk.percent <= 100.0
        # Reserved blocks mean used + free need not equal total; but neither can
        # individually exceed it.
        assert disk.free <= disk.total
        assert disk.used <= disk.total


@pytest.mark.os_agnostic
def test_iter_disks_covers_a_real_mountpoint() -> None:
    """At least one record has a non-empty device and mountpoint."""
    assert any(disk.device and disk.mountpoint for disk in iter_disks())
