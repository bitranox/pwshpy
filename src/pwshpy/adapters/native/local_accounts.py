"""native local-accounts source over win32net / win32security (Windows-only).

Enumerates local users (``Get-LocalUser``) and local groups (``Get-LocalGroup``)
through the SAM API (``win32net``), resolving each principal's SID with
``win32security``.  Principals are identified by SID (stable, locale-independent);
the SAM name is localized for built-in accounts (see ``docs/locale-and-identity.md``).
``pywin32`` is imported lazily so a portable install stays clean and raises
:class:`~pwshpy.domain.errors.PlatformUnsupportedError`.

Contents:
    * :func:`iter_local_users` - stream the local user accounts.
    * :func:`iter_local_groups` - stream the local groups.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any, NamedTuple

from ...domain.errors import PlatformUnsupportedError
from ...domain.records import LocalGroup, LocalUser
from .marshal import native_error

_UF_ACCOUNTDISABLE = 0x0002
_FILTER_NORMAL_ACCOUNT = 0x0002  # win32netcon.FILTER_NORMAL_ACCOUNT
_MAX_PREFERRED_LENGTH = -1  # MAX_PREFERRED_LENGTH ((DWORD)-1): let the API size each buffer to return all rows


class AccountModules(NamedTuple):
    """The two pywin32 modules the local-account source and controller work through.

    Named slots because both are ``Any``: a positional swap would type-check and fail at runtime.
    """

    #: ``win32net``: the SAM API (users, groups, membership).
    net: Any
    #: ``win32security``: name to SID resolution.
    security: Any


def load_account_modules(subsystem: str) -> AccountModules:
    """Import ``win32net`` + ``win32security`` lazily (absent on a portable install).

    Shared by this source and :mod:`~pwshpy.adapters.native.local_account_control`, which differ
    only in how the refusal names them.

    Args:
        subsystem: Opens the refusal sentence (``"<subsystem> requires pywin32 ..."``).

    Returns:
        The two modules, by name.

    Raises:
        PlatformUnsupportedError: pywin32 is not installed (a portable, non-Windows install).
    """
    try:
        net: Any = importlib.import_module("win32net")
        security: Any = importlib.import_module("win32security")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            f"{subsystem} requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc
    return AccountModules(net=net, security=security)


def _load() -> AccountModules:
    """This source's pywin32 modules; the seam a test replaces with doubles."""
    return load_account_modules("The local-accounts subsystem")


def sid_for(security: Any, name: str) -> str:
    """Resolve a principal name to its string SID, or ``""`` if it cannot be looked up."""
    try:
        sid, _domain, _type = security.LookupAccountName(None, name)
        return str(security.ConvertSidToStringSid(sid))
    except Exception:
        return ""


def iter_local_users() -> Iterator[LocalUser]:
    """Stream the local Windows user accounts as :class:`LocalUser` records (like ``Get-LocalUser``).

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import LocalUser
        >>> sys.platform != "win32" or isinstance(next(iter_local_users()), LocalUser)
        True
    """
    modules = _load()
    resume = 0
    while True:
        try:
            data, _total, resume = modules.net.NetUserEnum(
                None, 2, _FILTER_NORMAL_ACCOUNT, resume, _MAX_PREFERRED_LENGTH
            )
        except Exception as exc:
            raise native_error(str(exc) or "cannot enumerate local users", exc) from exc
        for entry in data:
            name = str(entry["name"])
            yield LocalUser.model_construct(
                name=name,
                sid=sid_for(modules.security, name),
                enabled=not (int(entry["flags"]) & _UF_ACCOUNTDISABLE),
                full_name=str(entry.get("full_name") or ""),
                description=str(entry.get("comment") or ""),
            )
        if not resume:
            return


def iter_local_groups() -> Iterator[LocalGroup]:
    """Stream the local Windows groups as :class:`LocalGroup` records (like ``Get-LocalGroup``).

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import LocalGroup
        >>> sys.platform != "win32" or isinstance(next(iter_local_groups()), LocalGroup)
        True
    """
    modules = _load()
    resume = 0
    while True:
        try:
            data, _total, resume = modules.net.NetLocalGroupEnum(None, 1, resume, _MAX_PREFERRED_LENGTH)
        except Exception as exc:
            raise native_error(str(exc) or "cannot enumerate local groups", exc) from exc
        for entry in data:
            name = str(entry["name"])
            yield LocalGroup.model_construct(
                name=name, sid=sid_for(modules.security, name), description=str(entry.get("comment") or "")
            )
        if not resume:
            return


__all__ = ["iter_local_groups", "iter_local_users"]
