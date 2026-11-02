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
  (Linux/macOS) re-execs the command under ``sudo -n``: at once when sudo runs the current
  interpreter without a password, otherwise after asking for the password once with ``sudo -v``.

``win32`` modules are imported lazily so a portable install stays clean.

Contents:
    * :func:`is_elevated` - whether this process has administrative rights.
    * :func:`elevate` - relaunch the current process elevated, forwarding argv/cwd.
"""

from __future__ import annotations

import errno
import importlib
import ntpath
import os
import shutil
import subprocess  # nosec B404 - argv quoting (list2cmdline) + the sudo re-exec on POSIX; always a fixed argv, no shell
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from ...domain.errors import ElevationDeclinedError, ElevationDirectoryError, PlatformUnsupportedError
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
    directory - fixing PowerShell's ``System32`` gotcha).  POSIX (Linux/macOS) re-execs the
    command under ``sudo -n``, which never prompts: at once when sudo runs the current interpreter
    without a password (a NOPASSWD sudoers rule), otherwise after asking for the password with
    ``sudo -v`` on the TTY and checking with ``sudo -n -v`` that a credential was cached.  ``argv``
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
        ElevationDirectoryError: Windows, before any prompt: the directory is on a ``net use`` drive, or
            the script being relaunched is and ``EnableLinkedConnections`` is not set.
        ElevationDeclinedError: The UAC prompt was refused, or sudo did not grant root or
            cached no credential for the command (sudoers ``timestamp_timeout=0``).
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
        # ntpath, not Path.absolute(): below Python 3.12 that keeps a drive-relative "Z:sub" as it is.
        refuse_a_mapped_drive(ntpath.abspath(directory), connection_of=_network_connection)
        script = _script_being_relaunched(resolved_exe, resolved_argv)
        if script is not None:
            refuse_a_script_the_child_cannot_open(
                _path_the_child_opens(script, directory),
                connection_of=_network_connection,
                linked_connections=_linked_connections,
            )
        return _shell_execute_runas(resolved_exe, resolved_argv, directory, wait=wait)
    return _sudo_relaunch(resolved_exe, resolved_argv, directory, wait=wait)


def _sudo_relaunch(executable: str, argv: Sequence[str], cwd: str, *, wait: bool) -> int | None:
    """Re-exec ``executable`` elevated via ``sudo`` (POSIX), forwarding argv and cwd.

    A user sudo lets run the interpreter without a password (a NOPASSWD rule) is
    asked nothing ([280], :func:`_sudo_runs_without_a_password`). Otherwise the
    password is asked by ``sudo -v`` on its own, before the command: sudo exits
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
    try:
        sudo = _find_sudo()
        command = [sudo, "-n", executable, *argv]
        if not _sudo_runs_without_a_password(sudo, executable, cwd):
            _ask_sudo_for_root(sudo, cwd)
        if wait:
            completed = subprocess.run(command, cwd=cwd, check=False)  # noqa: S603  # nosec B603 - fixed argv, no shell
            return completed.returncode
        subprocess.Popen(command, cwd=cwd)  # noqa: S603  # nosec B603 - fixed argv, no shell
    except OSError as exc:
        raise native_error(f"elevated relaunch via sudo failed: {exc}", exc) from exc
    return None


def _find_sudo() -> str:
    """The absolute path of the ``sudo`` this process's PATH finds, for every sudo call of one relaunch.

    Looked up once, here, rather than per call: a relative PATH entry is resolved against the cwd of
    the process doing the lookup, so a per-call lookup in the child's ``cwd`` could run another sudo
    than the probe did.

    Raises:
        FileNotFoundError: No ``sudo`` on PATH (the caller reports it as a failed relaunch).
    """
    found = shutil.which("sudo")
    if found is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), "sudo")
    return str(Path(found).absolute())


#: What the probe runs the relaunch's interpreter with: Python started to do nothing.
_PROBE_ARGS = ("-c", "pass")
#: How long the probe may take; ``sudo -n`` never waits for a person.
_PROBE_TIMEOUT_SECONDS = 60


def _sudo_runs_without_a_password(sudo: str, executable: str, cwd: str) -> bool:
    """Whether ``sudo -n`` runs the relaunch's own program without asking for a password ([280]).

    Under sudo's default ``verifypw=all``, ``sudo -v`` wants a password unless EVERY sudoers rule of
    the user is NOPASSWD, so a user with a NOPASSWD rule beside Ubuntu's ``%sudo ALL`` was asked for
    a password the command does not need (and refused without a terminal, or without any password).
    The probe runs the same program the relaunch runs, so a rule allowing some other program only
    cannot pass it and then refuse the relaunch. Only the current interpreter can be run to do
    nothing; any other program (an explicit ``executable``, a frozen build) is never probed, and the
    ``sudo -v`` flow decides as before. Silent: its output and refusal are not the user's business.
    """
    if executable != sys.executable or bool(getattr(sys, "frozen", False)):
        return False
    try:
        probe = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, no shell
            [sudo, "-n", executable, *_PROBE_ARGS],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # A sudo that cannot start, or a hung probe, proves nothing: the flow below decides.
        return False
    return probe.returncode == 0


