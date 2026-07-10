"""Native ACLs on POSIX via the kernel ACL xattr and stat/chown (stdlib, Linux-focused).

The portable counterpart to the win32security ACL adapter. It reads and writes the POSIX access
ACL directly through the kernel's binary ``system.posix_acl_access`` extended attribute
(``os.getxattr`` / ``os.setxattr``) and the file mode bits (``os.stat``) - the same structures
``getfacl`` / ``setfacl`` use, so no subprocess and no text scraping. Owner changes go through
``os.chown``.

The model mismatch with Windows DACLs is real and handled honestly (see :class:`AclEntry`):
POSIX ACLs are allow-only (so ``access_type`` is always ``ALLOW``), principals are uid/gid, and the
entry categories (owner / named user / owning group / named group / mask / other) are carried in
``kind``. A file with no extended ACL yields its three base entries synthesized from the mode bits.

Contents:
    * :func:`perm_to_rwx` - a 0-7 POSIX perm to its ``rwx`` string (pure).
    * :func:`iter_acl` - stream a path's ACL entries as records.
    * :class:`PosixAclController` - set_owner / add_ace / remove_ace (**mutating**).
"""

from __future__ import annotations

import importlib
import os
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

from ...domain.enums import AceType, AclEntryKind
from ...domain.errors import NativeCallError
from ...domain.records import AclEntry

_os: Any = os  # funnel the Unix-only os calls (getxattr/setxattr/chown) past the type checker's platform pruning

_XATTR_ACCESS = "system.posix_acl_access"
_ACL_VERSION = 2  # POSIX_ACL_XATTR_VERSION
_HEADER = struct.Struct("<I")  # a_version
_ENTRY = struct.Struct("<HHI")  # e_tag, e_perm, e_id

_TAG_USER_OBJ = 0x01
_TAG_USER = 0x02
_TAG_GROUP_OBJ = 0x04
_TAG_GROUP = 0x08
_TAG_MASK = 0x10
_TAG_OTHER = 0x20
_UNDEFINED_ID = 0xFFFFFFFF

#: Canonical serialization order of the ACL tags (the kernel validates a well-formed ACL).
_ORDER = {_TAG_USER_OBJ: 0, _TAG_USER: 1, _TAG_GROUP_OBJ: 2, _TAG_GROUP: 3, _TAG_MASK: 4, _TAG_OTHER: 5}
#: ACL tag -> (entry kind, whether the entry names a specific uid/gid).
_KINDS = {
    _TAG_USER_OBJ: AclEntryKind.OWNER,
    _TAG_USER: AclEntryKind.USER,
    _TAG_GROUP_OBJ: AclEntryKind.OWNING_GROUP,
    _TAG_GROUP: AclEntryKind.GROUP,
    _TAG_MASK: AclEntryKind.MASK,
    _TAG_OTHER: AclEntryKind.OTHER,
}


class _XattrAclEntry(NamedTuple):
    """One kernel ACL entry: its tag, its rwx perm bits, and the uid/gid it names.

    ``entry_id`` is ``_UNDEFINED_ID`` for the base (owner/owning-group/other) and mask entries.
    """

    tag: int
    perm: int
    entry_id: int


def perm_to_rwx(perm: int) -> str:
    """Render a POSIX permission (0-7) as its ``rwx`` string.

    Example:
        >>> perm_to_rwx(5), perm_to_rwx(6), perm_to_rwx(0)
        ('r-x', 'rw-', '---')
    """
    return ("r" if perm & 4 else "-") + ("w" if perm & 2 else "-") + ("x" if perm & 1 else "-")


def _load(module_name: str) -> Any:
    """Import a POSIX-only stdlib module (``pwd`` / ``grp``) lazily; import stays safe on Windows."""
    return importlib.import_module(module_name)


def _user_name(uid: int) -> str:
    try:
        return str(_load("pwd").getpwuid(uid).pw_name)
    except (KeyError, OSError):
        return ""


def _group_name(gid: int) -> str:
    try:
        return str(_load("grp").getgrgid(gid).gr_name)
    except (KeyError, OSError):
        return ""


