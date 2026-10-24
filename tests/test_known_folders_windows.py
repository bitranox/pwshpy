"""Windows known folders: ``SHGetKnownFolderPath`` with .NET's fallback, through the ``load=`` seam.

The resolver asks the shell for each member's FOLDERID with flags 0 (verify the folder exists),
as .NET's ``Environment.Win32.cs`` does. A "not found" HRESULT (0x80070002 / 0x80070003) gives
``None``, except for the three application-data members, which take their environment variable
when it names an existing directory, as pwsh does. Any other HRESULT is a ``NativeCallError``;
anything that is not a COM error is not pwshpy's to translate and propagates unchanged.

The shell module is a double handed in through ``load=``, so every case runs on any OS; the
``os_windows`` cases at the end call the real pywin32.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from pwshpy.adapters.native.known_folders import windows_known_folder
from pwshpy.domain.enums import KnownFolder
from pwshpy.domain.errors import NativeCallError

_SHELL_MODULE = "win32com.shell.shell"
_FILE_NOT_FOUND = 0x80070002
_PATH_NOT_FOUND = 0x80070003
_E_FAIL = 0x80004005

#: The FOLDERIDs from the Windows SDK (KnownFolders.h), as measured against pywin32 312.
_EXPECTED_IDS: dict[KnownFolder, str] = {
    KnownFolder.DESKTOP: "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    KnownFolder.DOCUMENTS: "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    KnownFolder.LOCAL_APP_DATA: "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
    KnownFolder.ROAMING_APP_DATA: "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",
    KnownFolder.PROGRAM_DATA: "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}",
    KnownFolder.PROGRAMS: "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}",
}

#: pywin32's ``shell.FOLDERID_<name>`` attribute for each member.
_PYWIN32_NAMES: dict[KnownFolder, str] = {
    KnownFolder.DESKTOP: "FOLDERID_Desktop",
    KnownFolder.DOCUMENTS: "FOLDERID_Documents",
    KnownFolder.LOCAL_APP_DATA: "FOLDERID_LocalAppData",
    KnownFolder.ROAMING_APP_DATA: "FOLDERID_RoamingAppData",
    KnownFolder.PROGRAM_DATA: "FOLDERID_ProgramData",
    KnownFolder.PROGRAMS: "FOLDERID_Programs",
}

#: The members .NET falls back to an environment variable for, and that variable.
_FALLBACK_VARS: dict[KnownFolder, str] = {
    KnownFolder.LOCAL_APP_DATA: "LOCALAPPDATA",
    KnownFolder.ROAMING_APP_DATA: "APPDATA",
    KnownFolder.PROGRAM_DATA: "ProgramData",
}

_NO_FALLBACK = [KnownFolder.DESKTOP, KnownFolder.DOCUMENTS, KnownFolder.PROGRAMS]


class com_error(Exception):  # noqa: N801, N818 - pywin32's class name is what the resolver recognises
    """A stand-in for ``pywintypes.com_error``: the HRESULT arrives signed, on ``.hresult``."""

    def __init__(self, code: int) -> None:
        signed = code - 0x1_0000_0000 if code & 0x8000_0000 else code
        super().__init__(signed, "localized text the resolver must not rely on", None, None)
        self.hresult = signed


class _FakeShell:
    """A ``win32com.shell.shell`` double: records each call and answers with one outcome."""

    def __init__(self, outcome: str | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[tuple[object, ...]] = []

    def SHGetKnownFolderPath(self, *args: object) -> str:  # noqa: N802 - the pywin32 name
        self.calls.append(args)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _loader(shell: _FakeShell) -> Any:
    def load(module: str) -> Any:
        assert module == _SHELL_MODULE
        return shell

    return load


def _resolve(folder: KnownFolder, outcome: str | BaseException, environ: Mapping[str, str]) -> Path | None:
    return windows_known_folder(folder, environ=environ, load=_loader(_FakeShell(outcome)))


# --- the call ------------------------------------------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize("folder", list(KnownFolder), ids=lambda f: f.name)
def test_each_member_asks_for_its_folderid_with_flags_0(folder: KnownFolder) -> None:
    shell = _FakeShell("C:\\Users\\someone\\Somewhere")

    result = windows_known_folder(folder, environ={}, load=_loader(shell))

    assert result == Path("C:\\Users\\someone\\Somewhere")
    assert shell.calls == [(_EXPECTED_IDS[folder], 0, None)]


# --- the not-found fallback (Hybrid) -------------------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize("code", [_FILE_NOT_FOUND, _PATH_NOT_FOUND], ids=lambda c: f"0x{c:08X}")
@pytest.mark.parametrize("folder", list(_FALLBACK_VARS), ids=lambda f: f.name)
def test_not_found_app_data_takes_its_env_var_when_that_directory_exists(
    tmp_path: Path, folder: KnownFolder, code: int
) -> None:
    result = _resolve(folder, com_error(code), {_FALLBACK_VARS[folder]: str(tmp_path)})

    assert result == tmp_path


@pytest.mark.os_agnostic
@pytest.mark.parametrize("code", [_FILE_NOT_FOUND, _PATH_NOT_FOUND], ids=lambda c: f"0x{c:08X}")
@pytest.mark.parametrize("folder", list(_FALLBACK_VARS), ids=lambda f: f.name)
def test_not_found_app_data_is_none_when_its_env_var_names_a_missing_directory(
    tmp_path: Path, folder: KnownFolder, code: int
) -> None:
    result = _resolve(folder, com_error(code), {_FALLBACK_VARS[folder]: str(tmp_path / "missing")})

    assert result is None


@pytest.mark.os_agnostic
@pytest.mark.parametrize("code", [_FILE_NOT_FOUND, _PATH_NOT_FOUND], ids=lambda c: f"0x{c:08X}")
@pytest.mark.parametrize("folder", list(_FALLBACK_VARS), ids=lambda f: f.name)
def test_not_found_app_data_is_none_when_its_env_var_is_unset(tmp_path: Path, folder: KnownFolder, code: int) -> None:
    """The other two variables point at an existing directory, so only the member's own counts."""
    others = {var: str(tmp_path) for member, var in _FALLBACK_VARS.items() if member is not folder}

    result = _resolve(folder, com_error(code), others)

    assert result is None


