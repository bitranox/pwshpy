"""native process control - stop/wait for processes and restart/stop the computer.

``stop_process`` / ``wait_process`` are portable (``psutil``).  ``restart_computer`` /
``stop_computer`` shell out to Windows ``shutdown`` and are **destructive** - they are
Windows-only and default to ``force=True``; never call them on a machine you care about.

Contents:
    * :class:`NativeProcessControl` - the process/computer control operations (**mutating**).
"""

from __future__ import annotations

import ctypes
import ntpath
import subprocess  # nosec B404 - used only to invoke the Windows shutdown command with a fixed argv
import sys
from collections.abc import Callable
from typing import Any

import psutil as _psutil_module  # pyright: ignore[reportMissingTypeStubs]

from ...domain.errors import ConfigurationError, NativeCallError, OperationTimeoutError, PlatformUnsupportedError
from .marshal import native_error

#: shutdown.exe's largest ``/t`` (ten years), from its own usage text.
_MAX_DELAY_SECONDS = 315_360_000

#: Characters GetSystemDirectoryW is first offered; it reports the size it needs when that is short.
_SYSTEM_DIRECTORY_BUFFER = 260

_psutil: Any = _psutil_module
_NO_SUCH_PROCESS: type[BaseException] = _psutil.NoSuchProcess
_ACCESS_DENIED: type[BaseException] = _psutil.AccessDenied
_TIMEOUT_EXPIRED: type[BaseException] = _psutil.TimeoutExpired


class NativeProcessControl:
    """Stop/wait for processes (portable) and restart/stop the computer (Windows, destructive).

    Args:
        system_directory: Returns the directory ``shutdown.exe`` is started from; defaults to
            :func:`windows_system_directory` (what Windows reports, never an environment variable).

    Example:
        >>> pc = NativeProcessControl()
        >>> callable(pc.stop_process)
        True
    """

    def __init__(self, *, system_directory: Callable[[], str] | None = None) -> None:
        self._system_directory = windows_system_directory if system_directory is None else system_directory

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

    def _shutdown(self, flag: str, delay_seconds: object, *, force: bool) -> None:
        # delay_seconds is typed object here because a library caller can pass anything, and a bool
        # is an int to isinstance: True would pass the range check and schedule /t 1.
        if sys.platform != "win32":
            raise PlatformUnsupportedError("restart_computer/stop_computer use Windows 'shutdown'; use sudo on POSIX.")
        if (
            isinstance(delay_seconds, bool)
            or not isinstance(delay_seconds, int)
            or not 0 <= delay_seconds <= _MAX_DELAY_SECONDS
        ):
            # shutdown.exe answers an out-of-range /t with its whole usage text on stdout and exit 1.
            raise ConfigurationError(
                f"delay_seconds must be 0-{_MAX_DELAY_SECONDS} whole seconds as an int (shutdown /t), "
                f"got {delay_seconds!r}"
            )
        argv = [self._shutdown_exe(), flag, "/t", str(delay_seconds)]
        if force:
            argv.append("/f")
        try:
            # shutdown.exe writes its (localised) messages in the OEM code page; the locale default
            # (ANSI) cannot decode e.g. a German umlaut, which kills the reader thread and leaves
            # stderr None. "oem" exists only on Windows, which the guard above has established.
            completed = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv (shutdown), no shell
                argv, capture_output=True, text=True, encoding="oem", errors="replace", check=False
            )
        except OSError as exc:
            raise native_error(f"shutdown failed: {exc}", exc) from exc
        if completed.returncode != 0:
            raise NativeCallError(f"shutdown exited {completed.returncode}: {completed.stderr.strip()}")

    def _shutdown_exe(self) -> str:
        # A bare "shutdown", or a path relative to anything, lets CreateProcess take a shutdown.exe
        # from the current directory first; a path built from SYSTEMROOT is relative when the
        # variable is empty and a guess when it is unset, so the directory comes from Windows.
        directory = self._system_directory()
        if not _is_absolute_windows_path(directory):
            raise NativeCallError(f"the Windows system directory {directory!r} is not an absolute path")
        return ntpath.join(directory, "shutdown.exe")


def windows_system_directory() -> str:
    """Return the Windows system directory (System32) as ``GetSystemDirectoryW`` reports it.

    Returns:
        The directory, e.g. ``C:\\Windows\\system32``.

    Raises:
        PlatformUnsupportedError: Not Windows.
        NativeCallError: ``GetSystemDirectoryW`` failed (carrying its Win32 code).
    """
    if sys.platform != "win32":
        raise PlatformUnsupportedError("the Windows system directory exists only on Windows")
    get_system_directory = ctypes.WinDLL("kernel32", use_last_error=True).GetSystemDirectoryW
    get_system_directory.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    get_system_directory.restype = ctypes.c_uint
    size = _SYSTEM_DIRECTORY_BUFFER
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length: int = get_system_directory(buffer, size)
        if length == 0:
            error = ctypes.WinError(ctypes.get_last_error())
            raise native_error(f"GetSystemDirectoryW failed: {error}", error) from error
        if length < size:
            return buffer.value
        size = length  # too short: length is the size needed, terminating NUL included


def _is_absolute_windows_path(path: str) -> bool:
    """Whether ``path`` is drive-qualified or UNC and rooted, judged the same on every OS.

    ``ntpath.isabs`` is not used: it calls a rooted path with no drive (``\\Windows``) absolute
    before Python 3.13 and not from 3.13 on, and such a path is relative to the current drive.

    Example:
        >>> [_is_absolute_windows_path(p) for p in (r"C:\\Windows", r"\\\\srv\\share\\x")]
        [True, True]
        >>> [_is_absolute_windows_path(p) for p in ("System32", r"C:Windows", r"\\Windows", "")]
        [False, False, False, False]
    """
    drive, rest = ntpath.splitdrive(path)
    return bool(drive) and rest.startswith(("\\", "/"))


__all__ = ["NativeProcessControl", "windows_system_directory"]
