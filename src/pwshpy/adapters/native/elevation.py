"""native elevation - check for and acquire administrative (elevated) privileges.

PowerShell makes elevation pure boilerplate: build a ``WindowsPrincipal``, test
``IsInRole('Administrator')``, and on failure ``Start-Process -Verb RunAs`` to
relaunch yourself - while remembering the elevated child starts in ``System32``,
loses your working directory, and loses your arguments.  This module makes it one
call.

- :func:`is_elevated` is **cross-platform**: Windows token-elevation via
  ``win32security``; POSIX ``os.geteuid() == 0``.
- :func:`elevate` relaunches the current process elevated, **preserving the working
  directory and forwarding argv**.  It is a no-op when already elevated.  Windows uses
  UAC (``ShellExecuteEx`` ``"runas"``, fixing PowerShell's ``System32`` gotcha); POSIX
  (Linux/macOS) re-execs the command under ``sudo``.

``win32`` modules are imported lazily so a portable install stays clean.

Contents:
    * :func:`is_elevated` - whether this process has administrative rights.
    * :func:`elevate` - relaunch the current process elevated, forwarding argv/cwd.
"""

from __future__ import annotations

import importlib
import os
import subprocess  # nosec B404 - argv quoting (list2cmdline) + the sudo re-exec on POSIX; always a fixed argv, no shell
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...domain.errors import NativeCallError, PlatformUnsupportedError


def is_elevated() -> bool:
    """Return whether the current process is running with administrative rights.

    Cross-platform: on Windows this reads the process token's elevation flag; on
    POSIX it is ``os.geteuid() == 0``.

    Example:
        >>> isinstance(is_elevated(), bool)
        True
    """
    if sys.platform == "win32":
        return _is_elevated_windows()
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _is_elevated_windows() -> bool:
    """Read the current process token's ``TokenElevation`` flag via ``win32security``."""
    win32api = _load("win32api")
    win32con = _load("win32con")
    win32security = _load("win32security")
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    except Exception as exc:  # pywintypes.error - keep the PwshPyError invariant
        raise NativeCallError(f"OpenProcessToken failed: {exc}") from exc
    try:
        return bool(win32security.GetTokenInformation(token, win32security.TokenElevation))
    except Exception as exc:
        raise NativeCallError(f"GetTokenInformation(TokenElevation) failed: {exc}") from exc
    finally:
        win32api.CloseHandle(token)


def elevate(
    argv: Sequence[str] | None = None,
    *,
    executable: str | None = None,
    cwd: str | None = None,
    wait: bool = True,
) -> int | None:
    """Relaunch the current process elevated, forwarding argv and cwd (portable).

    A no-op returning ``0`` when already elevated.  Windows triggers a UAC prompt via
    ``ShellExecuteEx`` ``"runas"`` (the elevated child inherits the given/current working
    directory - fixing PowerShell's ``System32`` gotcha).  POSIX (Linux/macOS) re-execs the
    command under ``sudo`` (password prompt on the TTY).  ``argv`` defaults to the current
    process's.

    Args:
        argv: Argument vector for the relaunched process; defaults to ``sys.argv``.
        executable: Program to run elevated; defaults to the current interpreter
            (or, for a frozen/console-script ``.exe``, that executable).
        cwd: Working directory for the child; defaults to the current directory.
        wait: When ``True`` (default), wait for the child and return its exit code;
            when ``False``, return ``None`` immediately.

    Returns:
        ``0`` if already elevated; the child's exit code when ``wait`` is ``True``;
        ``None`` when ``wait`` is ``False``.

    Raises:
        NativeCallError: If the elevated relaunch (``ShellExecuteEx`` / ``sudo``) fails.

    Example:
        >>> callable(elevate)
        True
    """
    if is_elevated():
        return 0
    resolved_exe, resolved_argv = _relaunch_target(executable, argv)
    directory = cwd or str(Path.cwd())
    if sys.platform == "win32":
        return _shell_execute_runas(resolved_exe, resolved_argv, directory, wait=wait)
    return _sudo_relaunch(resolved_exe, resolved_argv, directory, wait=wait)


def _sudo_relaunch(executable: str, argv: Sequence[str], cwd: str, *, wait: bool) -> int | None:
    """Re-exec ``executable`` elevated via ``sudo`` (POSIX), forwarding argv and cwd."""
    command = ["sudo", executable, *argv]
    try:
        if wait:
            completed = subprocess.run(command, cwd=cwd, check=False)  # noqa: S603  # nosec B603 - fixed argv, no shell
            return completed.returncode
        subprocess.Popen(command, cwd=cwd)  # noqa: S603  # nosec B603 - fixed argv, no shell
    except OSError as exc:
        raise NativeCallError(f"elevated relaunch via sudo failed: {exc}") from exc
    return None


def _relaunch_target(executable: str | None, argv: Sequence[str] | None) -> tuple[str, list[str]]:
    """Resolve the ``(executable, argv)`` that faithfully reproduces this invocation.

    Handles the three ways the current process may have been started:
    a frozen build or a console-script ``.exe`` (re-run the exe with its own args);
    ``python -m pkg`` (re-run as ``python -m pkg <args>``); and a plain
    ``python script.py`` (re-run ``python <sys.argv>``).  An explicit ``executable``
    or ``argv`` overrides the detection.
    """
    if executable is not None or argv is not None:
        return (executable or sys.executable), (list(argv) if argv is not None else list(sys.argv))
    argv0 = sys.argv[0] if sys.argv else ""
    tail = list(sys.argv[1:])
    if bool(getattr(sys, "frozen", False)):
        return sys.executable, tail
    if argv0.lower().endswith(".exe"):
        return argv0, tail
    if Path(argv0).name == "__main__.py":
        package = Path(argv0).parent.name
        return sys.executable, ["-m", package, *tail]
    return sys.executable, list(sys.argv)


def _shell_execute_runas(executable: str, argv: Sequence[str], cwd: str, *, wait: bool) -> int | None:
    """Launch ``executable`` elevated via ``ShellExecuteEx`` ``"runas"`` (UAC prompt)."""
    shell = _load("win32com.shell.shell")
    shellcon = _load("win32com.shell.shellcon")
    win32con = _load("win32con")
    win32event = _load("win32event")
    win32process = _load("win32process")
    win32api = _load("win32api")
    parameters = subprocess.list2cmdline(list(argv))
    try:
        result = shell.ShellExecuteEx(
            nShow=win32con.SW_SHOWNORMAL,
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
            lpVerb="runas",
            lpFile=executable,
            lpParameters=parameters,
            lpDirectory=cwd,
        )
    except Exception as exc:  # pywintypes.error (e.g. the user declined the UAC prompt)
        raise NativeCallError(f"Elevated relaunch (ShellExecuteEx runas) failed: {exc}") from exc
    handle = result["hProcess"]
    if not wait:
        return None
    try:
        win32event.WaitForSingleObject(handle, win32event.INFINITE)
        return int(win32process.GetExitCodeProcess(handle))
    finally:
        win32api.CloseHandle(handle)


def _load(module: str) -> Any:
    """Import a ``win32`` module lazily; absent means a portable (non-Windows) install."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError("Elevation requires pywin32 (Windows only); install pwshpy on Windows.") from exc


__all__ = ["elevate", "is_elevated"]
