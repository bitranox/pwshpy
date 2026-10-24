"""Oracle: the known-folder resolvers vs pwsh's ``[Environment]::GetFolderPath`` on every OS.

pwshpy's contract for known folders is "answer what pwsh answers on the same machine", so each
test runs ONE pwsh child that prints all six ``KnownFolder`` members as JSON and compares that
with :func:`posix_known_folder` / :func:`windows_known_folder` given the same environment.
Not ``local_only``: GitHub's ubuntu, macos and windows runners all ship pwsh, so CI runs this
oracle on every OS (macOS is checked nowhere else). Without pwsh on PATH every test skips with
a reason naming the missing pwsh, so a CI log shows whether the oracle ran.

Mapping: pwsh's ``""`` is pwshpy's ``None``, except Programs on Linux and macOS, where .NET has
no folder by construction and pwshpy raises :class:`PlatformUnsupportedError` instead. Paths
are compared as :class:`~pathlib.Path`, never as raw strings.

The pwsh child runs first and pwshpy second: pwsh creates ``$HOME/.local/share/powershell``
at startup (measured), which makes LocalApplicationData exist for the second reader in a
scratch home. Each Linux scenario also pins what it was built to produce, so a scenario whose
setup pwsh ignores fails instead of passing with both sides agreeing on something else.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias, cast

import pytest

from pwshpy.adapters.native.known_folders import posix_known_folder, windows_known_folder
from pwshpy.domain.enums import KnownFolder
from pwshpy.domain.errors import PlatformUnsupportedError

if sys.platform != "win32":
    import pwd

_PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(
    _PWSH is None, reason="pwsh (PowerShell 7) is not on PATH: the known-folder oracle needs it"
)

_IS_ROOT = sys.platform != "win32" and os.geteuid() == 0

#: One pwsh call answers every member; EscapeNonAscii keeps the JSON ASCII whatever the console
#: code page, so a non-ASCII path survives the pipe on every OS.
_SCRIPT = (
    "$folders = [ordered]@{}; "
    f"foreach ($name in @({', '.join(repr(member.value) for member in KnownFolder)})) "
    "{ $folders[$name] = [Environment]::GetFolderPath($name) }; "
    "$folders | ConvertTo-Json -Compress -EscapeHandling EscapeNonAscii"
)

#: Keeps the scratch child quiet; neither variable bears on a known folder.
_QUIET_PWSH = {"POWERSHELL_TELEMETRY_OPTOUT": "1", "POWERSHELL_UPDATECHECK": "Off"}

#: Stands for "pwshpy raised PlatformUnsupportedError" in a comparison table.
_UNSUPPORTED = "<PlatformUnsupportedError>"

Answer: TypeAlias = "Path | str | None"


def _pwsh_folders(env: Mapping[str, str]) -> dict[KnownFolder, Path | None]:
    """Run pwsh with ``env`` and return its answer per member, ``""`` mapped to ``None``."""
    assert _PWSH is not None
    completed = subprocess.run(  # noqa: S603 - absolute pwsh path and a constant script; test oracle only
        [_PWSH, "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
        env=dict(env),
        capture_output=True,
        check=False,
        timeout=120,
    )
    stderr = completed.stderr.decode("utf-8", errors="replace")
    assert completed.returncode == 0, f"pwsh exited {completed.returncode}: {stderr}"
    raw = cast("dict[str, Any]", json.loads(completed.stdout.decode("utf-8")))
    assert set(raw) == {member.value for member in KnownFolder}, raw
    return {member: Path(raw[member.value]) if raw[member.value] else None for member in KnownFolder}


def _ours(resolve: Callable[[KnownFolder], Path | None]) -> dict[KnownFolder, Answer]:
    """Return pwshpy's answer per member; a refused member reads as ``_UNSUPPORTED``."""
    return {member: _answer(resolve, member) for member in KnownFolder}


def _answer(resolve: Callable[[KnownFolder], Path | None], member: KnownFolder) -> Answer:
    try:
        return resolve(member)
    except PlatformUnsupportedError:
        return _UNSUPPORTED


def _expected(theirs: Mapping[KnownFolder, Path | None], *, posix: bool) -> dict[KnownFolder, Answer]:
    """Translate pwsh's answers into what pwshpy must answer.

    Programs on POSIX: pwsh's ``""`` is structural there, so pwshpy refuses the member. A
    non-empty pwsh answer for it stays a path and fails the comparison.
    """
    expected: dict[KnownFolder, Answer] = dict(theirs)
    if posix and theirs[KnownFolder.PROGRAMS] is None:
        expected[KnownFolder.PROGRAMS] = _UNSUPPORTED
    return expected


def _differences(ours: Mapping[KnownFolder, Answer], expected: Mapping[KnownFolder, Answer]) -> str:
    rows = [f"{m.value}: pwshpy={ours[m]!r} pwsh={expected[m]!r}" for m in KnownFolder if ours[m] != expected[m]]
    return "; ".join(rows)


