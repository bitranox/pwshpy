"""native inventory: os_agnostic get_computer_info + a Windows get_hotfix oracle."""

from __future__ import annotations

import sys

import pytest

from pwshpy.adapters.native.inventory import get_computer_info, iter_hotfixes
from pwshpy.domain.records import ComputerInfo, Hotfix


@pytest.mark.os_agnostic
def test_get_computer_info_is_a_summary() -> None:
    """get_computer_info returns a populated ComputerInfo on every OS."""
    info = get_computer_info()
    assert isinstance(info, ComputerInfo)
    assert info.hostname
    assert info.os_name
    assert info.cpu_count is None or info.cpu_count >= 1
    assert info.total_memory_bytes is None or info.total_memory_bytes > 0


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Get-Hotfix (Win32_QuickFixEngineering) is Windows-only")
def test_get_hotfix_yields_typed_records() -> None:
    """On Windows, installed updates marshal into Hotfix records with a HotFixID."""
    hotfixes = list(iter_hotfixes())
    assert all(isinstance(h, Hotfix) for h in hotfixes)
    assert all(h.hotfix_id for h in hotfixes)
