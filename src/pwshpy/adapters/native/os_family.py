"""The OS family a ``sys.platform`` string names, parsed once into a closed set.

Platform decisions read this family rather than comparing raw ``sys.platform`` strings at each
site: the string set is open (``linux``, ``freebsd14``, ``cygwin`` ...) and a site that spells
its own test (``== "darwin"``, ``startswith("linux")``) can drift from the next one.

Contents:
    * :class:`OsFamily` - Windows, Linux, macOS or anything else.
    * :func:`os_family_of` - the one parse step from a platform string to an :class:`OsFamily`.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["OsFamily", "os_family_of"]


class OsFamily(str, Enum):
    """The OS families pwshpy decides on; every other platform is :attr:`OTHER`.

    Attributes:
        WINDOWS: ``sys.platform == "win32"``.
        LINUX: ``sys.platform`` starting with ``linux``.
        MACOS: ``sys.platform == "darwin"``.
        OTHER: Any other platform (``freebsd14``, ``cygwin``, ...).
    """

    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    OTHER = "other"


def os_family_of(platform: OsFamily | str) -> OsFamily:
    """Return the :class:`OsFamily` a ``sys.platform`` string names; a member passes through unchanged.

    A plain string is read as a ``sys.platform`` value, never as a member value, so ``"macos"``
    is :attr:`OsFamily.OTHER` (no ``sys.platform`` spells it that way) while ``OsFamily.MACOS`` is
    itself.

    Args:
        platform: A ``sys.platform`` value, or an :class:`OsFamily` member (a test seam).

    Returns:
        The family it belongs to.

    Example:
        >>> [os_family_of(p).value for p in ("win32", "linux", "darwin", "freebsd14", "cygwin")]
        ['windows', 'linux', 'macos', 'other', 'other']
        >>> os_family_of(OsFamily.MACOS) is OsFamily.MACOS
        True
        >>> os_family_of("macos") is OsFamily.OTHER
        True
    """
    if isinstance(platform, OsFamily):
        return platform
    if platform == "win32":
        return OsFamily.WINDOWS
    if platform.startswith("linux"):
        return OsFamily.LINUX
    if platform == "darwin":
        return OsFamily.MACOS
    return OsFamily.OTHER
