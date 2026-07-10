"""native process control: os_agnostic tests.

stop_process / wait_process drive a real throwaway subprocess (hermetic - only the
process this test spawned is touched). The destructive restart_computer /
stop_computer are tested by MOCKING subprocess.run and asserting the argv - the test
never actually reboots.
"""
# subprocess is used to spawn a disposable child under test; the argv is fixed.

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

import pwshpy.adapters.native.process_control as pcmod
from pwshpy.adapters.native.process_control import NativeProcessControl
from pwshpy.domain.errors import NativeCallError, PlatformUnsupportedError

_SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


@pytest.mark.os_agnostic
def test_stop_and_wait_process() -> None:
    """stop_process kills a spawned child; wait_process returns once it exits (code or None if reaped)."""
    child = subprocess.Popen(_SLEEP)  # noqa: S603 - fixed argv, disposable child
    pc = NativeProcessControl()
    pc.stop_process(child.pid, force=True)
    code = pc.wait_process(child.pid, timeout=10.0)  # returns without raising; None if the OS already reaped it
    assert code is None or isinstance(code, int)
    child.wait()


@pytest.mark.os_agnostic
def test_wait_missing_process_returns_none() -> None:
    """wait_process on an absent PID returns None (already exited), never raises."""
    assert NativeProcessControl().wait_process(2_000_000_000) is None


@pytest.mark.os_agnostic
def test_stop_missing_process_raises() -> None:
    """Stopping a non-existent PID raises NativeCallError."""
    with pytest.raises(NativeCallError):
        NativeProcessControl().stop_process(2_000_000_000)


@pytest.mark.os_agnostic
def test_wait_timeout_raises() -> None:
    """wait_process raises NativeCallError when the process outlives the timeout."""
    child = subprocess.Popen(_SLEEP)  # noqa: S603 - fixed argv, disposable child
    try:
        with pytest.raises(NativeCallError):
            NativeProcessControl().wait_process(child.pid, timeout=0.5)
    finally:
        child.kill()
        child.wait()


@pytest.mark.os_agnostic
def test_restart_computer_builds_shutdown_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """restart_computer builds 'shutdown /r /t N /f' - and NEVER actually reboots (mocked)."""
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stderr = ""

    def _fake_run(argv: list[str], **_kwargs: Any) -> _Result:
        calls.append(argv)
        return _Result()

    monkeypatch.setattr(pcmod.sys, "platform", "win32")
    monkeypatch.setattr(pcmod.subprocess, "run", _fake_run)
    NativeProcessControl().restart_computer(delay_seconds=3, force=True)
    assert calls == [["shutdown", "/r", "/t", "3", "/f"]]


@pytest.mark.os_agnostic
def test_shutdown_on_posix_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """restart/stop_computer raise PlatformUnsupportedError off Windows."""
    monkeypatch.setattr(pcmod.sys, "platform", "linux")
    with pytest.raises(PlatformUnsupportedError):
        NativeProcessControl().stop_computer()