def _compare_posix(env: Mapping[str, str]) -> dict[KnownFolder, Path | None]:
    """Assert pwshpy equals pwsh for every member under ``env``; return pwsh's answers."""
    theirs = _pwsh_folders(env)  # first: pwsh's startup creates folders the second reader sees
    ours = _ours(lambda member: posix_known_folder(member, environ=env))
    expected = _expected(theirs, posix=True)
    assert ours == expected, _differences(ours, expected)
    return theirs


def _scratch_env(overrides: Mapping[str, str], *, drop: Callable[[str], bool]) -> dict[str, str]:
    """Return this process's environment minus the ``drop`` names, plus pwsh quieting and ``overrides``.

    Never a fresh dict: the child still needs PATH and the rest of the inherited environment.
    """
    kept = {key: value for key, value in os.environ.items() if not drop(key)}
    return {**kept, **_QUIET_PWSH, **overrides}


# --- Host environment (Linux and macOS) -------------------------------------------------------


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX resolver; Windows has its own test")
def test_host_environment_matches_pwsh() -> None:
    """With the environment this test runs in, every member equals pwsh's answer."""
    theirs = _compare_posix(_scratch_env({}, drop=lambda _key: False))
    assert theirs[KnownFolder.PROGRAM_DATA] == Path("/usr/share")


# --- Linux scenarios --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Scenario:
    """A scratch Linux home: the environment overrides and the Desktop/MyDocuments it must produce."""

    env: dict[str, str]
    desktop: Path | None
    documents: Path | None


def _make_home(tmp_path: Path, *subdirs: str) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for sub in subdirs:
        (home / sub).mkdir(parents=True)
    return home


def _write_user_dirs(config: Path, text: str) -> None:
    """Write ``user-dirs.dirs`` as UTF-8 bytes with LF line ends."""
    config.mkdir(parents=True, exist_ok=True)
    (config / "user-dirs.dirs").write_bytes(text.encode("utf-8"))


def _user_dirs_home_relative(tmp_path: Path) -> _Scenario:
    desktop = "Schreibtisch-" + chr(0xE4)  # non-ASCII: the JSON pipe must carry it intact
    home = _make_home(tmp_path, desktop, "Docs", ".config")
    _write_user_dirs(home / ".config", f'XDG_DESKTOP_DIR="$HOME/{desktop}"\nXDG_DOCUMENTS_DIR="$HOME/Docs"\n')
    return _Scenario({"HOME": str(home)}, home / desktop, home / "Docs")


def _user_dirs_absolute(tmp_path: Path) -> _Scenario:
    home = _make_home(tmp_path)
    elsewhere = tmp_path / "abs-desktop"
    elsewhere.mkdir()
    _write_user_dirs(home / ".config", f'XDG_DESKTOP_DIR="{elsewhere}"\n')
    return _Scenario({"HOME": str(home)}, elsewhere, None)


def _xdg_env_wins(tmp_path: Path) -> _Scenario:
    home = _make_home(tmp_path, "FromFile", "Documents")
    _write_user_dirs(home / ".config", 'XDG_DESKTOP_DIR="$HOME/FromFile"\n')
    from_env = tmp_path / "from-env"
    from_env.mkdir()
    return _Scenario({"HOME": str(home), "XDG_DESKTOP_DIR": str(from_env)}, from_env, home / "Documents")


def _no_folders(tmp_path: Path) -> _Scenario:
    home = _make_home(tmp_path)
    return _Scenario({"HOME": str(home)}, None, None)


def _home_fallbacks(tmp_path: Path) -> _Scenario:
    home = _make_home(tmp_path, "Desktop", "Documents")
    return _Scenario({"HOME": str(home)}, home / "Desktop", home / "Documents")


def _xdg_config_home_elsewhere(tmp_path: Path) -> _Scenario:
    home = _make_home(tmp_path, "Ignored", "Chosen")
    _write_user_dirs(home / ".config", 'XDG_DESKTOP_DIR="$HOME/Ignored"\n')
    config = tmp_path / "config"
    _write_user_dirs(config, 'XDG_DESKTOP_DIR="$HOME/Chosen"\n')
    data = tmp_path / "data"
    data.mkdir()
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(config), "XDG_DATA_HOME": str(data)}
    return _Scenario(env, home / "Chosen", None)


def _relative_xdg_config_home_ignored(tmp_path: Path) -> _Scenario:
    home = _make_home(tmp_path, "FromDotConfig")
    _write_user_dirs(home / ".config", 'XDG_DESKTOP_DIR="$HOME/FromDotConfig"\n')
    return _Scenario({"HOME": str(home), "XDG_CONFIG_HOME": "relative/config"}, home / "FromDotConfig", None)


_LINUX_SCENARIOS: dict[str, Callable[[Path], _Scenario]] = {
    "user-dirs-home-relative": _user_dirs_home_relative,
    "user-dirs-absolute": _user_dirs_absolute,
    "xdg-env-var-wins": _xdg_env_wins,
    "no-folders": _no_folders,
    "home-fallbacks": _home_fallbacks,
    "xdg-config-home-elsewhere": _xdg_config_home_elsewhere,
    "relative-xdg-config-home-ignored": _relative_xdg_config_home_ignored,
}