def _base_entries(mode: int) -> list[_XattrAclEntry]:
    """The three base ACL entries a file always has, synthesized from its mode bits."""
    return [
        _XattrAclEntry(_TAG_USER_OBJ, (mode >> 6) & 7, _UNDEFINED_ID),
        _XattrAclEntry(_TAG_GROUP_OBJ, (mode >> 3) & 7, _UNDEFINED_ID),
        _XattrAclEntry(_TAG_OTHER, mode & 7, _UNDEFINED_ID),
    ]


def _read_raw(path: str) -> list[_XattrAclEntry] | None:
    """The extended access ACL as entries, or ``None`` when the file has none (mode bits only)."""
    try:
        blob: bytes = _os.getxattr(path, _XATTR_ACCESS)
    except OSError:
        return None  # ENODATA (no extended ACL) or ENOTSUP (fs/OS without POSIX ACLs)
    entries: list[_XattrAclEntry] = []
    offset = _HEADER.size
    while offset < len(blob):
        tag, perm, eid = _ENTRY.unpack_from(blob, offset)
        entries.append(_XattrAclEntry(int(tag), int(perm), int(eid)))
        offset += _ENTRY.size
    return entries


def _current_acl(path: str) -> list[_XattrAclEntry]:
    """The path's ACL as entries - the extended ACL if present, else the base entries from its mode."""
    entries = _read_raw(path)
    if entries is not None:
        return entries
    try:
        return _base_entries(Path(path).stat().st_mode)
    except OSError as exc:
        raise NativeCallError(f"cannot stat {path!r}: {exc}") from exc


def _to_entry(path: str, info: os.stat_result, entry: _XattrAclEntry) -> AclEntry:
    """Marshal one ACL entry into an :class:`AclEntry`, resolving the principal uid/gid + name."""
    if entry.tag == _TAG_USER_OBJ:
        trustee_sid, trustee_name = str(info.st_uid), _user_name(info.st_uid)
    elif entry.tag == _TAG_USER:
        trustee_sid, trustee_name = str(entry.entry_id), _user_name(entry.entry_id)
    elif entry.tag == _TAG_GROUP_OBJ:
        trustee_sid, trustee_name = str(info.st_gid), _group_name(info.st_gid)
    elif entry.tag == _TAG_GROUP:
        trustee_sid, trustee_name = str(entry.entry_id), _group_name(entry.entry_id)
    else:  # mask / other - no principal
        trustee_sid, trustee_name = "", ("mask" if entry.tag == _TAG_MASK else "other")
    return AclEntry.model_construct(
        path=path,
        owner_sid=str(info.st_uid),
        trustee_sid=trustee_sid,
        access_type=AceType.ALLOW,  # POSIX ACLs are allow-only
        rights=entry.perm,
        trustee_name=trustee_name,
        inherited=False,
        kind=_KINDS[entry.tag],
        permissions=perm_to_rwx(entry.perm),
    )


def iter_acl(path: str) -> Iterator[AclEntry]:
    """Stream a path's ACL entries as :class:`AclEntry` records (like ``Get-Acl``, POSIX).

    Yields the owner, any named users/groups, the owning group, the mask and other. A file with no
    extended ACL yields just its three base entries (owner / owning group / other) from the mode.

    Example:
        >>> callable(iter_acl)
        True
    """
    try:
        info = Path(path).stat()
    except OSError as exc:
        raise NativeCallError(f"cannot stat {path!r}: {exc}") from exc
    for entry in _current_acl(path):
        yield _to_entry(path, info, entry)


def _resolve_uid(owner: str) -> int:
    if owner.isdigit():
        return int(owner)
    try:
        return int(_load("pwd").getpwnam(owner).pw_uid)
    except (KeyError, OSError) as exc:
        raise NativeCallError(f"no such user {owner!r}") from exc


def _resolve_gid(group: str) -> int:
    if group.isdigit():
        return int(group)
    try:
        return int(_load("grp").getgrnam(group).gr_gid)
    except (KeyError, OSError) as exc:
        raise NativeCallError(f"no such group {group!r}") from exc


