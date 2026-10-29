"""native elevation: dispatch, relaunch-target resolution, and a Windows oracle.

The os_agnostic units fake the platform and the native relaunch so the decision
logic (Windows vs POSIX, no-op-when-elevated, argv/cwd forwarding, exe-vs-``-m``
target detection) is verified on any OS without ever spawning a UAC prompt.  A
Windows-only oracle test pins ``is_elevated()`` against the independent Win32
``IsUserAnAdmin`` API (read-only, safe, runs on the CI Windows lane).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from elevation_doubles import second_prompts, write_fake_sudo

from pwshpy.adapters.native import elevation


@pytest.mark.os_agnostic
def test_is_elevated_posix_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """On POSIX, euid 0 means elevated."""
    monkeypatch.setattr(elevation.sys, "platform", "linux")
    monkeypatch.setattr(elevation.os, "geteuid", lambda: 0, raising=False)
    assert elevation.is_elevated() is True


@pytest.mark.os_agnostic
def test_is_elevated_posix_nonroot(monkeypatch: pytest.MonkeyPatch) -> None:
    """On POSIX, a nonzero euid means not elevated."""
    monkeypatch.setattr(elevation.sys, "platform", "linux")
    monkeypatch.setattr(elevation.os, "geteuid", lambda: 1000, raising=False)
    assert elevation.is_elevated() is False


@pytest.mark.os_agnostic
def test_is_elevated_windows_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, is_elevated delegates to the token-elevation check."""
    monkeypatch.setattr(elevation.sys, "platform", "win32")
    monkeypatch.setattr(elevation, "_is_elevated_windows", lambda: True)
    assert elevation.is_elevated() is True


