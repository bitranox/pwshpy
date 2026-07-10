"""native ACL source over win32security (Windows-only).

Reads a filesystem path's owner + DACL via ``GetFileSecurity``, mirroring
``Get-Acl``, and yields one :class:`~pwshpy.domain.records.AclEntry` per
access-control entry.  Principals are identified by SID (locale-independent);
``trustee_name`` is the localized readable name (see ``docs/locale-and-identity.md``).
``pywin32`` is imported lazily so a portable install stays clean and raises
:class:`~pwshpy.domain.errors.PlatformUnsupportedError`.

Contents:
    * :func:`iter_acl` - stream the DACL entries of a filesystem path.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

from ...domain.enums import AceType, WellKnownSid
from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import AclEntry
from .marshal import to_ace_type

_INHERITED_ACE = 0x10
_GENERIC_ALL = 0x10000000


def _load() -> Any:
    """Import ``win32security`` lazily (absent on a portable install)."""
    try:
        return importlib.import_module("win32security")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "The ACL subsystem requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc


def _name_for(security: Any, sid: Any) -> str:
    """Resolve a SID to a ``DOMAIN\\name`` readable string, or ``""`` if unresolvable."""
    try:
        name, domain, _type = security.LookupAccountSid(None, sid)
        return f"{domain}\\{name}" if domain else str(name)
    except Exception:
        return ""


def iter_acl(path: str) -> Iterator[AclEntry]:
    """Stream the DACL entries of a filesystem path as :class:`AclEntry` records (like ``Get-Acl``).

    A NULL DACL (the object is unprotected: everyone has full access) yields a
    single synthetic Everyone/Allow entry, so it is never confused with a present
    but EMPTY DACL (nobody has access), which correctly yields no entries.

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import AclEntry
        >>> sys.platform != "win32" or isinstance(next(iter_acl("C:\\\\Windows")), AclEntry)
        True
    """
    security: Any = _load()
    flags = security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION
    try:
        descriptor = security.GetFileSecurity(path, flags)
        owner = descriptor.GetSecurityDescriptorOwner()
        owner_sid = str(security.ConvertSidToStringSid(owner)) if owner is not None else ""
        dacl = descriptor.GetSecurityDescriptorDacl()
    except Exception as exc:
        raise NativeCallError(str(exc) or f"cannot read ACL of {path!r}") from exc
    if dacl is None:  # null DACL: unprotected object -> everyone has full access (distinct from empty DACL)
        yield AclEntry.model_construct(
            path=path,
            owner_sid=owner_sid,
            trustee_sid=WellKnownSid.EVERYONE.value,
            access_type=AceType.ALLOW,
            rights=_GENERIC_ALL,
            trustee_name="Everyone (implicit: null DACL)",
            inherited=False,
        )
        return
    for index in range(dacl.GetAceCount()):
        (ace_type, ace_flags), mask, sid = dacl.GetAce(index)
        yield AclEntry.model_construct(
            path=path,
            owner_sid=owner_sid,
            trustee_sid=str(security.ConvertSidToStringSid(sid)),
            access_type=to_ace_type(int(ace_type)),
            rights=int(mask),
            trustee_name=_name_for(security, sid),
            inherited=bool(int(ace_flags) & _INHERITED_ACE),
        )


__all__ = ["iter_acl"]
