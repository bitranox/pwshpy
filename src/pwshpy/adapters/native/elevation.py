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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from ...domain.errors import ElevationDeclinedError, PlatformUnsupportedError
from .marshal import native_error


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
        raise native_error(f"OpenProcessToken failed: {exc}", exc) from exc
    try:
        return bool(win32security.GetTokenInformation(token, win32security.TokenElevation))
    except Exception as exc:
        raise native_error(f"GetTokenInformation(TokenElevation) failed: {exc}", exc) from exc
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
    directory - fixing PowerShell's ``System32`` gotcha).  POSIX (Linux/macOS) asks for the
    password with ``sudo -v`` on the TTY, then re-execs the command under ``sudo``.  ``argv``
    defaults to the current process's.

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
        ElevationDeclinedError: The UAC prompt was refused, or sudo did not grant root.
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
    """Re-exec ``executable`` elevated via ``sudo`` (POSIX), forwarding argv and cwd.

    The password is asked by ``sudo -v`` on its own, before the command: sudo exits
    1 when authentication fails (``man sudo``), which is a code the command itself
    may return, so only a separate step can tell a refusal from a result. The
    command then runs on the credential ``-v`` cached, under ``sudo -n`` so it can
    never prompt a second time: a sudo that caches nothing (sudoers
    ``timestamp_timeout=0``) would ask again, and a refusal there would come back
    as the command's exit 1.

    Raises:
        ElevationDeclinedError: sudo did not grant root - a wrong or refused
            password, a Ctrl+C at the prompt, a user sudo does not allow, or a
            sudo that cached no credential for the command to run on.
        NativeCallError: sudo could not be started at all.
    """
    command = ["sudo", "-n", executable, *argv]
    try:
        _ask_sudo_for_root(cwd)
        if wait:
            completed = subprocess.run(command, cwd=cwd, check=False)  # noqa: S603  # nosec B603 - fixed argv, no shell
            return completed.returncode
        subprocess.Popen(command, cwd=cwd)  # noqa: S603  # nosec B603 - fixed argv, no shell
    except OSError as exc:
        raise native_error(f"elevated relaunch via sudo failed: {exc}", exc) from exc
    return None


def _ask_sudo_for_root(cwd: str) -> None:
    """Ask for the password with ``sudo -v``, and raise unless sudo granted root.

    A Ctrl+C at the prompt is a refusal too: a password prompt has no "No", so
    that is how a person declines, and the terminal delivers the SIGINT to this
    process as well as to sudo (measured with sudo 1.9.15p5 in a pty). Only this
    step converts it; a Ctrl+C once the command runs interrupts as usual.
    """
    try:
        granted = subprocess.run(["sudo", "-v"], cwd=cwd, check=False).returncode == 0  # noqa: S607  # nosec B603 B607 - fixed argv, no shell; sudo resolved from PATH like the relaunch
    except KeyboardInterrupt as exc:
        raise native_error("The sudo password prompt was interrupted.", exc, kind=ElevationDeclinedError) from exc
    if not granted:
        raise ElevationDeclinedError("sudo did not grant root (refused, a wrong password, or not allowed).")
    # `-n -v` re-validates without prompting: it fails when `-v` cached nothing. `-n true` would
    # also fail for a user whose sudoers rule allows only the command itself, not `true`.
    cached = subprocess.run(["sudo", "-n", "-v"], cwd=cwd, check=False).returncode == 0  # noqa: S607  # nosec B603 B607 - fixed argv, no shell; sudo resolved from PATH like the relaunch
    if not cached:
        raise ElevationDeclinedError(
            "sudo granted root but cached no credential (sudoers timestamp_timeout=0?), so the command "
            "would ask for the password again; run it under sudo yourself instead."
        )


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
    # A uv-built console-script launcher reports argv0 WITHOUT ".exe" (measured on
    # Windows): only the .exe sibling exists, and `python <argv0>` cannot open it.
    if argv0 and Path(argv0 + ".exe").is_file():
        return argv0 + ".exe", tail
    if Path(argv0).name == "__main__.py":
        package = Path(argv0).parent.name
        return sys.executable, ["-m", package, *tail]
    return sys.executable, list(sys.argv)


#: ShellExecuteEx's error when the person refuses the UAC prompt (winerror.h).
ERROR_CANCELLED = 1223


class _Launch(NamedTuple):
    """The program, its arguments and the directory it starts in, as one request."""

    executable: str
    argv: Sequence[str]
    cwd: str


def _shell_execute_runas(executable: str, argv: Sequence[str], cwd: str, *, wait: bool) -> int | None:
    """Launch ``executable`` elevated via ``ShellExecuteEx`` ``"runas"`` (UAC prompt)."""
    return _shell_execute(_Launch(executable, argv, cwd), wait=wait)


def _shell_execute(
    launch: _Launch, *, wait: bool, verb: str = "runas", load: Callable[[str], Any] | None = None
) -> int | None:
    """Start ``launch`` through ``ShellExecuteEx`` and optionally wait for its exit code.

    ``verb`` is ``"runas"`` for every caller; a test passes ``"open"`` so the real
    call runs on Windows without a UAC prompt. ``SEE_MASK_FLAG_NO_UI`` is
    load-bearing: measured on Windows, a request it could not satisfy otherwise
    waited on an error box, which never returns where no desktop shows it; with
    the flag the same request failed at once with error 2.

    Raises:
        ElevationDeclinedError: The person refused the prompt (``ERROR_CANCELLED``).
        NativeCallError: Any other reason ShellExecuteEx could not start it.
    """
    # Resolved per call, not as a default: a default is bound when the def runs.
    loader = load if load is not None else _load
    shell = loader("win32com.shell.shell")
    shellcon = loader("win32com.shell.shellcon")
    win32con = loader("win32con")
    win32event = loader("win32event")
    win32process = loader("win32process")
    win32api = loader("win32api")
    parameters = subprocess.list2cmdline(list(launch.argv))
    try:
        result = shell.ShellExecuteEx(
            nShow=win32con.SW_SHOWNORMAL,
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS | shellcon.SEE_MASK_FLAG_NO_UI,
            lpVerb=verb,
            lpFile=launch.executable,
            lpParameters=parameters,
            lpDirectory=launch.cwd,
        )
    except Exception as exc:  # pywintypes.error, its Windows code on .winerror
        if getattr(exc, "winerror", None) == ERROR_CANCELLED:
            raise native_error("The Administrator prompt was declined.", exc, kind=ElevationDeclinedError) from exc
        raise native_error(f"Elevated relaunch (ShellExecuteEx {verb}) failed: {exc}", exc) from exc
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
