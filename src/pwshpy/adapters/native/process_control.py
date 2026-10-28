"""native process control - stop/wait for processes and restart/stop the computer.

``stop_process`` / ``wait_process`` are portable (``psutil``).  ``restart_computer`` /
``stop_computer`` shell out to Windows ``shutdown`` and are **destructive** - they are
Windows-only and default to ``force=True``; never call them on a machine you care about.

Contents:
    * :class:`NativeProcessControl` - the process/computer control operations (**mutating**).
"""

from __future__ import annotations

import subprocess  # nosec B404 - used only to invoke the Windows shutdown command with a fixed argv
import sys
from typing import Any

import psutil as _psutil_module  # pyright: ignore[reportMissingTypeStubs]

from ...domain.errors import NativeCallError, OperationTimeoutError, PlatformUnsupportedError
from .marshal import native_error

_psutil: Any = _psutil_module
_NO_SUCH_PROCESS: type[BaseException] = _psutil.NoSuchProcess
_ACCESS_DENIED: type[BaseException] = _psutil.AccessDenied
_TIMEOUT_EXPIRED: type[BaseException] = _psutil.TimeoutExpired


class NativeProcessControl:
    """Stop/wait for processes (portable) and restart/stop the computer (Windows, destructive).

    Example:
        >>> pc = NativeProcessControl()
        >>> callable(pc.stop_process)
        True
    """

    def stop_process(self, pid: int, *, force: bool = False) -> None:
        """Terminate a process by PID (``force`` sends a hard kill) (like Stop-Process) (**mutating**)."""
        try:
            proc = _psutil.Process(pid)
        except _NO_SUCH_PROCESS as exc:
            raise native_error(f"no process with PID {pid}", exc) from exc
        try:
            proc.kill() if force else proc.terminate()
        except (_NO_SUCH_PROCESS, _ACCESS_DENIED, OSError) as exc:
            raise native_error(f"cannot stop PID {pid}: {exc}", exc) from exc

    def wait_process(self, pid: int, *, timeout: float | None = None) -> int | None:
        """Wait for a process to exit; return its exit code (like Wait-Process).

        A PID that is already absent counts as exited (its code is unknowable, so ``None``);
        this makes ``stop_process`` then ``wait_process`` race-free - the OS may reap the
        process between the two calls.

        Raises:
            OperationTimeoutError: A still-running process did not exit within ``timeout``
                seconds (a :class:`~pwshpy.domain.errors.NativeCallError`, carrying the limit).
        """
        try:
            proc = _psutil.Process(pid)
        except _NO_SUCH_PROCESS:
            return None  # already gone -> "wait for it to exit" is trivially satisfied
        try:
            code = proc.wait(timeout)
        except _TIMEOUT_EXPIRED as exc:
            # psutil raises TimeoutExpired only when a limit was given, so the 0.0 is never reported.
            limit = 0.0 if timeout is None else float(timeout)
            raise OperationTimeoutError(f"PID {pid} did not exit within {timeout}s", timeout=limit) from exc
        except _NO_SUCH_PROCESS:
            return None  # reaped mid-wait
        return None if code is None else int(code)

    def restart_computer(self, *, delay_seconds: int = 0, force: bool = True) -> None:
        """Restart the computer (Windows ``shutdown /r``) (**destructive**, Windows-only)."""
        self._shutdown("/r", delay_seconds, force=force)

    def stop_computer(self, *, delay_seconds: int = 0, force: bool = True) -> None:
        """Shut the computer down (Windows ``shutdown /s``) (**destructive**, Windows-only)."""
        self._shutdown("/s", delay_seconds, force=force)

    def _shutdown(self, flag: str, delay_seconds: int, *, force: bool) -> None:
        if sys.platform != "win32":
            raise PlatformUnsupportedError("restart_computer/stop_computer use Windows 'shutdown'; use sudo on POSIX.")
        argv = ["shutdown", flag, "/t", str(delay_seconds)]
        if force:
            argv.append("/f")
        try:
            completed = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv (shutdown), no shell
                argv, capture_output=True, text=True, check=False
            )
        except OSError as exc:
            raise native_error(f"shutdown failed: {exc}", exc) from exc
        if completed.returncode != 0:
            raise NativeCallError(f"shutdown exited {completed.returncode}: {completed.stderr.strip()}")


__all__ = ["NativeProcessControl"]
