"""native process control: os_agnostic tests.

stop_process / wait_process drive a real throwaway subprocess (hermetic - only the
process this test spawned is touched). The destructive restart_computer /
stop_computer are tested by MOCKING subprocess.run and asserting the argv - the test
never actually reboots.
"""
# subprocess is used to spawn a disposable child under test; the argv is fixed.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import pwshpy.adapters.native.process_control as pcmod
from pwshpy.adapters.native.process_control import NativeProcessControl
from pwshpy.domain.errors import ConfigurationError, NativeCallError, OperationTimeoutError, PlatformUnsupportedError

#: Windows' own shutdown.exe, spelled out here rather than read from the module under test.
_SHUTDOWN_EXE = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "shutdown.exe")
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
    """[D1]: wait_process raises OperationTimeoutError (a NativeCallError) carrying the limit when the
    process outlives the timeout, like every other call that waits."""
    child = subprocess.Popen(_SLEEP)  # noqa: S603 - fixed argv, disposable child
    try:
        with pytest.raises(OperationTimeoutError) as caught:
            NativeProcessControl().wait_process(child.pid, timeout=0.5)
        assert caught.value.timeout == 0.5
    finally:
        child.kill()
        child.wait()


@pytest.mark.os_agnostic
def test_restart_computer_builds_shutdown_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """restart_computer builds 'shutdown /r /t N /f' - and NEVER actually reboots (mocked).

    [320] The program is System32's shutdown.exe by absolute path (a bare name lets CreateProcess
    take one from the current directory first), and its output is decoded as the OEM code page
    shutdown.exe writes, with replacement, so a localised message never leaves stderr None.
    """
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class _Result:
        returncode = 0
        stderr = ""

    def _fake_run(argv: list[str], **kwargs: Any) -> _Result:
        calls.append((argv, kwargs))
        return _Result()

    monkeypatch.setattr(pcmod.sys, "platform", "win32")
    monkeypatch.setattr(pcmod.subprocess, "run", _fake_run)
    NativeProcessControl().restart_computer(delay_seconds=3, force=True)
    [(argv, kwargs)] = calls
    assert argv == [_SHUTDOWN_EXE, "/r", "/t", "3", "/f"]
    assert argv[0].replace("\\", "/").lower().endswith("/system32/shutdown.exe")
    assert (kwargs["encoding"], kwargs["errors"]) == ("oem", "replace")


@pytest.mark.os_agnostic
def test_shutdown_on_posix_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """restart/stop_computer raise PlatformUnsupportedError off Windows."""
    monkeypatch.setattr(pcmod.sys, "platform", "linux")
    with pytest.raises(PlatformUnsupportedError):
        NativeProcessControl().stop_computer()


@pytest.mark.os_agnostic
@pytest.mark.parametrize("delay", [-1, 315_360_001])
def test_a_delay_shutdown_cannot_take_is_refused_before_it_runs(monkeypatch: pytest.MonkeyPatch, delay: int) -> None:
    """[320] shutdown.exe takes /t 0-315360000 and answers anything else with its usage text, so the
    delay is refused up front, typed, and shutdown is never started."""
    started: list[list[str]] = []

    def _record(argv: list[str], **_kwargs: Any) -> None:
        started.append(argv)

    monkeypatch.setattr(pcmod.sys, "platform", "win32")
    monkeypatch.setattr(pcmod.subprocess, "run", _record)
    with pytest.raises(ConfigurationError, match="delay_seconds must be 0-315360000"):
        NativeProcessControl().restart_computer(delay_seconds=delay)
    assert started == []


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows shutdown.exe")
def test_a_refused_shutdown_is_a_native_call_error_naming_shutdowns_message() -> None:
    """[320] A shutdown.exe refusal is a NativeCallError carrying its (localised) message.

    A second shutdown while one is scheduled is refused on stderr ("... bereits geplant.(1190)" on a
    German Windows). That message decodes in either code page, so this pins the refusal path end to
    end; the OEM decoding itself is pinned by the argv test. Schedules a real shutdown (aborted in
    ``finally``), so it runs on the throwaway VM only.
    """
    control = NativeProcessControl()
    try:
        control.stop_computer(delay_seconds=600, force=False)
        with pytest.raises(NativeCallError, match=r"^shutdown exited [1-9]\d*: \S"):
            control.stop_computer(delay_seconds=600, force=False)
    finally:
        subprocess.run(  # noqa: S603 - fixed argv: abort the shutdown this test scheduled
            [_SHUTDOWN_EXE, "/a"], capture_output=True, check=False
        )
