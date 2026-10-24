"""POSIX known folders: the path pwsh's ``[Environment]::GetFolderPath`` returns on Linux and macOS.

Every case drives the real resolver over real ``tmp_path`` directories and real
``user-dirs.dirs`` bytes, with the environment handed in through the ``environ=`` seam and the
OS through ``platform=``, so nothing global is patched.  The expected values follow the .NET
Unix rules (``Environment.GetFolderPathCore.Unix.cs``): an absolute ``XDG_*_DIR`` variable wins,
then the first valid line of ``user-dirs.dirs``, then ``<home>/<fallback>``; the result must be
readable (``access(R_OK)``) or the call answers ``None`` where pwsh answers ``""``.  The macOS
table follows the same file's ``TARGET_OSX`` arm, with the home CoreFoundation resolves.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pwshpy.adapters.native.known_folders import posix_known_folder
from pwshpy.domain.enums import KnownFolder
from pwshpy.domain.errors import PlatformUnsupportedError

if sys.platform != "win32":
    import pwd

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX known-folder rules need POSIX paths")
_IS_ROOT = sys.platform != "win32" and os.geteuid() == 0
_NOT_AS_ROOT = pytest.mark.skipif(_IS_ROOT or sys.platform == "win32", reason="root passes access(R_OK) on mode 000")


def _home(tmp_path: Path, *subdirs: str) -> Path:
    """Create a scratch home with the given subdirectories and return it."""
    home = tmp_path / "home"
    home.mkdir()
    for sub in subdirs:
        (home / sub).mkdir(parents=True)
    return home


def _write_user_dirs(config: Path, text: bytes) -> Path:
    """Write ``user-dirs.dirs`` with exactly ``text`` (bytes, so line endings are what the test says)."""
    config.mkdir(parents=True, exist_ok=True)
    path = config / "user-dirs.dirs"
    path.write_bytes(text)
    return path


def _linux(folder: KnownFolder, environ: dict[str, str]) -> Path | None:
    return posix_known_folder(folder, environ=environ, platform="linux")


def _readable_or_none(path: str) -> Path | None:
    return Path(path) if os.access(path, os.R_OK) else None


# --- XDG user dirs (Desktop / Documents) ------------------------------------------------------


@pytest.mark.os_posix
@_POSIX_ONLY
def test_absolute_xdg_env_var_wins_over_user_dirs_file(tmp_path: Path) -> None:
    home = _home(tmp_path, "FromFile")
    _write_user_dirs(home / ".config", b'XDG_DESKTOP_DIR="$HOME/FromFile"\n')
    from_env = tmp_path / "from_env"
    from_env.mkdir()

    result = _linux(KnownFolder.DESKTOP, {"HOME": str(home), "XDG_DESKTOP_DIR": str(from_env)})

    assert result == from_env


@pytest.mark.os_posix
@_POSIX_ONLY
def test_relative_xdg_env_var_is_ignored(tmp_path: Path) -> None:
    home = _home(tmp_path, "Desktop", "relative")

    result = _linux(KnownFolder.DESKTOP, {"HOME": str(home), "XDG_DESKTOP_DIR": "relative"})

    assert result == home / "Desktop"


@pytest.mark.os_posix
@_POSIX_ONLY
def test_user_dirs_home_relative_line(tmp_path: Path) -> None:
    home = _home(tmp_path, "Schreibtisch", "Dokumente")
    _write_user_dirs(
        home / ".config",
        b"# written by xdg-user-dirs-update\n"
        b'XDG_DESKTOP_DIR="$HOME/Schreibtisch"\n'
        b'XDG_DOCUMENTS_DIR="$HOME/Dokumente"\n',
    )
    environ = {"HOME": str(home)}

    assert _linux(KnownFolder.DESKTOP, environ) == home / "Schreibtisch"
    assert _linux(KnownFolder.DOCUMENTS, environ) == home / "Dokumente"


@pytest.mark.os_posix
@_POSIX_ONLY
def test_user_dirs_absolute_line_is_taken_verbatim(tmp_path: Path) -> None:
    home = _home(tmp_path)
    docs = tmp_path / "elsewhere" / "docs"
    docs.mkdir(parents=True)
    _write_user_dirs(home / ".config", f'XDG_DOCUMENTS_DIR="{docs}"\n'.encode())

    assert _linux(KnownFolder.DOCUMENTS, {"HOME": str(home)}) == docs


@pytest.mark.os_posix
@_POSIX_ONLY
def test_first_valid_line_wins_and_invalid_lines_are_skipped(tmp_path: Path) -> None:
    home = _home(tmp_path, "Wrong", "Right", "Later")
    _write_user_dirs(
        home / ".config",
        b"\n".join(
            [
                b"# XDG_DESKTOP_DIR is commented out here",
                b"XDG_DESKTOP_DIR",  # the key alone: the guards must stop before indexing past the end
                b"XDG_DESKTOP_DIR  =",
                b'XDG_DESKTOP_DIRX="$HOME/Wrong"',  # a longer key is a different key
                b'XDG_DESKTOP_DIR="${HOME}/Wrong"',  # only the literal $HOME/ prefix is expanded
                b'XDG_DESKTOP_DIR="~/Wrong"',
                b"XDG_DESKTOP_DIR=$HOME/Wrong",  # unquoted
                b'XDG_DESKTOP_DIR="$HOME/"',  # an empty remainder is skipped
                b'XDG_DESKTOP_DIR="$HOME/Wrong',  # no closing quote
                b'  XDG_DESKTOP_DIR \t=  "$HOME/Right"',
                b'XDG_DESKTOP_DIR="$HOME/Later"',
                b"",
            ]
        ),
    )

    assert _linux(KnownFolder.DESKTOP, {"HOME": str(home)}) == home / "Right"


@pytest.mark.os_posix
@_POSIX_ONLY
def test_user_dirs_honours_absolute_xdg_config_home_only(tmp_path: Path) -> None:
    home = _home(tmp_path, "ViaConfigHome", "ViaDotConfig")
    config_home = tmp_path / "cfg"
    _write_user_dirs(config_home, b'XDG_DESKTOP_DIR="$HOME/ViaConfigHome"\n')
    _write_user_dirs(home / ".config", b'XDG_DESKTOP_DIR="$HOME/ViaDotConfig"\n')

    absolute = _linux(KnownFolder.DESKTOP, {"HOME": str(home), "XDG_CONFIG_HOME": str(config_home)})
    relative = _linux(KnownFolder.DESKTOP, {"HOME": str(home), "XDG_CONFIG_HOME": "cfg"})

    assert absolute == home / "ViaConfigHome"
    assert relative == home / "ViaDotConfig"


@pytest.mark.os_posix
@_POSIX_ONLY
def test_fallback_is_home_desktop_when_no_file(tmp_path: Path) -> None:
    home = _home(tmp_path, "Desktop", "Documents")

    assert _linux(KnownFolder.DESKTOP, {"HOME": str(home)}) == home / "Desktop"
    assert _linux(KnownFolder.DOCUMENTS, {"HOME": str(home)}) == home / "Documents"


@pytest.mark.os_posix
@_POSIX_ONLY
def test_missing_folder_is_none(tmp_path: Path) -> None:
    home = _home(tmp_path)

    assert _linux(KnownFolder.DESKTOP, {"HOME": str(home)}) is None
    assert _linux(KnownFolder.LOCAL_APP_DATA, {"HOME": str(home)}) is None


@pytest.mark.os_posix
@_POSIX_ONLY
def test_home_relative_value_uses_dotnet_path_join(tmp_path: Path) -> None:
    """``$HOME//Sub`` joins as ``<home>/Sub`` (Path.Join), not as the rooted ``/Sub`` os.path.join gives."""
    home = _home(tmp_path, "Sub")
    _write_user_dirs(home / ".config", b'XDG_DESKTOP_DIR="$HOME//Sub"\n')

    assert _linux(KnownFolder.DESKTOP, {"HOME": str(home)}) == home / "Sub"


