"""Known folders: the path pwsh's ``[Environment]::GetFolderPath(<SpecialFolder>)`` returns.

The rules reproduce .NET's own resolver (dotnet/runtime ``Environment.GetFolderPathCore.Unix.cs``
and ``PersistedFiles.Unix.cs``), so pwshpy answers what pwsh answers on the same machine:

- Linux (any non-macOS POSIX): Desktop and MyDocuments come from ``ReadXdgDirectory`` - an
  absolute ``XDG_DESKTOP_DIR`` / ``XDG_DOCUMENTS_DIR`` variable, else the first valid line of
  ``<XDG_CONFIG_HOME or ~/.config>/user-dirs.dirs``, else ``<home>/Desktop`` / ``<home>/Documents``.
  ApplicationData is an absolute ``XDG_CONFIG_HOME`` else ``<home>/.config``;
  LocalApplicationData an absolute ``XDG_DATA_HOME`` else ``<home>/.local/share``. The home is
  ``HOME`` when non-empty, else the effective uid's passwd entry, else ``/``.
- macOS: .NET asks Foundation (``NSFileManager URLsForDirectory``), which builds on
  CoreFoundation's home: ``CFFIXED_USER_HOME``, else the passwd entry of the effective uid (the
  real uid when that is root), and ``HOME`` only when neither exists. Desktop, Documents and
  ``Library/Application Support`` (both application-data members) sit under that home, spelled
  as Foundation standardizes them: a leading ``/private/var/automount``, ``/var/automount`` or
  ``/private`` component is dropped when the rest still exists (Apple's
  ``stringByStandardizingPath`` documentation), so a home under ``/private/var/folders/xx/...``
  answers ``/var/folders/xx/...``.
- Both: CommonApplicationData is ``/usr/share``; Programs has no folder (.NET answers ``""``),
  which this module refuses with :class:`PlatformUnsupportedError`. Like .NET's default
  ``SpecialFolderOption.None``, a path that fails ``access(R_OK)`` is not returned: the
  resolver answers ``None`` where pwsh answers ``""``.
- Windows (``Environment.Win32.cs``): ``SHGetKnownFolderPath(<FOLDERID>, 0, NULL)``, which
  verifies the folder exists. When it reports the folder missing (``0x80070002`` /
  ``0x80070003``), ApplicationData, LocalApplicationData and CommonApplicationData fall back to
  ``APPDATA`` / ``LOCALAPPDATA`` / ``ProgramData`` like pwsh, but only to an existing directory;
  every other member answers ``None``. Any other HRESULT raises :class:`NativeCallError` where
  .NET would answer ``""`` silently.

Contents:
    * :func:`posix_known_folder` - the Linux / macOS resolver.
    * :func:`windows_known_folder` - the Windows resolver.
"""

from __future__ import annotations

import importlib
import io
import os
import sys
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ...domain.enums import KnownFolder
from ...domain.errors import PlatformUnsupportedError
from .marshal import com_hresult, is_com_error, native_error

if sys.platform != "win32":
    import pwd

__all__ = ["posix_known_folder", "windows_known_folder"]

_COMMON_APPLICATION_DATA = "/usr/share"
_USER_DIRS_FILE = "user-dirs.dirs"
_HOME_PREFIX = "$HOME/"

#: Desktop / MyDocuments on Linux: the ``user-dirs.dirs`` key and the fallback under home.
_XDG_USER_DIRS: dict[KnownFolder, tuple[str, str]] = {
    KnownFolder.DESKTOP: ("XDG_DESKTOP_DIR", "Desktop"),
    KnownFolder.DOCUMENTS: ("XDG_DOCUMENTS_DIR", "Documents"),
}

#: The macOS user-domain search paths, relative to CoreFoundation's home.
_MACOS_USER_DIRS: dict[KnownFolder, str] = {
    KnownFolder.DESKTOP: "Desktop",
    KnownFolder.DOCUMENTS: "Documents",
    KnownFolder.ROAMING_APP_DATA: "Library/Application Support",
    KnownFolder.LOCAL_APP_DATA: "Library/Application Support",
}

#: The leading components Foundation's path standardization drops, in Apple's documented order.
_FOUNDATION_PREFIXES = ("/private/var/automount", "/var/automount", "/private")

