"""native ACL CONTROL over ``win32security`` (Windows-only, **mutating**).

Set-Acl building blocks: add an access-control entry, remove the ACEs of a
trustee, and set the owner of a filesystem path - the mutating counterpart of the
read-only :mod:`~pwshpy.adapters.native.acl` source.  Trustees are given as a SID
string (``S-1-...``) or a resolvable name; SID is the canonical identity (see
``docs/locale-and-identity.md``).  Rights are a raw Windows access mask (int).
``pywin32`` is imported lazily; every native call is wrapped in
:class:`~pwshpy.domain.errors.NativeCallError`.

Note: ACEs are appended in call order - this low-level surface does not enforce
the "deny before allow" canonical ordering, exactly like a raw
``SetSecurityDescriptorDacl``.  Setting an owner to another principal needs
``SeRestorePrivilege``.

**MUTATING** - see CLAUDE.md "Development Safety": real tests scope to a temp file.

Contents:
    * :class:`NativeAclController` - add_ace / remove_ace / set_owner.
"""

from __future__ import annotations

import importlib
from typing import Any

from ...domain.enums import AceType
from ...domain.errors import NativeCallError, PlatformUnsupportedError
from .marshal import to_ace_type


def _load() -> Any:
    """Import ``win32security`` lazily (absent on a portable install)."""
    try:
        return importlib.import_module("win32security")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "ACL control requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc


def _resolve_sid(security: Any, principal: str) -> Any:
    """Resolve a principal (a ``S-1-...`` SID string or a name) to a native SID object."""
    if principal.startswith("S-1-"):
        return security.ConvertStringSidToSid(principal)
    sid, _domain, _type = security.LookupAccountName(None, principal)
    return sid


class NativeAclController:
    """Mutating ACL control over ``win32security`` (add/remove ACE, set owner)."""

    def add_ace(self, path: str, trustee: str, rights: int, *, access_type: AceType = AceType.ALLOW) -> None:
        """Append an allow/deny ACE granting ``rights`` to ``trustee`` on ``path``."""
        security = _load()
        try:
            sid = _resolve_sid(security, trustee)
            descriptor = security.GetFileSecurity(path, security.DACL_SECURITY_INFORMATION)
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None:
                dacl = security.ACL()
            revision = security.ACL_REVISION
            if access_type is AceType.DENY:
                dacl.AddAccessDeniedAce(revision, rights, sid)
            else:
                dacl.AddAccessAllowedAce(revision, rights, sid)
            descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
            security.SetFileSecurity(path, security.DACL_SECURITY_INFORMATION, descriptor)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot add ACE to {path!r}") from exc

    def remove_ace(self, path: str, trustee: str, *, access_type: AceType | None = None) -> None:
        """Remove the ACEs of ``trustee`` on ``path`` (optionally only of ``access_type``)."""
        security = _load()
        try:
            target = str(security.ConvertSidToStringSid(_resolve_sid(security, trustee)))
            descriptor = security.GetFileSecurity(path, security.DACL_SECURITY_INFORMATION)
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None:
                return
            for index in range(dacl.GetAceCount() - 1, -1, -1):
                (ace_type, _flags), _mask, ace_sid = dacl.GetAce(index)
                if str(security.ConvertSidToStringSid(ace_sid)) != target:
                    continue
                if access_type is None or to_ace_type(int(ace_type)) is access_type:
                    dacl.DeleteAce(index)
            descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
            security.SetFileSecurity(path, security.DACL_SECURITY_INFORMATION, descriptor)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot remove ACE from {path!r}") from exc

    def set_owner(self, path: str, owner: str) -> None:
        """Set the owner of ``path`` to ``owner`` (needs SeRestorePrivilege for other principals)."""
        security = _load()
        try:
            sid = _resolve_sid(security, owner)
            descriptor = security.GetFileSecurity(path, security.OWNER_SECURITY_INFORMATION)
            descriptor.SetSecurityDescriptorOwner(sid, 0)
            security.SetFileSecurity(path, security.OWNER_SECURITY_INFORMATION, descriptor)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot set owner of {path!r}") from exc


__all__ = ["NativeAclController"]