@pytest.mark.os_posix
@_POSIX_ONLY
def test_one_character_value_needs_trailing_text_to_pass_the_dotnet_length_guards(tmp_path: Path) -> None:
    """.NET skips ``KEY="/"`` at end of line and ``KEY = "/"`` but accepts ``KEY="/" `` (literal guards)."""
    home = _home(tmp_path, "Desktop")
    environ = {"HOME": str(home)}

    _write_user_dirs(home / ".config", b'XDG_DESKTOP_DIR="/"\n')
    assert _linux(KnownFolder.DESKTOP, environ) == home / "Desktop"

    _write_user_dirs(home / ".config", b'XDG_DESKTOP_DIR = "/"\n')
    assert _linux(KnownFolder.DESKTOP, environ) == home / "Desktop"

    _write_user_dirs(home / ".config", b'XDG_DESKTOP_DIR="/" \n')
    assert _linux(KnownFolder.DESKTOP, environ) == Path("/")


@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize(
    "text",
    [
        b'XDG_DESKTOP_DIR="$HOME/Sub"\r\n',
        b'# comment\rXDG_DESKTOP_DIR="$HOME/Sub"\r',
        b'\xef\xbb\xbfXDG_DESKTOP_DIR="$HOME/Sub"\n',
        b"\xff\xfe" + 'XDG_DESKTOP_DIR="$HOME/Sub"\n'.encode("utf-16-le"),
        b"\xfe\xff" + 'XDG_DESKTOP_DIR="$HOME/Sub"\n'.encode("utf-16-be"),
    ],
    ids=["crlf", "cr-only", "utf8-bom", "utf16le-bom", "utf16be-bom"],
)
def test_user_dirs_line_endings_and_byte_order_marks_like_streamreader(tmp_path: Path, text: bytes) -> None:
    home = _home(tmp_path, "Sub")
    _write_user_dirs(home / ".config", text)

    assert _linux(KnownFolder.DESKTOP, {"HOME": str(home)}) == home / "Sub"