@pytest.mark.os_agnostic
def test_elevate_is_noop_when_already_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Already elevated -> return 0 without launching anything."""
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    calls: list[Any] = []

    def _spy(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(elevation, "_shell_execute_runas", _spy)
    assert elevation.elevate() == 0
    assert calls == []


def _sudo_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    grant: bool = True,
    cached: bool = True,
    interrupt_prompt: bool = False,
) -> Path:
    """Put the shared sudo double first on PATH and return the log it writes.

    The double (``elevation_doubles.write_fake_sudo``) is a real executable, so the real
    ``_sudo_relaunch`` runs it through the real subprocess call. ``grant=False`` refuses the
    password at ``sudo -v``; ``cached=False`` plays sudoers ``timestamp_timeout=0`` (``-v`` succeeds
    but caches nothing, so ``-n`` fails with sudo's exit 1); ``interrupt_prompt`` plays a Ctrl+C at
    the prompt, which a terminal sends to the caller as well as to sudo.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "sudo.log"
    write_fake_sudo(bin_dir, grant=grant, cached=cached, calls=log, interrupt_prompt=interrupt_prompt)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log


def _exits_with(code: int) -> list[str]:
    return ["-c", f"import sys; sys.exit({code})"]


def _sudo_relaunch() -> Any:
    return elevation._sudo_relaunch  # pyright: ignore[reportPrivateUsage] - white-box test


@pytest.mark.os_posix
def test_sudo_asks_for_the_password_before_it_runs_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``sudo -v`` first, then the command in the given directory, whose exit code comes back."""
    log = _sudo_on_path(tmp_path, monkeypatch)
    work = tmp_path / "work"
    work.mkdir()

    code = _sudo_relaunch()(sys.executable, _exits_with(7), str(work), wait=True)

    assert code == 7
    assert log.read_text(encoding="utf-8").splitlines() == [
        "-v",
        "-n -v",
        f"-n {sys.executable} {' '.join(_exits_with(7))}",
    ]


@pytest.mark.os_posix
def test_a_sudo_that_caches_no_credential_is_declined_before_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sudoers ``timestamp_timeout=0``: ``-v`` succeeds but caches nothing, so the command would prompt again.

    A refusal at that second prompt came back as the command's exit 1. The cache is checked
    with ``sudo -n -v`` instead, and a sudo that kept nothing is a declined elevation.
    """
    from pwshpy import ElevationDeclinedError

    log = _sudo_on_path(tmp_path, monkeypatch, cached=False)

    with pytest.raises(ElevationDeclinedError, match="cached no credential"):
        _sudo_relaunch()(sys.executable, _exits_with(0), str(tmp_path), wait=True)
    assert log.read_text(encoding="utf-8").splitlines() == ["-v", "-n -v"], "the command ran or prompted again"


@pytest.mark.os_posix
def test_the_command_never_prompts_a_second_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The command runs under ``sudo -n``, so even a credential lost after the check cannot prompt again."""
    log = _sudo_on_path(tmp_path, monkeypatch)

    _sudo_relaunch()(sys.executable, _exits_with(0), str(tmp_path), wait=False)

    lines: list[str] = []
    for _ in range(50):  # the detached command logs itself as soon as the stand-in starts it
        lines = log.read_text(encoding="utf-8").splitlines()
        if len(lines) == 3:
            break
        time.sleep(0.05)
    assert len(lines) == 3, lines
    assert lines[2].startswith("-n "), lines
    assert second_prompts(log) == [], "the command was started without -n and prompted again"


@pytest.mark.os_posix
def test_a_refused_sudo_password_is_declined_and_never_runs_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sudo exits 1 on a failed authentication, the same as any command's 1, so ``-v`` tells them apart."""
    from pwshpy import ElevationDeclinedError

    log = _sudo_on_path(tmp_path, monkeypatch, grant=False)

    with pytest.raises(ElevationDeclinedError, match="sudo"):
        _sudo_relaunch()(sys.executable, _exits_with(0), str(tmp_path), wait=True)
    assert log.read_text(encoding="utf-8").splitlines() == ["-v"], "the command ran although sudo refused"


@pytest.mark.os_posix
def test_ctrl_c_at_the_sudo_prompt_is_declined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A password prompt has no 'No' button, so Ctrl+C there is how a person says no."""
    from pwshpy import ElevationDeclinedError

    log = _sudo_on_path(tmp_path, monkeypatch, interrupt_prompt=True)

    with pytest.raises(ElevationDeclinedError, match="interrupted"):
        _sudo_relaunch()(sys.executable, _exits_with(0), str(tmp_path), wait=True)
    assert log.read_text(encoding="utf-8").splitlines() == ["-v"], "the command ran after the prompt was interrupted"


@pytest.mark.os_posix
def test_ctrl_c_inside_the_elevated_command_still_interrupts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: once root was granted, Ctrl+C is the program's own business again.

    This stand-in shares the caller's process group, as a sudo without ``use_pty``
    does, so the caller sees the interrupt. Real sudo 1.9.15p5 (Ubuntu 24.04)
    runs the command in its own pty instead: the command dies of SIGINT and the
    caller gets ``-2`` back. Either way nothing here turns it into a refusal.
    """
    _sudo_on_path(tmp_path, monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        _sudo_relaunch()("/bin/sh", ["-c", 'kill -INT "$PPID"; sleep 2'], str(tmp_path), wait=True)


@pytest.mark.os_posix
def test_a_command_exiting_1_after_sudo_granted_root_is_its_own_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a 1 from the command itself is a result, not a refusal."""
    _sudo_on_path(tmp_path, monkeypatch)

    assert _sudo_relaunch()(sys.executable, _exits_with(1), str(tmp_path), wait=True) == 1


@pytest.mark.os_posix
def test_no_sudo_on_path_is_a_failure_that_names_sudo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pwshpy import ElevationDeclinedError, NativeCallError

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    with pytest.raises(NativeCallError, match="sudo") as raised:
        _sudo_relaunch()(sys.executable, _exits_with(0), str(tmp_path), wait=True)
    assert not isinstance(raised.value, ElevationDeclinedError)


@pytest.mark.os_agnostic
def test_elevate_posix_hands_the_target_to_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not elevated + POSIX -> the sudo relaunch gets the resolved target, cwd and wait."""
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    monkeypatch.setattr(elevation.sys, "platform", "linux")

    def _target(_executable: str | None, _argv: Any) -> tuple[str, list[str]]:
        return ("/opt/app", ["--flag"])

    monkeypatch.setattr(elevation, "_relaunch_target", _target)
    captured: dict[str, Any] = {}

    def _fake_sudo(executable: str, argv: Any, cwd: str, *, wait: bool) -> int:
        captured.update(executable=executable, argv=argv, cwd=cwd, wait=wait)
        return 7

    monkeypatch.setattr(elevation, "_sudo_relaunch", _fake_sudo)
    assert elevation.elevate(cwd="/work") == 7
    assert captured == {"executable": "/opt/app", "argv": ["--flag"], "cwd": "/work", "wait": True}


@pytest.mark.os_agnostic
def test_elevate_windows_forwards_target_and_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, elevate resolves the relaunch target and forwards cwd/wait."""
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    monkeypatch.setattr(elevation.sys, "platform", "win32")

    def _fake_target(_executable: str | None, _argv: Any) -> tuple[str, list[str]]:
        return "py.exe", ["-m", "pwshpy", "services"]

    monkeypatch.setattr(elevation, "_relaunch_target", _fake_target)
    captured: dict[str, Any] = {}

    def _fake_runas(executable: str, argv: Any, cwd: str, *, wait: bool) -> int:
        captured.update(executable=executable, argv=argv, cwd=cwd, wait=wait)
        return 3

    monkeypatch.setattr(elevation, "_shell_execute_runas", _fake_runas)
    assert elevation.elevate(cwd="C:/work", wait=True) == 3
    assert captured == {"executable": "py.exe", "argv": ["-m", "pwshpy", "services"], "cwd": "C:/work", "wait": True}


@pytest.mark.os_agnostic
def test_relaunch_target_console_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A console-script .exe is re-run directly with its own args."""
    monkeypatch.setattr(elevation.sys, "argv", [r"C:\Scripts\pwshpy.exe", "services", "--json"])
    monkeypatch.setattr(elevation.sys, "frozen", False, raising=False)
    relaunch_target = elevation._relaunch_target  # pyright: ignore[reportPrivateUsage] - white-box test
    assert relaunch_target(None, None) == (r"C:\Scripts\pwshpy.exe", ["services", "--json"])


@pytest.mark.os_agnostic
def test_relaunch_target_dash_m(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``python -m pkg`` run is reconstructed as ``python -m pkg <args>``."""
    # Native-separator path so Path(argv0).name splits the same way on every OS.
    argv0 = str(Path("proj") / "pwshpy" / "__main__.py")
    monkeypatch.setattr(elevation.sys, "argv", [argv0, "services"])
    monkeypatch.delattr(elevation.sys, "frozen", raising=False)
    monkeypatch.setattr(elevation.sys, "executable", "python.exe")
    relaunch_target = elevation._relaunch_target  # pyright: ignore[reportPrivateUsage] - white-box test
    assert relaunch_target(None, None) == ("python.exe", ["-m", "pwshpy", "services"])


@pytest.mark.os_agnostic
def test_relaunch_target_explicit_override() -> None:
    """An explicit executable + argv is used verbatim."""
    relaunch_target = elevation._relaunch_target  # pyright: ignore[reportPrivateUsage] - white-box test
    assert relaunch_target("my.exe", ["a", "b"]) == ("my.exe", ["a", "b"])


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows elevation oracle is Windows-only")
def test_is_elevated_matches_win32_oracle() -> None:
    """is_elevated() agrees with the independent Win32 shell32.IsUserAnAdmin oracle."""
    import ctypes

    from pwshpy import ps

    oracle = bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]  # windll is Windows-only
    assert ps.is_elevated() == oracle


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="POSIX euid path is Linux/macOS")
def test_is_elevated_matches_euid() -> None:
    """On POSIX, is_elevated() reflects the real euid (root -> True, else False)."""
    # os.geteuid is POSIX-only (absent from typeshed on Windows); this test is Linux-gated.
    euid = int(elevation.os.geteuid())  # type: ignore[attr-defined]
    assert elevation.is_elevated() is (euid == 0)


@pytest.mark.os_agnostic
def test_relaunch_target_uv_console_launcher_has_no_exe_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A uv-built console script reports ``sys.argv[0]`` WITHOUT ``.exe``; its ``.exe`` sibling is what runs.

    Measured on Windows: under a launcher from ``uv pip install`` argv0 was
    ``...\\Scripts\\argvprobe`` while only ``argvprobe.exe`` existed, so the old
    fallback re-ran ``python <argv0>`` and failed with "can't open file".
    """
    launcher = tmp_path / "pwshpy"
    (tmp_path / "pwshpy.exe").write_bytes(b"MZ")
    monkeypatch.setattr(elevation.sys, "argv", [str(launcher), "services", "--json"])
    monkeypatch.setattr(elevation.sys, "frozen", False, raising=False)
    relaunch_target = elevation._relaunch_target  # pyright: ignore[reportPrivateUsage] - white-box test
    assert relaunch_target(None, None) == (str(tmp_path / "pwshpy.exe"), ["services", "--json"])


class _ShellDoubles:
    """The pywin32 modules ``_shell_execute`` loads, as doubles that record the request.

    ``fail_with`` is the Windows error code ShellExecuteEx raises, or None to
    start a process whose exit code is ``exit_code``.
    """

    def __init__(self, *, fail_with: int | None = None, exit_code: int = 5) -> None:
        """Answer every request the same way."""
        self.fail_with = fail_with
        self.exit_code = exit_code
        self.requests: list[dict[str, Any]] = []
        self.closed: list[int] = []

    def load(self, name: str) -> Any:
        """Stand in for ``_load``: one namespace per pywin32 module the code imports."""
        from types import SimpleNamespace

        modules = {
            "win32com.shell.shell": SimpleNamespace(ShellExecuteEx=self._shell_execute_ex),
            "win32com.shell.shellcon": SimpleNamespace(SEE_MASK_NOCLOSEPROCESS=0x40, SEE_MASK_FLAG_NO_UI=0x400),
            "win32con": SimpleNamespace(SW_SHOWNORMAL=1),
            "win32event": SimpleNamespace(WaitForSingleObject=self._wait, INFINITE=0xFFFFFFFF),
            "win32process": SimpleNamespace(GetExitCodeProcess=self._exit_code_of),
            "win32api": SimpleNamespace(CloseHandle=self.closed.append),
        }
        return modules[name]

    def _wait(self, handle: int, milliseconds: int) -> int:
        del handle, milliseconds
        return 0

    def _exit_code_of(self, handle: int) -> int:
        del handle
        return self.exit_code

    def _shell_execute_ex(self, **request: Any) -> dict[str, int]:
        self.requests.append(request)
        if self.fail_with is not None:
            raise _WinError(self.fail_with)
        return {"hProcess": 42}


class _WinError(Exception):
    """Shaped like ``pywintypes.error``: the code on ``winerror`` (measured on Windows)."""

    def __init__(self, code: int) -> None:
        """Carry ``code`` the way pywin32 does."""
        super().__init__(code, "ShellExecuteEx", "a Windows error")
        self.winerror = code


def _launch() -> Any:
    return elevation._Launch("C:\\Python\\python.exe", ["-m", "tool", "a b"], "C:\\work")  # pyright: ignore[reportPrivateUsage] - white-box test


@pytest.mark.os_agnostic
def test_a_declined_prompt_raises_its_own_type() -> None:
    """Error 1223 is the person refusing the prompt, which a caller handles unlike a failure."""
    from pwshpy import ElevationDeclinedError, NativeCallError

    doubles = _ShellDoubles(fail_with=1223)
    shell_execute = elevation._shell_execute  # pyright: ignore[reportPrivateUsage] - white-box test

    with pytest.raises(ElevationDeclinedError, match="declined"):
        shell_execute(_launch(), wait=True, load=doubles.load)
    assert issubclass(ElevationDeclinedError, NativeCallError), "callers catching NativeCallError must still catch it"


@pytest.mark.os_agnostic
def test_any_other_shell_execute_failure_stays_a_native_call_error() -> None:
    from pwshpy import ElevationDeclinedError, NativeCallError

    doubles = _ShellDoubles(fail_with=2)
    shell_execute = elevation._shell_execute  # pyright: ignore[reportPrivateUsage] - white-box test

    with pytest.raises(NativeCallError, match="ShellExecuteEx") as raised:
        shell_execute(_launch(), wait=True, load=doubles.load)
    assert not isinstance(raised.value, ElevationDeclinedError)


@pytest.mark.os_agnostic
def test_the_request_never_lets_windows_show_an_error_box() -> None:
    """The request carries SEE_MASK_FLAG_NO_UI, and everything else the relaunch asks for.

    Measured on Windows: without the flag a request for a missing program never
    returned where no desktop shows the box; with it, it failed at once with error 2.
    """
    doubles = _ShellDoubles()
    shell_execute = elevation._shell_execute  # pyright: ignore[reportPrivateUsage] - white-box test

    assert shell_execute(_launch(), wait=True, load=doubles.load) == 5

    (request,) = doubles.requests
    assert request["fMask"] & 0x400, "no SEE_MASK_FLAG_NO_UI"
    assert request["fMask"] & 0x40, "no SEE_MASK_NOCLOSEPROCESS, so nothing to wait on"
    assert request["lpVerb"] == "runas"
    assert request["lpFile"] == "C:\\Python\\python.exe"
    assert request["lpParameters"] == '-m tool "a b"'
    assert request["lpDirectory"] == "C:\\work"
    assert doubles.closed == [42], "the process handle leaked"


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="ShellExecuteEx exists only on Windows")
def test_shell_execute_really_starts_a_process_and_returns_its_exit_code() -> None:
    """The real call, end to end, with the ``open`` verb so no UAC prompt appears.

    Read-only and safe: ``cmd /c exit 7`` changes nothing. Every other test here
    replaces ShellExecuteEx; this is the one that proves the request pwshpy
    builds is one Windows accepts and that the wait and exit code come back.
    """
    import os

    cmd = str(Path(os.environ["SYSTEMROOT"]) / "System32" / "cmd.exe")
    launch = elevation._Launch(cmd, ["/c", "exit", "7"], str(Path.cwd()))  # pyright: ignore[reportPrivateUsage] - white-box test
    shell_execute = elevation._shell_execute  # pyright: ignore[reportPrivateUsage] - white-box test

    assert shell_execute(launch, wait=True, verb="open") == 7