@pytest.mark.os_agnostic
@pytest.mark.parametrize("code", [_FILE_NOT_FOUND, _PATH_NOT_FOUND], ids=lambda c: f"0x{c:08X}")
@pytest.mark.parametrize("folder", _NO_FALLBACK, ids=lambda f: f.name)
def test_not_found_desktop_documents_programs_are_none_whatever_the_environment(
    tmp_path: Path, folder: KnownFolder, code: int
) -> None:
    every_var = {var: str(tmp_path) for var in _FALLBACK_VARS.values()}

    result = _resolve(folder, com_error(code), every_var)

    assert result is None


@pytest.mark.os_agnostic
def test_fallback_env_var_name_is_matched_case_insensitively(tmp_path: Path) -> None:
    """Windows environment names ignore case; a caller's dict may spell ``ProgramData`` in capitals."""
    result = _resolve(KnownFolder.PROGRAM_DATA, com_error(_FILE_NOT_FOUND), {"PROGRAMDATA": str(tmp_path)})

    assert result == tmp_path


@pytest.mark.os_agnostic
def test_empty_fallback_env_var_is_unset_not_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Path("")`` is ``.``, an existing directory; an empty value must not turn into the cwd."""
    monkeypatch.chdir(tmp_path)

    result = _resolve(KnownFolder.LOCAL_APP_DATA, com_error(_FILE_NOT_FOUND), {"LOCALAPPDATA": ""})

    assert result is None


@pytest.mark.os_agnostic
def test_environ_none_reads_the_process_environment_per_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _FakeShell(com_error(_FILE_NOT_FOUND))
    monkeypatch.delenv("APPDATA", raising=False)
    assert windows_known_folder(KnownFolder.ROAMING_APP_DATA, load=_loader(shell)) is None

    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert windows_known_folder(KnownFolder.ROAMING_APP_DATA, load=_loader(shell)) == tmp_path


@pytest.mark.os_posix
@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs a POSIX mode-000 directory that non-root cannot search"
)
def test_fallback_directory_behind_an_unsearchable_parent_is_none(tmp_path: Path) -> None:
    """A stat refused with EACCES (raised by ``is_dir()`` before Python 3.14) counts as missing."""
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        result = _resolve(
            KnownFolder.LOCAL_APP_DATA, com_error(_FILE_NOT_FOUND), {"LOCALAPPDATA": str(locked / "inner")}
        )
    finally:
        locked.chmod(0o700)

    assert result is None


# --- other failures ------------------------------------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize("folder", [KnownFolder.DESKTOP, KnownFolder.LOCAL_APP_DATA], ids=lambda f: f.name)
def test_other_hresult_raises_native_call_error_naming_code_and_member(tmp_path: Path, folder: KnownFolder) -> None:
    """E_FAIL (measured for a virtual folder) is not "missing": no fallback, even with the variable set."""
    error = com_error(_E_FAIL)

    with pytest.raises(NativeCallError) as info:
        _resolve(folder, error, {var: str(tmp_path) for var in _FALLBACK_VARS.values()})

    assert "0x80004005" in str(info.value)
    assert folder.name in str(info.value)
    assert info.value.__cause__ is error


@pytest.mark.os_agnostic
def test_com_error_without_an_hresult_raises_native_call_error(tmp_path: Path) -> None:
    error = com_error(_FILE_NOT_FOUND)
    del error.hresult

    with pytest.raises(NativeCallError):
        _resolve(KnownFolder.PROGRAM_DATA, error, {"ProgramData": str(tmp_path)})


@pytest.mark.os_agnostic
def test_non_com_exception_propagates_unchanged() -> None:
    error = RuntimeError("not a COM failure")

    with pytest.raises(RuntimeError) as info:
        _resolve(KnownFolder.DESKTOP, error, {})

    assert info.value is error


# --- the real pywin32 ----------------------------------------------------------------------------


_WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="calls the real SHGetKnownFolderPath")


@pytest.mark.os_windows
@_WINDOWS_ONLY
@pytest.mark.parametrize("folder", list(KnownFolder), ids=lambda f: f.name)
def test_folderid_table_matches_pywin32(folder: KnownFolder) -> None:
    from win32com.shell import shell

    assert str(getattr(shell, _PYWIN32_NAMES[folder])).upper() == _EXPECTED_IDS[folder]


@pytest.mark.os_windows
@_WINDOWS_ONLY
@pytest.mark.parametrize("folder", list(KnownFolder), ids=lambda f: f.name)
def test_real_call_answers_an_existing_directory_or_none(folder: KnownFolder) -> None:
    result = windows_known_folder(folder)

    assert result is None or result.is_dir()
    if folder is KnownFolder.PROGRAM_DATA:
        assert result == Path(os.environ["PROGRAMDATA"])
