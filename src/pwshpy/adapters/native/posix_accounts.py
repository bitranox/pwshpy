"""Native local accounts on POSIX (Linux/macOS) via the stdlib ``pwd`` / ``grp``.

The portable counterpart to the win32net local-accounts adapter: reads the local user
and group databases with zero dependencies.  Identity is the **uid/gid** - the POSIX
analogue of a Windows SID, stable and locale-independent - carried in the record's
``sid`` field (a plain number like ``"0"``, never a localized name).

``pwd`` / ``grp`` are Unix-only, so they are loaded lazily through ``importlib`` and
funnelled through an ``Any`` alias (the same seam the registry adapter uses for its
Windows-only backend): importing this module is safe on Windows, and the one place the
type checker sees the untyped POSIX surface is confined here. Calling it off POSIX
raises :class:`PlatformUnsupportedError`.

Contents:
    * :func:`iter_local_users` - one :class:`LocalUser` per passwd entry.
    * :func:`iter_local_groups` - one :class:`LocalGroup` per group entry.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import LocalGroup, LocalUser

#: Login shells that mark a non-interactive / system account. Without /etc/shadow (root),
#: this is the best-effort "enabled" signal - a nologin shell means the account cannot log in.
_NOLOGIN_SHELLS = frozenset({"/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false", "/dev/null", ""})


def _load(module_name: str) -> Any:
    """Import a POSIX-only stdlib module (``pwd``/``grp``); raise a clear error off POSIX."""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - only if called off POSIX (Windows)
        raise PlatformUnsupportedError("local accounts need Windows (win32net) or POSIX (pwd/grp).") from exc


def iter_local_users() -> Iterator[LocalUser]:
    """Yield a :class:`LocalUser` per local account from ``pwd`` (like Get-LocalUser, POSIX).

    ``enabled`` is inferred from the login shell (no nologin/false shell), since the real
    lock state lives in root-only ``/etc/shadow``; ``full_name`` is the first GECOS field.

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import LocalUser
        >>> sys.platform == "win32" or all(isinstance(u, LocalUser) for u in iter_local_users())
        True
    """
    pwd = _load("pwd")
    try:
        entries = pwd.getpwall()
    except OSError as exc:
        raise NativeCallError(f"cannot read the user database: {exc}") from exc
    for entry in entries:
        yield LocalUser(
            name=str(entry.pw_name),
            sid=str(entry.pw_uid),
            enabled=entry.pw_shell not in _NOLOGIN_SHELLS,
            full_name=str(entry.pw_gecos).split(",", 1)[0],
            description="",
        )


def iter_local_groups() -> Iterator[LocalGroup]:
    """Yield a :class:`LocalGroup` per local group from ``grp`` (like Get-LocalGroup, POSIX).

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import LocalGroup
        >>> sys.platform == "win32" or all(isinstance(g, LocalGroup) for g in iter_local_groups())
        True
    """
    grp = _load("grp")
    try:
        entries = grp.getgrall()
    except OSError as exc:
        raise NativeCallError(f"cannot read the group database: {exc}") from exc
    for entry in entries:
        yield LocalGroup(name=str(entry.gr_name), sid=str(entry.gr_gid), description="")


__all__ = ["iter_local_groups", "iter_local_users"]