#: Windows: each member's FOLDERID (KnownFolders.h; pywin32's ``shell.FOLDERID_<name>``).
_FOLDER_IDS: dict[KnownFolder, str] = {
    KnownFolder.DESKTOP: "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    KnownFolder.DOCUMENTS: "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    KnownFolder.LOCAL_APP_DATA: "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
    KnownFolder.ROAMING_APP_DATA: "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",
    KnownFolder.PROGRAM_DATA: "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}",
    KnownFolder.PROGRAMS: "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}",
}

#: Windows: the variable .NET answers when the shell does not find the folder.
_ENV_FALLBACKS: dict[KnownFolder, str] = {
    KnownFolder.LOCAL_APP_DATA: "LOCALAPPDATA",
    KnownFolder.ROAMING_APP_DATA: "APPDATA",
    KnownFolder.PROGRAM_DATA: "ProgramData",
}

#: ERROR_FILE_NOT_FOUND (the folder is missing) and ERROR_PATH_NOT_FOUND (its parent is too).
_NOT_FOUND = frozenset({0x80070002, 0x80070003})

#: .NET ``char.IsWhiteSpace`` for Latin-1; above it only the Unicode separator categories count.
#: Python's ``str.isspace`` also accepts U+001C..U+001F, which .NET does not skip.
_LATIN1_WHITESPACE = frozenset("\t\n\v\f\r \x85\xa0")
_SEPARATOR_CATEGORIES = frozenset({"Zs", "Zl", "Zp"})

