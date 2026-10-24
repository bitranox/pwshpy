"""POSIX ACLs: pure perm rendering (os_agnostic) + a hermetic read/mutate round-trip (Linux).

The perm->rwx mapping is pure, so it runs everywhere. The live read and add/remove/set_owner
round-trip work on a private temp file (no host state touched), so they are os_linux - runnable in
CI's ubuntu lane - and skip cleanly on a filesystem without POSIX ACL support.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pwshpy.adapters.native import posix_acl
from pwshpy.adapters.native.posix_acl import PosixAclController, iter_acl, perm_to_rwx
from pwshpy.domain.enums import AceType, AclEntryKind
from pwshpy.domain.errors import NativeCallError

_LINUX = sys.platform.startswith("linux")
_os: Any = os  # funnel the Linux-only os.getxattr past the type checker's platform pruning

_AclTag = posix_acl._AclTag  # pyright: ignore[reportPrivateUsage] - white-box test of the wire-tag parse

#: The ``system.posix_acl_access`` blob for a mode-0640 file after granting ``user:0`` r-x and
#: ``group:0`` rw-: version 2, then (tag, perm, id) per entry in canonical order - owner rw-,
#: user 0 r-x, owning group r--, group 0 rw-, mask rwx (recomputed), other ---.
_GRANTED_BLOB = bytes.fromhex(
    "0200000001000600ffffffff020005000000000004000400ffffffff080006000000000010000700ffffffff20000000ffffffff"
)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("perm", "rwx"),
    [(7, "rwx"), (5, "r-x"), (6, "rw-"), (4, "r--"), (0, "---"), (1, "--x")],
)
def test_perm_to_rwx(perm: int, rwx: str) -> None:
    """A 0-7 POSIX permission renders to its rwx string."""
    assert perm_to_rwx(perm) == rwx


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("raw", "member"),
    [
        (0x01, _AclTag.USER_OBJ),
        (0x02, _AclTag.USER),
        (0x04, _AclTag.GROUP_OBJ),
        (0x08, _AclTag.GROUP),
        (0x10, _AclTag.MASK),
        (0x20, _AclTag.OTHER),
    ],
)
def test_acl_tag_parses_every_kernel_tag(raw: int, member: object) -> None:
    """Each e_tag the kernel defines parses to its member, and packs back to the same bytes."""
    assert _AclTag(raw) is member
    assert struct.pack("<H", _AclTag(raw)) == struct.pack("<H", raw)


@pytest.mark.os_agnostic
def test_acl_tag_keeps_an_unknown_tag_as_its_raw_value() -> None:
    """An unknown e_tag still parses (no error at read time), keeps its value, and has no kind or order.

    So, as with the bare integer before, it fails only where it is marshaled or written back: a
    KeyError from the kind / canonical-order lookup.
    """
    unknown = _AclTag(0x40)
    assert isinstance(unknown, _AclTag)
    assert int(unknown) == 0x40
    assert unknown not in (*_AclTag,)
    kinds = posix_acl._KINDS  # pyright: ignore[reportPrivateUsage] - white-box test
    order = posix_acl._ORDER  # pyright: ignore[reportPrivateUsage] - white-box test
    with pytest.raises(KeyError):
        _ = kinds[unknown]
    with pytest.raises(KeyError):
        _ = order[unknown]


@pytest.mark.os_agnostic
def test_acl_tag_refuses_a_non_integer() -> None:
    """Only an integer can be a tag; anything else is the ordinary Enum ValueError."""
    with pytest.raises(ValueError, match="is not a valid _AclTag"):
        _AclTag("1")


@pytest.fixture
def scratch_file() -> Iterator[str]:
    """A private mode-0640 temp file on the local fs (removed on teardown)."""
    fd, path = tempfile.mkstemp()
    os.close(fd)
    Path(path).chmod(0o640)
    try:
        yield path
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.os_linux
@pytest.mark.skipif(not _LINUX, reason="POSIX ACLs are Linux")
def test_read_base_entries(scratch_file: str) -> None:
    """A file with no extended ACL yields owner/owning-group/other from its mode bits."""
    by_kind = {e.kind: e for e in iter_acl(scratch_file)}
    assert AclEntryKind.OWNER in by_kind
    assert AclEntryKind.OWNING_GROUP in by_kind
    assert AclEntryKind.OTHER in by_kind
    assert by_kind[AclEntryKind.OWNER].permissions == "rw-"  # mode 0640 owner
    assert by_kind[AclEntryKind.OWNING_GROUP].permissions == "r--"  # mode 0640 group
    assert by_kind[AclEntryKind.OTHER].permissions == "---"  # mode 0640 other
    assert all(e.access_type is AceType.ALLOW for e in by_kind.values())  # POSIX is allow-only


@pytest.mark.os_linux
@pytest.mark.skipif(not _LINUX, reason="POSIX ACLs are Linux")
def test_add_remove_ace_and_set_owner(scratch_file: str) -> None:
    """add_ace grants a named user (+ a mask), remove_ace clears it, set_owner chowns to self."""
    controller = PosixAclController()
    try:
        controller.add_ace(scratch_file, "user:0", rights=5)  # root gets r-x
    except NativeCallError as exc:
        pytest.skip(f"filesystem without POSIX ACL support: {exc}")

    named = [e for e in iter_acl(scratch_file) if e.kind is AclEntryKind.USER and e.trustee_sid == "0"]
    assert len(named) == 1
    assert named[0].permissions == "r-x"
    assert named[0].trustee_name == "root"
    assert any(e.kind is AclEntryKind.MASK for e in iter_acl(scratch_file))  # a mask is created for named entries

    controller.remove_ace(scratch_file, "user:0")
    after = list(iter_acl(scratch_file))
    assert not any(e.kind is AclEntryKind.USER for e in after)  # named entry gone
    assert not any(e.kind is AclEntryKind.MASK for e in after)  # mask dropped with the last named entry

    owner_uid = next(e.trustee_sid for e in after if e.kind is AclEntryKind.OWNER)
    controller.set_owner(scratch_file, owner_uid)  # chown to the current owner (unprivileged no-op)


@pytest.mark.os_linux
@pytest.mark.skipif(not _LINUX, reason="POSIX ACLs are Linux")
def test_add_ace_writes_the_canonical_xattr_bytes(scratch_file: str) -> None:
    """The packed ACL blob is byte-exact: header, canonical entry order, recomputed mask."""
    controller = PosixAclController()
    try:
        controller.add_ace(scratch_file, "user:0", rights=5)
        controller.add_ace(scratch_file, "group:0", rights=6)
    except NativeCallError as exc:
        pytest.skip(f"filesystem without POSIX ACL support: {exc}")
    assert _os.getxattr(scratch_file, "system.posix_acl_access") == _GRANTED_BLOB