def _parse_trustee(trustee: str) -> tuple[int, int]:
    """A ``user:NAME`` / ``group:NAME`` (or bare -> user) trustee -> (named tag, resolved id)."""
    if trustee.startswith("group:"):
        return _TAG_GROUP, _resolve_gid(trustee[len("group:") :])
    name = trustee[len("user:") :] if trustee.startswith("user:") else trustee
    return _TAG_USER, _resolve_uid(name)


def _with_recomputed_mask(entries: list[_XattrAclEntry]) -> list[_XattrAclEntry]:
    """Re-derive the ACL mask: the union of every group-class entry's perms, dropped if no named entries."""
    without_mask = [e for e in entries if e.tag != _TAG_MASK]
    named = [e for e in without_mask if e.tag in (_TAG_USER, _TAG_GROUP)]
    if not named:
        return without_mask  # a minimal ACL (base entries only) carries no mask
    group_class = [e for e in without_mask if e.tag in (_TAG_USER, _TAG_GROUP, _TAG_GROUP_OBJ)]
    mask_perm = 0
    for entry in group_class:
        mask_perm |= entry.perm
    return [*without_mask, _XattrAclEntry(_TAG_MASK, mask_perm, _UNDEFINED_ID)]


def _write_acl(path: str, entries: list[_XattrAclEntry]) -> None:
    """Serialize the ACL in canonical order and write it back through ``setxattr``."""
    ordered = sorted(entries, key=lambda e: (_ORDER[e.tag], e.entry_id))
    body = b"".join(_ENTRY.pack(e.tag, e.perm, e.entry_id & 0xFFFFFFFF) for e in ordered)
    try:
        _os.setxattr(path, _XATTR_ACCESS, _HEADER.pack(_ACL_VERSION) + body)
    except OSError as exc:
        raise NativeCallError(f"cannot set ACL on {path!r}: {exc}") from exc


class PosixAclController:
    """Mutating POSIX ACL control (set owner, add/remove a named user/group entry), **mutating**.

    The counterpart to the win32 NativeAclController. ``trustee`` is ``user:NAME`` / ``group:NAME``
    (a bare name is a user); ``rights`` uses the low three bits as POSIX ``rwx``. POSIX ACLs are
    allow-only, so a deny ACE is rejected.

    Example:
        >>> callable(PosixAclController().add_ace)
        True
    """

    def set_owner(self, path: str, owner: str) -> None:
        """Set the owning user of ``path`` (like ``chown``); needs privilege to change to another user."""
        uid = _resolve_uid(owner)
        try:
            _os.chown(path, uid, -1)  # -1 leaves the owning group unchanged
        except OSError as exc:
            raise NativeCallError(f"cannot set owner of {path!r}: {exc}") from exc

    def add_ace(self, path: str, trustee: str, rights: int, *, access_type: AceType = AceType.ALLOW) -> None:
        """Grant a named user/group ``rights`` (low 3 bits as rwx) on ``path``; the mask is recomputed."""
        if access_type is AceType.DENY:
            raise NativeCallError(
                "POSIX ACLs are allow-only; there is no deny entry (grant fewer permissions instead)."
            )
        tag, ident = _parse_trustee(trustee)
        perm = rights & 7
        entries = [e for e in _current_acl(path) if not (e.tag == tag and e.entry_id == ident)]
        entries.append(_XattrAclEntry(tag, perm, ident))
        _write_acl(path, _with_recomputed_mask(entries))

    def remove_ace(self, path: str, trustee: str, *, access_type: AceType | None = None) -> None:
        """Remove a named user/group entry from ``path``; the mask is recomputed (dropped if none remain)."""
        if access_type is AceType.DENY:
            return  # POSIX has no deny entries to remove
        tag, ident = _parse_trustee(trustee)
        entries = [e for e in _current_acl(path) if not (e.tag == tag and e.entry_id == ident)]
        _write_acl(path, _with_recomputed_mask(entries))


__all__ = ["PosixAclController", "iter_acl", "perm_to_rwx"]