@pytest.mark.os_posix
@_POSIX_ONLY
def test_whitespace_is_dotnet_char_is_white_space_not_str_isspace(tmp_path: Path) -> None:
    """U+001C is whitespace to Python's isspace but not to .NET; NBSP and EM SPACE are to both."""
    home = _home(tmp_path, "Wrong", "Right")
    _write_user_dirs(
        home / ".config",
        b'\x1cXDG_DESKTOP_DIR="$HOME/Wrong"\n\xc2\xa0\xe2\x80\x83XDG_DESKTOP_DIR="$HOME/Right"\n',
    )

    assert _linux(KnownFolder.DESKTOP, {"HOME": str(home)}) == home / "Right"


@pytest.mark.os_posix
@_POSIX_ONLY
def test_lines_split_only_on_cr_and_lf_like_readline(tmp_path: Path) -> None:
    """NEL (U+0085) ends a line for str.splitlines but not for StreamReader.ReadLine."""
    home = _home(tmp_path, "Desktop", "Wrong")
    _write_user_dirs(home / ".config", b'XDG_DOCUMENTS_DIR="$HOME/x"\xc2\x85XDG_DESKTOP_DIR="$HOME/Wrong"\n')

    assert _linux(KnownFolder.DESKTOP, {"HOME": str(home)}) == home / "Desktop"


@pytest.mark.os_posix
@_NOT_AS_ROOT
def test_unreadable_user_dirs_file_falls_back(tmp_path: Path) -> None:
    home = _home(tmp_path, "Desktop", "Sub")
    user_dirs = _write_user_dirs(home / ".config", b'XDG_DESKTOP_DIR="$HOME/Sub"\n')
    user_dirs.chmod(0)
    try:
        assert _linux(KnownFolder.DESKTOP, {"HOME": str(home)}) == home / "Desktop"
    finally:
        user_dirs.chmod(0o600)


@pytest.mark.os_posix
@_POSIX_ONLY
def test_user_dirs_that_is_a_directory_falls_back(tmp_path: Path) -> None:
    home = _home(tmp_path, "Desktop", ".config/user-dirs.dirs")

    assert _linux(KnownFolder.DESKTOP, {"HOME": str(home)}) == home / "Desktop"


@pytest.mark.os_posix
@_NOT_AS_ROOT
def test_unreadable_folder_is_none(tmp_path: Path) -> None:
    home = _home(tmp_path, "Desktop")
    (home / "Desktop").chmod(0)
    try:
        assert _linux(KnownFolder.DESKTOP, {"HOME": str(home)}) is None
    finally:
        (home / "Desktop").chmod(0o700)


@pytest.mark.os_posix
@_POSIX_ONLY
def test_result_is_checked_as_the_raw_string_not_the_normalised_path(tmp_path: Path) -> None:
    """``<file>/`` fails access(R_OK) with ENOTDIR although Path() would drop the trailing slash."""
    a_file = tmp_path / "plain"
    a_file.write_bytes(b"")

    assert _linux(KnownFolder.DESKTOP, {"HOME": str(tmp_path), "XDG_DESKTOP_DIR": f"{a_file}/"}) is None
    assert _linux(KnownFolder.DESKTOP, {"HOME": str(tmp_path), "XDG_DESKTOP_DIR": str(a_file)}) == a_file


# --- XDG base dirs (application data) -----------------------------------------------------------


@pytest.mark.os_posix
@_POSIX_ONLY
def test_roaming_app_data_is_absolute_xdg_config_home_else_dot_config(tmp_path: Path) -> None:
    home = _home(tmp_path, ".config")
    config_home = tmp_path / "cfg"
    config_home.mkdir()

    def roaming(config_value: str) -> Path | None:
        return _linux(KnownFolder.ROAMING_APP_DATA, {"HOME": str(home), "XDG_CONFIG_HOME": config_value})

    assert roaming(str(config_home)) == config_home
    assert roaming("cfg") == home / ".config"
    assert roaming("") == home / ".config"