def _ask_sudo_for_root(sudo: str, cwd: str) -> None:
    """Ask for the password with ``sudo -v``, and raise unless sudo granted root.

    A Ctrl+C at the prompt is a refusal too: a password prompt has no "No", so
    that is how a person declines, and the terminal delivers the SIGINT to this
    process as well as to sudo (measured with sudo 1.9.15p5 in a pty). Only this
    step converts it; a Ctrl+C once the command runs interrupts as usual.
    """
    try:
        granted = subprocess.run([sudo, "-v"], cwd=cwd, check=False).returncode == 0  # noqa: S603  # nosec B603 - fixed argv, no shell
    except KeyboardInterrupt as exc:
        raise native_error("The sudo password prompt was interrupted.", exc, kind=ElevationDeclinedError) from exc
    if not granted:
        raise ElevationDeclinedError("sudo did not grant root (refused, a wrong password, or not allowed).")
    # `-n -v` re-validates without prompting: it fails when `-v` cached nothing. `-n true` would
    # also fail for a user whose sudoers rule allows only the command itself, not `true`.
    cached = subprocess.run([sudo, "-n", "-v"], cwd=cwd, check=False).returncode == 0  # noqa: S603  # nosec B603 - fixed argv, no shell
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


def refuse_a_mapped_drive(directory: str, *, connection_of: Callable[[str], str | None]) -> None:
    """Refuse an elevation that would start in the UNC path of ``directory``'s ``net use`` drive.

    ShellExecute hands the elevated child that path (measured, with and without
    ``EnableLinkedConnections``), so nothing the caller did would tell them they left the drive.

    Args:
        directory: The absolute directory the child would start in.
        connection_of: Answers a drive (``"Z:"``) with its share, or None when it is not a network drive.

    Raises:
        ElevationDirectoryError: ``directory`` is on a network drive.

    Example:
        >>> refuse_a_mapped_drive("C:\\\\work", connection_of=lambda drive: None)
    """
    unc = _unc_of(directory, connection_of)
    if unc is None:
        return
    raise ElevationDirectoryError(
        f"{directory} is on a mapped network drive, which an elevated process runs from as {unc}: there an "
        "elevated cmd.exe works in C:\\Windows and a bare program name can run from the share. Change to a "
        f"local path, or to {unc} if you mean it."
    )


def refuse_a_script_the_child_cannot_open(
    script: str, *, connection_of: Callable[[str], str | None], linked_connections: Callable[[], bool]
) -> None:
    """Refuse relaunching a script on a ``net use`` drive the elevated child has no letter for.

    The script path travels in the parameters, which ShellExecute leaves as they are, so the child gets
    the letter while its token has no such drive: consent, then Python's exit 2. A program path is
    different: ShellExecute makes that one UNC and it runs, so a console-script ``.exe`` or an interpreter
    on the drive is not refused, and with ``EnableLinkedConnections`` the letter is there and the script
    opens (all measured with real consent).

    Args:
        script: The absolute path of the script.
        connection_of: Answers a drive (``"Z:"``) with its share, or None when it is not a network drive.
        linked_connections: Whether the elevated token shares the drive letters; only asked for a mapped script.

    Raises:
        ElevationDirectoryError: ``script`` is on a network drive the child cannot see.

    Example:
        >>> refuse_a_script_the_child_cannot_open(
        ...     "C:\\\\work\\\\run.py", connection_of=lambda drive: None, linked_connections=lambda: False
        ... )
    """
    unc = _unc_of(script, connection_of)
    if unc is None or linked_connections():
        return
    raise ElevationDirectoryError(
        f"{script} is on a mapped network drive that an elevated process cannot see here "
        f"(EnableLinkedConnections is not set), so it could not open the script after the prompt. Copy the "
        f"script to a local path, or start it as {unc} if you mean to run it from the share."
    )


def _unc_of(path: str, connection_of: Callable[[str], str | None]) -> str | None:
    """The UNC spelling of a path on a network drive; None for any other path."""
    drive, rest = ntpath.splitdrive(path.removeprefix("\\\\?\\").removeprefix("\\\\.\\"))
    if not drive.endswith(":"):
        return None
    share = connection_of(drive)
    return None if share is None else share.rstrip("\\") + rest


def _script_being_relaunched(executable: str, argv: Sequence[str]) -> str | None:
    """The script of a ``python script.py`` relaunch; None for the other shapes of :func:`_relaunch_target`.

    Compared with this process's own start, because a caller's own ``argv`` is not known to hold a script.
    Python puts ``""``, ``"-"`` or ``"-c"`` in ``sys.argv[0]`` when no script file was named.
    """
    if bool(getattr(sys, "frozen", False)) or executable != sys.executable or list(argv) != sys.argv or not argv:
        return None
    return None if argv[0] in {"", "-", "-c"} else argv[0]


def _path_the_child_opens(script: str, directory: str) -> str:
    """Where a script path leads for a child starting in ``directory``: a relative one is opened from there."""
    return ntpath.abspath(ntpath.join(directory, script))


def _linked_connections(load: Callable[[str], Any] | None = None) -> bool:
    """Whether the ``EnableLinkedConnections`` policy is set, as far as the registry says.

    A value nobody can read answers True: the refusal stands only on a policy known to be unset, and
    the caller otherwise gets the prompt, as before the refusal existed.
    """
    winreg = (load if load is not None else _load)("winreg")
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _POLICIES_SYSTEM_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "EnableLinkedConnections")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return isinstance(value, int) and value != 0


def _network_connection(drive: str) -> str | None:
    """The share a drive letter is mapped to; None when the answer is anything else.

    pywin32 raises on every non-zero answer, so a remembered drive that is disconnected yields no share for
    the refusal to name; such a drive gets the prompt, as before the refusal existed.
    """
    win32wnet = _load("win32wnet")
    try:
        return str(win32wnet.WNetGetConnection(drive))
    except _load("pywintypes").error:
        return None


#: Where Windows keeps the ``EnableLinkedConnections`` policy, under HKEY_LOCAL_MACHINE.
_POLICIES_SYSTEM_KEY = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System"

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