#: Byte-order marks StreamReader detects, longest first (UTF-32 LE starts like UTF-16 LE).
_BYTE_ORDER_MARKS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def posix_known_folder(
    folder: KnownFolder,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path | None:
    """Return the folder pwsh's ``[Environment]::GetFolderPath`` names on Linux or macOS.

    POSIX-only by contract: the caller dispatches by OS and sends Windows to
    :func:`windows_known_folder`. Run on Windows it would build POSIX paths that mean nothing
    there.

    Args:
        folder: The known folder to resolve.
        environ: The environment to read ``HOME`` / ``XDG_*`` / ``CFFIXED_USER_HOME`` from;
            ``None`` reads :data:`os.environ` at call time.
        platform: ``"darwin"`` for the macOS rules, any other value for the Linux (XDG) rules;
            ``None`` reads :data:`sys.platform` at call time.

    Returns:
        The folder path, or ``None`` when it is missing or not readable (pwsh answers ``""``).

    Raises:
        PlatformUnsupportedError: ``folder`` is ``KnownFolder.PROGRAMS``, which .NET maps to no
            folder on Linux or macOS.

    Example:
        >>> from pwshpy.domain.enums import KnownFolder
        >>> try:
        ...     posix_known_folder(KnownFolder.PROGRAMS, environ={}, platform="linux")
        ... except PlatformUnsupportedError as exc:
        ...     print("Programs" in str(exc))
        True
        >>> found = posix_known_folder(KnownFolder.PROGRAM_DATA, environ={}, platform="linux")
        >>> sys.platform == "win32" or found == Path("/usr/share")
        True
    """
    env = os.environ if environ is None else environ
    on_macos = (sys.platform if platform is None else platform) == "darwin"
    if folder is KnownFolder.PROGRAMS:
        system = "macOS" if on_macos else "Linux"
        raise PlatformUnsupportedError(
            f"KnownFolder.PROGRAMS ({folder.value}) has no folder on {system}: "
            f"pwsh's [Environment]::GetFolderPath returns an empty string there; it exists on Windows only."
        )
    path = _special_folder(folder, env, on_macos=on_macos)
    # The raw string is checked, as .NET does: Path() would drop a trailing slash that makes
    # access() fail with ENOTDIR on a file.
    return Path(path) if _readable(path) else None


def _special_folder(folder: KnownFolder, env: Mapping[str, str], *, on_macos: bool) -> str:
    """Return the unverified path .NET's ``GetSpecialFolder`` builds (``""`` for none)."""
    if folder is KnownFolder.PROGRAM_DATA:
        return _COMMON_APPLICATION_DATA
    if on_macos:
        return _macos_folder(folder, env)
    return _xdg_folder(folder, env)


def _xdg_folder(folder: KnownFolder, env: Mapping[str, str]) -> str:
    """Return the Linux path for a per-user ``folder`` (the non-macOS arm of .NET)."""
    home = _dotnet_home(env)
    user_dir = _XDG_USER_DIRS.get(folder)
    if user_dir is not None:
        key, fallback = user_dir
        return _read_xdg_directory(home, env, key=key, fallback=fallback)
    if folder is KnownFolder.ROAMING_APP_DATA:
        return _xdg_config(home, env)
    data = _absolute_env(env, "XDG_DATA_HOME")
    return data if data is not None else _join(_join(home, ".local"), "share")


def _macos_folder(folder: KnownFolder, env: Mapping[str, str]) -> str:
    """Return the macOS path for a per-user ``folder`` under CoreFoundation's home."""
    home = _corefoundation_home(env)
    if not home:
        return ""
    # Foundation standardizes the URL it returns, so the whole joined path is what must exist.
    return _foundation_standardized(_join(home, _MACOS_USER_DIRS[folder]))


def _foundation_standardized(path: str, *, exists: Callable[[str], bool] = os.path.exists) -> str:
    """Drop a leading component Foundation's path standardization removes, as NSURL ``path`` does.

    Apple documents for ``-[NSString stringByStandardizingPath]`` that it removes an initial
    ``/private/var/automount``, ``/var/automount`` or ``/private`` component "if the result still
    indicates an existing file or directory (checked by consulting the file system)". The
    existence condition is argued from that documentation; the drop itself was observed on a
    macOS CI runner, where pwsh answered ``/var/folders/xx/.../Desktop`` for a
    ``CFFIXED_USER_HOME`` of ``/private/var/folders/xx/...``. Of the documented steps only this
    one changes an absolute path with no ``~``, ``.``, ``..``, doubled or trailing ``/``, and it is
    the only one reproduced. A prefix matches whole components only, so ``/privatex`` stays.

    Args:
        path: The absolute path Foundation would answer before standardization.
        exists: Whether a path names an existing file or directory (following symlinks, as
            Foundation's check does); defaults to :func:`os.path.exists`.

    Returns:
        ``path`` without the first listed prefix whose remainder exists, else ``path`` unchanged.

    Example:
        >>> _foundation_standardized("/private/var/folders/xx/Desktop", exists=lambda p: True)
        '/var/folders/xx/Desktop'
        >>> _foundation_standardized("/private/var/folders/xx/Desktop", exists=lambda p: False)
        '/private/var/folders/xx/Desktop'
        >>> _foundation_standardized("/privatex/Desktop", exists=lambda p: True)
        '/privatex/Desktop'
    """
    for prefix in _FOUNDATION_PREFIXES:
        if path.startswith(prefix + "/"):
            remainder = path[len(prefix) :]
            if exists(remainder):
                return remainder
    return path


def _dotnet_home(env: Mapping[str, str]) -> str:
    """Return .NET's home: non-empty ``HOME``, else the effective uid's passwd home, else ``/``."""
    home = env.get("HOME")
    if home:
        return home
    if sys.platform == "win32":
        return "/"
    return _passwd_home(os.geteuid()) or "/"


def _corefoundation_home(env: Mapping[str, str]) -> str:
    """Return CoreFoundation's home (``CFCopyHomeDirectoryURL``), ``""`` when none is known.

    ``CFFIXED_USER_HOME`` first, then the passwd entry of the effective uid (the real uid
    when the effective one is root), and ``HOME`` only when neither yields a directory.
    """
    fixed = env.get("CFFIXED_USER_HOME")
    if fixed:
        return fixed
    passwd_home = _corefoundation_passwd_home()
    return passwd_home or env.get("HOME", "")


def _corefoundation_passwd_home() -> str | None:
    """Return the passwd home of ``euid ? euid : getuid()``, as CoreFoundation picks the user."""
    if sys.platform == "win32":
        return None
    euid = os.geteuid()
    return _passwd_home(euid if euid else os.getuid())


def _passwd_home(uid: int) -> str | None:
    """Return the passwd home directory of ``uid``, ``None`` when it has no entry or an empty one."""
    if sys.platform == "win32":
        return None
    try:
        entry = pwd.getpwuid(uid)
    except KeyError:
        return None
    return entry.pw_dir or None


def _xdg_config(home: str, env: Mapping[str, str]) -> str:
    """Return .NET's ``GetXdgConfig``: an absolute ``XDG_CONFIG_HOME``, else ``<home>/.config``."""
    config = _absolute_env(env, "XDG_CONFIG_HOME")
    return config if config is not None else _join(home, ".config")


def _absolute_env(env: Mapping[str, str], key: str) -> str | None:
    """Return ``env[key]`` when it starts with ``/``; a relative or empty value counts as unset."""
    value = env.get(key)
    return value if value is not None and value.startswith("/") else None


def _read_xdg_directory(home: str, env: Mapping[str, str], *, key: str, fallback: str) -> str:
    """Return .NET's ``ReadXdgDirectory``: absolute env var, else ``user-dirs.dirs``, else fallback."""
    from_env = _absolute_env(env, key)
    if from_env is not None:
        return from_env
    user_dirs = _join(_xdg_config(home, env), _USER_DIRS_FILE)
    if _readable(user_dirs):
        found = _scan_user_dirs(user_dirs, home=home, key=key)
        if found is not None:
            return found
    return _join(home, fallback)


def _scan_user_dirs(path: str, *, home: str, key: str) -> str | None:
    """Return the first valid ``key`` line's value in ``path``; ``None`` when none or unreadable.

    Read like .NET's ``StreamReader``: UTF-8 unless a byte-order mark names UTF-16/32, invalid
    bytes replaced, lines ending at CR, LF or CRLF only, streamed line by line. Any read error is
    swallowed (.NET eats every exception here and takes the fallback).
    """
    try:
        # A FIFO here blocks the open until a writer appears; left unguarded on purpose, because
        # .NET's StreamReader blocks the same way and pwshpy answers what pwsh answers.
        with Path(path).open("rb") as raw:
            encoding = _stream_reader_encoding(raw.peek(4)[:4])
            text = io.TextIOWrapper(raw, encoding=encoding, errors="replace", newline=None)
            for line in text:
                value = _user_dirs_value(line.rstrip("\n"), home=home, key=key)
                if value is not None:
                    return value
    except OSError:
        return None
    return None


def _stream_reader_encoding(head: bytes) -> str:
    """Return the codec ``StreamReader`` picks for a file starting with ``head``."""
    for mark, encoding in _BYTE_ORDER_MARKS:
        if head.startswith(mark):
            return encoding
    return "utf-8"


def _user_dirs_value(line: str, *, home: str, key: str) -> str | None:
    """Parse one ``user-dirs.dirs`` line the way .NET does; ``None`` when it does not qualify.

    The length guards are .NET's literal ones (``pos >= Length - 4`` at ``=``, ``pos >= Length - 3``
    at the opening quote), so ``KEY="/"`` at end of line is skipped and ``KEY="/" `` is not. The
    first guard never decides a line the second would accept (it also keeps the ``=`` index in
    range); it stays so the code reads like the source it reproduces.
    Python counts code points where .NET counts UTF-16 units; the guards only decide lines whose
    remainder is at most four characters, which cannot hold a character outside the BMP and
    still qualify, so the two counts agree on every line that matters.
    """
    pos = _value_start(line, key)
    if pos is None:
        return None
    relative = line.startswith(_HOME_PREFIX, pos)
    if relative:
        pos += len(_HOME_PREFIX)
    elif line[pos] != "/":
        return None
    end = line.find('"', pos)
    if end <= pos:
        return None
    value = line[pos:end]
    return _join(home, value) if relative else value


def _value_start(line: str, key: str) -> int | None:
    """Return the index just past ``KEY <ws> = <ws> "`` in ``line``, or ``None`` if it is not one."""
    pos = _skip_whitespace(line, 0)
    if not line.startswith(key, pos):
        return None
    pos = _skip_whitespace(line, pos + len(key))
    if pos >= len(line) - 4 or line[pos] != "=":
        return None
    pos = _skip_whitespace(line, pos + 1)
    if pos >= len(line) - 3 or line[pos] != '"':
        return None
    return pos + 1


def _skip_whitespace(line: str, pos: int) -> int:
    """Return the first index at or after ``pos`` that is not .NET whitespace."""
    while pos < len(line) and _is_dotnet_whitespace(line[pos]):
        pos += 1
    return pos


def _is_dotnet_whitespace(char: str) -> bool:
    """Return whether ``char`` is whitespace to .NET's ``char.IsWhiteSpace``."""
    if char <= "\xff":
        return char in _LATIN1_WHITESPACE
    return unicodedata.category(char) in _SEPARATOR_CATEGORIES


def _join(first: str, second: str) -> str:
    """Join two path parts like .NET ``Path.Join`` on Unix: one ``/`` unless either side has it.

    ``Path.Combine`` would return a rooted ``second`` as is; every Combine in the .NET resolver
    joins a relative constant (``.config``, ``Desktop``), where the two agree, so one helper
    serves both. ``os.path.join`` differs: it lets a rooted ``second`` discard ``first``.
    """
    if not first:
        return second
    if not second:
        return first
    if first.endswith("/") or second.startswith("/"):
        return first + second
    return f"{first}/{second}"


def _readable(path: str) -> bool:
    """Return .NET's verification ``access(path, R_OK) == 0``; an empty path is never readable."""
    if not path:
        return False
    try:
        return os.access(path, os.R_OK)
    except ValueError:  # an embedded NUL from user-dirs.dirs: no such path can exist
        return False


def windows_known_folder(
    folder: KnownFolder,
    *,
    environ: Mapping[str, str] | None = None,
    load: Callable[[str], Any] | None = None,
) -> Path | None:
    """Return the folder pwsh's ``[Environment]::GetFolderPath`` names on Windows.

    Asks ``SHGetKnownFolderPath(<FOLDERID>, 0, NULL)`` like .NET's ``Environment.Win32.cs``;
    flags 0 makes the shell verify the folder exists. The call runs on the caller's thread with
    no COM initialisation of its own: measured on Windows, it answers from a thread that has
    no apartment at all, and pwshpy never initialises or uninitialises COM on a caller's thread.

    A "not found" HRESULT (``0x80070002`` / ``0x80070003``) means the folder is missing. For the
    three application-data members pwsh then answers the environment variable .NET falls back to
    (``LOCALAPPDATA`` / ``APPDATA`` / ``ProgramData``); this resolver does the same only when the
    variable is set and names an existing directory, and answers ``None`` otherwise. Desktop,
    MyDocuments and Programs have no fallback: ``None``.

    Args:
        folder: The known folder to resolve.
        environ: The environment to read the fallback variable from (names compared without
            regard to case, as Windows does); ``None`` reads :data:`os.environ` at call time.
        load: Module loader for ``win32com.shell.shell``; ``None`` imports the real pywin32
            module (a test passes a double).

    Returns:
        The folder path, or ``None`` when the folder is missing (pwsh answers ``""``).

    Raises:
        NativeCallError: ``SHGetKnownFolderPath`` failed with any other HRESULT (an access
            denied, ``0x80004005`` for a folder with no file-system path); the message carries
            the unsigned HRESULT and the member name.
        PlatformUnsupportedError: pywin32 is not installed (not Windows).

    Example:
        >>> sys.platform != "win32" or windows_known_folder(KnownFolder.PROGRAM_DATA) is not None
        True
    """
    # Resolved per call, not as a default: a default is bound when the def runs.
    loader = load if load is not None else _load
    shell = loader("win32com.shell.shell")
    try:
        found: str = shell.SHGetKnownFolderPath(_FOLDER_IDS[folder], 0, None)
    except Exception as exc:  # a pywin32 COM error; anything else is not a shell answer
        if not is_com_error(exc):
            raise
        code = com_hresult(exc)
        if code not in _NOT_FOUND:
            shown = "without an HRESULT" if code is None else f"with HRESULT 0x{code:08X}"
            raise native_error(
                f"SHGetKnownFolderPath for KnownFolder.{folder.name} ({folder.value}) failed {shown}", exc
            ) from exc
        return _env_fallback(folder, os.environ if environ is None else environ)
    return Path(found)


def _env_fallback(folder: KnownFolder, env: Mapping[str, str]) -> Path | None:
    """Return the existing directory .NET's fallback variable names for ``folder``, else ``None``."""
    name = _ENV_FALLBACKS.get(folder)
    if name is None:
        return None
    value = _env_lookup(env, name)
    if not value:  # Path("") is ".", the current directory: an empty value names nothing
        return None
    path = Path(value)
    try:
        return path if path.is_dir() else None
    except OSError:  # before Python 3.14 is_dir() re-raises EACCES: unreadable counts as missing
        return None


def _env_lookup(env: Mapping[str, str], name: str) -> str | None:
    """Return ``env[name]`` with the name compared without regard to case, as Windows does.

    ``os.environ`` on Windows already folds case; a plain mapping a caller passes does not.
    """
    value = env.get(name)
    if value is not None:
        return value
    wanted = name.upper()
    return next((v for k, v in env.items() if k.upper() == wanted), None)


def _load(module: str) -> Any:
    """Import a pywin32 module lazily, so importing this module stays safe on every OS."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            f"the Windows known folders need pywin32 (Windows only); {module} is unavailable."
        ) from exc
