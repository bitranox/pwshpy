"""POSIX ACLs: pure perm rendering (os_agnostic) + a hermetic read/mutate round-trip (Linux).

The perm->rwx mapping is pure, so it runs everywhere. The live read and add/remove/set_owner
round-trip work on a private temp file (no host state touched), so they are os_linux - runnable in
CI's ubuntu lane - and skip cleanly on a filesystem without POSIX ACL support.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from pwshpy.adapters.native.posix_acl import PosixAclController, iter_acl, perm_to_rwx
from pwshpy.domain.enums import AceType, AclEntryKind
from pwshpy.domain.errors import NativeCallError

_LINUX = sys.platform.startswith("linux")


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("perm", "rwx"),
    [(7, "rwx"), (5, "r-x"), (6, "rw-"), (4, "r--"), (0, "---"), (1, "--x")],
)
def test_perm_to_rwx(perm: int, rwx: str) -> None:
    """A 0-7 POSIX permission renders to its rwx string."""
    assert perm_to_rwx(perm) == rwx


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