@pytest.mark.os_posix
@_POSIX_ONLY
def test_local_app_data_is_absolute_xdg_data_home_else_local_share(tmp_path: Path) -> None:
    home = _home(tmp_path, ".local/share")
    data_home = tmp_path / "data"
    data_home.mkdir()

    def local(data_value: str) -> Path | None:
        return _linux(KnownFolder.LOCAL_APP_DATA, {"HOME": str(home), "XDG_DATA_HOME": data_value})

    assert local(str(data_home)) == data_home
    assert local("data") == home / ".local" / "share"


@pytest.mark.os_posix
@_POSIX_ONLY
def test_program_data_is_usr_share_whatever_the_environment(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path), "XDG_DATA_DIRS": str(tmp_path), "CFFIXED_USER_HOME": str(tmp_path)}
    expected = _readable_or_none("/usr/share")

    assert _linux(KnownFolder.PROGRAM_DATA, environ) == expected
    assert posix_known_folder(KnownFolder.PROGRAM_DATA, environ=environ, platform="darwin") == expected


# --- home resolution -----------------------------------------------------------------------------


@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize("home_entry", [{"HOME": ""}, {}], ids=["empty-home", "unset-home"])
def test_empty_or_unset_home_uses_the_effective_uids_passwd_entry(tmp_path: Path, home_entry: dict[str, str]) -> None:
    """``"$HOME/."`` names the passwd home itself, so no subdirectory of it has to exist."""
    if sys.platform == "win32":  # narrows for pyright; the skip marks keep Windows out at run time
        pytest.skip("POSIX only")
    config_home = tmp_path / "cfg"
    _write_user_dirs(config_home, b'XDG_DESKTOP_DIR="$HOME/."\n')
    passwd_home = pwd.getpwuid(os.geteuid()).pw_dir

    result = _linux(KnownFolder.DESKTOP, {**home_entry, "XDG_CONFIG_HOME": str(config_home)})

    assert result == _readable_or_none(f"{passwd_home}/.")


# --- macOS ---------------------------------------------------------------------------------------


@pytest.mark.os_posix
@_POSIX_ONLY
def test_macos_table_under_cffixed_user_home(tmp_path: Path) -> None:
    home = _home(tmp_path, "Desktop", "Documents", "Library/Application Support")
    environ = {"CFFIXED_USER_HOME": str(home), "XDG_DESKTOP_DIR": str(tmp_path), "XDG_DATA_HOME": str(tmp_path)}

    def mac(folder: KnownFolder) -> Path | None:
        return posix_known_folder(folder, environ=environ, platform="darwin")

    assert mac(KnownFolder.DESKTOP) == home / "Desktop"
    assert mac(KnownFolder.DOCUMENTS) == home / "Documents"
    assert mac(KnownFolder.ROAMING_APP_DATA) == home / "Library" / "Application Support"
    assert mac(KnownFolder.LOCAL_APP_DATA) == home / "Library" / "Application Support"


@pytest.mark.os_posix
@_POSIX_ONLY
def test_macos_home_is_the_passwd_entry_before_home(tmp_path: Path) -> None:
    """CoreFoundation's home: CFFIXED_USER_HOME, then the passwd entry, and HOME only last."""
    if sys.platform == "win32":  # narrows for pyright; the skip marks keep Windows out at run time
        pytest.skip("POSIX only")
    euid = os.geteuid()
    passwd_home = pwd.getpwuid(euid if euid else os.getuid()).pw_dir
    decoy = _home(tmp_path, "Desktop")

    result = posix_known_folder(KnownFolder.DESKTOP, environ={"HOME": str(decoy)}, platform="darwin")

    assert result != decoy / "Desktop" or passwd_home == str(decoy)
    assert result == _readable_or_none(f"{passwd_home}/Desktop")


# --- Programs and the defaults -------------------------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_programs_is_platform_unsupported_and_names_the_member(platform: str) -> None:
    with pytest.raises(PlatformUnsupportedError, match="Programs"):
        posix_known_folder(KnownFolder.PROGRAMS, environ={}, platform=platform)


@pytest.mark.os_posix
@_POSIX_ONLY
def test_defaults_read_the_running_platform_and_environment() -> None:
    assert posix_known_folder(KnownFolder.PROGRAM_DATA) == _readable_or_none("/usr/share")
    with pytest.raises(PlatformUnsupportedError, match="Programs"):
        posix_known_folder(KnownFolder.PROGRAMS)
