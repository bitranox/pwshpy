"""What ``new_shortcut`` writes, validated once before any COM call (pure domain).

:class:`ShortcutSpec` bundles the inputs of a Windows shortcut (``.lnk``) so the adapter takes
one value object instead of eight parameters.  It is a frozen dataclass rather than a Pydantic
model so that every refusal surfaces as :class:`~pwshpy.domain.errors.ConfigurationError`, never
wrapped in a Pydantic ``ValidationError`` that the facade and CLI would not map.

The checks run in the domain because past them the shell link object fails silently: it accepts
an over-long value and saves a wrapped, truncated or renamed shortcut while reporting success.
The domain touches no filesystem, so a relative ``.lnk`` path is refused here rather than
resolved; the caller makes it absolute first.

Contents:
    * :class:`ShortcutSpec` - the validated inputs of one shortcut.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - list2cmdline only (pure argv quoting); nothing here starts a process
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import cast

from .errors import ConfigurationError

# MAX_PATH (260) minus the terminating NUL: IShellLink and IPersistFile::Save cut or wrap a
# longer target or .lnk path while returning S_OK (measured with LongPathsEnabled=0).
_MAX_PATH = 259
# Description, working directory and icon location keep 260 characters and silently drop more.
_MAX_FIELD = 260
# The joined argument string; IShellLinkW::SetArguments stores at most this many characters.
_MAX_ARGUMENTS = 32767
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
# A line break or NUL cannot round-trip through a .lnk field, and a line break in the argument
# string would split the command line the target sees.
_FORBIDDEN_CHARACTERS = (("\x00", "NUL"), ("\r", "CR"), ("\n", "LF"))
# A leading %NAME% reference followed by a backslash: Windows expands it when the shortcut is
# read, so %SystemRoot%\notepad.exe is as definite as the absolute path it names.  An
# environment variable name cannot hold '=' and never holds a path separator.
_ENVIRONMENT_PREFIX = re.compile(r"%[^%=\\/]+%\\")


def _text(name: str, value: object) -> str:
    """Return ``value`` when it is a str free of NUL, CR and LF.

    Raises:
        ConfigurationError: ``value`` is not a str, or holds NUL, CR or LF.

    Example:
        >>> _text("description", "Run the tool")
        'Run the tool'
    """
    if not isinstance(value, str):
        raise ConfigurationError(f"{name} must be a str, not {type(value).__name__}")
    for character, label in _FORBIDDEN_CHARACTERS:
        if character in value:
            raise ConfigurationError(f"{name} contains a {label} character, which a shortcut cannot store")
    return value


def _bounded(name: str, value: str, limit: int) -> str:
    r"""Return ``value`` when it has at most ``limit`` characters.

    The refusal names the lengths only: the value itself may be thousands of characters.

    Raises:
        ConfigurationError: ``value`` is longer than ``limit``.

    Example:
        >>> _bounded("icon", "C:\\x.ico", 260)
        'C:\\x.ico'
    """
    if len(value) > limit:
        raise ConfigurationError(f"{name} is {len(value)} characters; the limit is {limit}")
    return value


def _lnk_path(value: object) -> str:
    r"""Return ``value`` when it is an absolute Windows path to a ``.lnk`` file within MAX_PATH.

    Raises:
        ConfigurationError: Not a str, NUL/CR/LF, relative (``Tool.lnk``, ``C:Tool.lnk``,
            ``\Tool.lnk``), not ending in a ``.lnk`` file name, or longer than 259 characters.

    Example:
        >>> _lnk_path("C:\\Users\\me\\Tool.LNK")
        'C:\\Users\\me\\Tool.LNK'
    """
    text = _text("path", value)
    windows_path = PureWindowsPath(text)
    if not windows_path.is_absolute():
        raise ConfigurationError("path must be an absolute Windows path (a drive or UNC path)")
    # Both checks: the raw text must END in .lnk (a trailing separator, dot or space would be
    # normalised away or stripped by Windows), and the file name must have a stem (".lnk" alone
    # is a name without an extension).
    if not text.lower().endswith(".lnk") or windows_path.suffix.lower() != ".lnk":
        raise ConfigurationError("path must name a file with the .lnk extension")
    return _bounded("path", text, _MAX_PATH)


def _target(value: object) -> str:
    r"""Return ``value`` when it is an absolute Windows path or starts with ``%NAME%\``.

    Only the raw text is bounded here: what an environment reference expands to depends on the
    host, so the 259-character limit on the expansion is left to Windows (see
    :attr:`ShortcutSpec.target`).

    Raises:
        ConfigurationError: Not a str, NUL/CR/LF, relative, or longer than 259 characters.

    Example:
        >>> _target("%SystemRoot%\\notepad.exe")
        '%SystemRoot%\\notepad.exe'
    """
    text = _text("target", value)
    if not (PureWindowsPath(text).is_absolute() or _ENVIRONMENT_PREFIX.match(text)):
        raise ConfigurationError(
            "target must be an absolute Windows path or start with an environment reference such as %SystemRoot%\\"
        )
    return _bounded("target", text, _MAX_PATH)


def _arguments(value: object) -> tuple[str, ...]:
    """Return ``value`` as a tuple when it is a sequence of clean strings that joins within bounds.

    A bare str is refused, never split: splitting it would guess at quoting the caller meant.

    Raises:
        ConfigurationError: A str, a non-sequence (an iterator would be consumed by one look), an
            element that is not a str or holds NUL/CR/LF, or a joined line over 32767 characters.

    Example:
        >>> _arguments(["--name", "a b"])
        ('--name', 'a b')
    """
    if isinstance(value, str):
        raise ConfigurationError("arguments must be a sequence of strings, not a single string; it is never split")
    if not isinstance(value, Sequence):
        raise ConfigurationError(f"arguments must be a sequence of strings, not {type(value).__name__}")
    items = cast("Sequence[object]", value)
    argv = tuple(_text(f"argument {position}", item) for position, item in enumerate(items, start=1))
    joined = subprocess.list2cmdline(argv)
    if len(joined) > _MAX_ARGUMENTS:
        raise ConfigurationError(
            f"arguments join to a command line of {len(joined)} characters; the limit is {_MAX_ARGUMENTS}"
        )
    return argv


def _optional(name: str, value: object) -> str | None:
    """Return ``value`` bounded at 260 characters, or None when it is None or ``""``.

    Raises:
        ConfigurationError: Not a str or None, NUL/CR/LF, or longer than 260 characters.

    Example:
        >>> _optional("description", ""), _optional("description", "Run it")
        (None, 'Run it')
    """
    if value is None or value == "":
        return None
    return _bounded(name, _text(name, value), _MAX_FIELD)


def _flag(name: str, value: object) -> bool:
    """Return ``value`` when it is a bool.

    An int or a str is refused rather than read for truth: ``"no"`` is truthy, and a shortcut
    that starts its target elevated is not one to build from a guess.

    Raises:
        ConfigurationError: ``value`` is not a bool.

    Example:
        >>> _flag("run_as_administrator", True)
        True
    """
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a bool, not {type(value).__name__}")
    return value


def _icon_index(value: object) -> int:
    """Return ``value`` when it is an int (not a bool) in the signed 32-bit range.

    The refusal never echoes the number: ``str()`` of a huge int raises past 4300 digits.

    Raises:
        ConfigurationError: A bool, a non-int, or an int outside -2**31 .. 2**31-1.

    Example:
        >>> _icon_index(-3)
        -3
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"icon_index must be an int, not {type(value).__name__}")
    if not _INT32_MIN <= value <= _INT32_MAX:
        raise ConfigurationError(f"icon_index is outside the signed 32-bit range {_INT32_MIN} .. {_INT32_MAX}")
    return int(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class ShortcutSpec:
    r"""The validated inputs of one Windows shortcut (``.lnk``).

    Built with keyword arguments; the constructor checks every field and raises
    :class:`~pwshpy.domain.errors.ConfigurationError` on the first refusal, so a spec that exists
    is one the shell link object stores exactly.  Whether the file already exists is not the
    spec's concern: that needs the filesystem and belongs to the adapter.

    Unset optional text is None, and ``""`` is stored as None too: the shell link object reads
    an unset field back as ``""`` either way, so the two cannot be told apart once written.  Read
    back as a :class:`~pwshpy.domain.records.ShortcutInfo`, an unset field is ``""``, so a round
    trip compares ``spec.description or ""`` with ``info.description``.

    Attributes:
        path: The ``.lnk`` file: an absolute Windows path (drive or UNC) ending in ``.lnk``
            (any case), at most 259 characters.  A relative path is refused, not resolved.
        target: What the shortcut runs or opens: an absolute Windows path, or one starting with
            an environment reference and a backslash (``%SystemRoot%\notepad.exe``); at most
            259 characters.  The expansion of such a reference must fit in 259 characters too:
            a longer one is refused by Windows when the shortcut is written (``SetPath`` fails
            with E_FAIL 0x80004005, raised as ``NativeCallError``; no file is written; measured
            with LongPathsEnabled=0).  The spec cannot check that, because the expansion depends
            on the host's environment.
        arguments: The argv passed to ``target``, stored as a tuple.  Any sequence of str is
            accepted; a bare str is refused, never split.  Joined with
            :func:`subprocess.list2cmdline` (see :attr:`joined_arguments`) it is at most 32767
            characters.
        working_directory: The directory ``target`` starts in, or None; at most 260 characters.
        icon: The file the icon comes from, or None; at most 260 characters.
        icon_index: Which icon in ``icon``: any signed 32-bit int (negative selects a resource
            id); a bool is refused.
        description: The comment Explorer shows as the tooltip, or None; at most 260 characters.
        run_as_administrator: Start ``target`` elevated (the "Run as administrator" box in the
            shortcut's Advanced properties, the link flag ``SLDF_RUNAS_USER``); a bool only.

    No text field and no argument may contain NUL, CR or LF.

    Raises:
        ConfigurationError: Any field breaks the rules above.

    Example:
        >>> spec = ShortcutSpec(path="C:\\Users\\me\\Desktop\\Tool.lnk", target="%SystemRoot%\\notepad.exe",
        ...                     arguments=["C:\\notes\\a b.txt"], description="")
        >>> spec.arguments, spec.joined_arguments, spec.description
        (('C:\\notes\\a b.txt',), '"C:\\notes\\a b.txt"', None)
    """

    path: str
    target: str
    arguments: Sequence[str] = ()
    working_directory: str | None = None
    icon: str | None = None
    icon_index: int = 0
    description: str | None = None
    run_as_administrator: bool = False

    def __post_init__(self) -> None:
        """Validate and normalise every field (frozen, hence ``object.__setattr__``)."""
        checked: dict[str, object] = {
            "path": _lnk_path(self.path),
            "target": _target(self.target),
            "arguments": _arguments(self.arguments),
            "working_directory": _optional("working_directory", self.working_directory),
            "icon": _optional("icon", self.icon),
            "icon_index": _icon_index(self.icon_index),
            "description": _optional("description", self.description),
            "run_as_administrator": _flag("run_as_administrator", self.run_as_administrator),
        }
        for name, value in checked.items():
            object.__setattr__(self, name, value)

    @property
    def joined_arguments(self) -> str:
        """The arguments as the one command-line string a shortcut stores.

        Joined by :func:`subprocess.list2cmdline`, the quoting the Microsoft C runtime parses
        back into the same argv.

        Example:
            >>> ShortcutSpec(path="C:\\\\a.lnk", target="C:\\\\t.exe", arguments=["x", "a b"]).joined_arguments
            'x "a b"'
        """
        return subprocess.list2cmdline(self.arguments)


__all__ = ["ShortcutSpec"]
