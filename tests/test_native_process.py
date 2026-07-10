"""native psutil process source — portable, runs on every OS."""

from __future__ import annotations

import os

import pytest

from pwshpy.adapters.native import iter_processes
from pwshpy.domain.records import ProcessInfo


@pytest.mark.os_agnostic
def test_iter_processes_yields_process_info() -> None:
    """Each yielded item is a typed ProcessInfo record."""
    first = next(iter_processes())
    assert isinstance(first, ProcessInfo)
    assert first.pid >= 0
    assert isinstance(first.name, str)


@pytest.mark.os_agnostic
def test_iter_processes_includes_current_process() -> None:
    """The current interpreter process appears in the listing."""
    pids = {proc.pid for proc in iter_processes()}
    assert os.getpid() in pids