def _without_xdg(key: str) -> bool:
    """Drop the host's ``XDG_*`` variables so only the scenario's own reach either side."""
    return key.startswith("XDG_")


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Linux (XDG) rules")
@pytest.mark.parametrize("build", list(_LINUX_SCENARIOS.values()), ids=list(_LINUX_SCENARIOS))
def test_linux_scenario_matches_pwsh(tmp_path: Path, build: Callable[[Path], _Scenario]) -> None:
    """In a scratch home every member equals pwsh's answer, and pwsh confirms the scenario."""
    scenario = build(tmp_path)

    theirs = _compare_posix(_scratch_env(scenario.env, drop=_without_xdg))

    assert theirs[KnownFolder.DESKTOP] == scenario.desktop, "pwsh did not see the scenario as built"
    assert theirs[KnownFolder.DOCUMENTS] == scenario.documents, "pwsh did not see the scenario as built"


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Linux (XDG) rules")
@pytest.mark.skipif(_IS_ROOT, reason="root passes access(R_OK) on a mode-000 directory")
def test_linux_unreadable_desktop_matches_pwsh(tmp_path: Path) -> None:
    """A Desktop that fails access(R_OK) is ``""`` to pwsh and ``None`` to pwshpy."""
    home = _make_home(tmp_path, "Desktop")
    desktop = home / "Desktop"
    desktop.chmod(0o000)
    try:
        theirs = _compare_posix(_scratch_env({"HOME": str(home)}, drop=_without_xdg))
    finally:
        desktop.chmod(0o755)
    assert theirs[KnownFolder.DESKTOP] is None, "pwsh did not see the unreadable Desktop"


# --- macOS (runs in CI only) ------------------------------------------------------------------

_MACOS_USER_DIRS = ("Desktop", "Documents", "Library/Application Support")


def _macos_home(root: Path) -> Path:
    for sub in _MACOS_USER_DIRS:
        (root / sub).mkdir(parents=True)
    return root


def _passwd_home() -> Path:
    """Return the effective uid's passwd home, the one CoreFoundation picks before HOME."""
    if sys.platform == "win32":
        raise AssertionError("no passwd database on Windows")
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def _home_pwsh_followed(theirs: Mapping[KnownFolder, Path | None], *, decoy: Path, passwd_home: Path) -> str:
    """Name the home pwsh's per-user answers sit under: HOME (the decoy), the passwd entry, or neither."""
    homes = {parent for answer in theirs.values() if answer is not None for parent in answer.parents}
    if decoy in homes:
        return f"HOME ({decoy})"
    if passwd_home in homes:
        return f"the passwd entry ({passwd_home})"
    return "neither HOME nor the passwd entry"


@pytest.mark.os_macos
@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS (CoreFoundation home) rules")
def test_macos_decoy_home_matches_pwsh(tmp_path: Path) -> None:
    """HOME pointing at a decoy settles which home .NET uses on macOS.

    ``posix_known_folder`` follows CoreFoundation (CFFIXED_USER_HOME, then the passwd entry,
    then HOME), inferred from source and never measured. The decoy holds every per-user
    folder, so pwsh lands in it only if it follows HOME; the failure names the order pwsh took.
    """
    decoy = _macos_home(tmp_path / "decoy")
    passwd_home = _passwd_home()
    assert passwd_home != decoy
    env = _scratch_env({"HOME": str(decoy)}, drop=lambda key: key == "CFFIXED_USER_HOME")

    theirs = _pwsh_folders(env)
    ours = _ours(lambda member: posix_known_folder(member, environ=env))
    expected = _expected(theirs, posix=True)

    followed = _home_pwsh_followed(theirs, decoy=decoy, passwd_home=passwd_home)
    assert ours == expected, f"pwsh followed {followed}; {_differences(ours, expected)}"


@pytest.mark.os_macos
@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS (CoreFoundation home) rules")
def test_macos_cffixed_user_home_matches_pwsh(tmp_path: Path) -> None:
    """CFFIXED_USER_HOME set: every member equals pwsh's answer (pwshpy puts it first)."""
    fixed = _macos_home(tmp_path / "fixed")
    env = _scratch_env({"CFFIXED_USER_HOME": str(fixed)}, drop=lambda _key: False)

    theirs = _compare_posix(env)

    passwd_home = _passwd_home()
    followed = _home_pwsh_followed(theirs, decoy=fixed, passwd_home=passwd_home)
    assert theirs[KnownFolder.DESKTOP] == fixed / "Desktop", f"pwsh followed {followed}"


# --- Windows ----------------------------------------------------------------------------------


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="the Windows resolver needs SHGetKnownFolderPath")
def test_windows_all_six_match_pwsh() -> None:
    """Every member equals pwsh's answer exactly, through the real pywin32."""
    theirs = _pwsh_folders(_scratch_env({}, drop=lambda _key: False))
    ours = _ours(windows_known_folder)
    expected = _expected(theirs, posix=False)

    assert ours == expected, _differences(ours, expected)
    assert theirs[KnownFolder.PROGRAM_DATA] is not None, "pwsh answered no ProgramData: the oracle compared nothing"
