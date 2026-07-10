"""native elevation: dispatch, relaunch-target resolution, and a Windows oracle.

The os_agnostic units fake the platform and the native relaunch so the decision
logic (Windows vs POSIX, no-op-when-elevated, argv/cwd forwarding, exe-vs-``-m``
target detection) is verified on any OS without ever spawning a UAC prompt.  A
Windows-only oracle test pins ``is_elevated()`` against the independent Win32
``IsUserAnAdmin`` API (read-only, safe, runs on the CI Windows lane).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

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


@pytest.mark.os_agnostic
def test_elevate_posix_reexecs_via_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not elevated + POSIX -> re-exec under sudo, forwarding executable/argv/cwd and exit code."""
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    monkeypatch.setattr(elevation.sys, "platform", "linux")

    def _target(_executable: str | None, _argv: Any) -> tuple[str, list[str]]:
        return ("/opt/app", ["--flag"])

    monkeypatch.setattr(elevation, "_relaunch_target", _target)
    captured: dict[str, Any] = {}

    class _Result:
        returncode = 7

    def _fake_run(command: list[str], **kwargs: Any) -> _Result:
        captured.update(command=command, cwd=kwargs.get("cwd"))
        return _Result()

    monkeypatch.setattr(elevation.subprocess, "run", _fake_run)
    assert elevation.elevate(cwd="/work") == 7
    assert captured == {"command": ["sudo", "/opt/app", "--flag"], "cwd": "/work"}


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
