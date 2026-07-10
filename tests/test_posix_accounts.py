"""Native local accounts on POSIX: live pwd/grp read (skipped on Windows).

Deterministic because every POSIX box has root at uid/gid 0 and at least one nologin
system account. Runs on Linux/macOS CI; skipped on Windows (pwd/grp do not exist there).
"""

from __future__ import annotations

import sys

import pytest

from pwshpy.adapters.native.posix_accounts import iter_local_groups, iter_local_users
from pwshpy.composition import build_ps
from pwshpy.domain.records import LocalGroup, LocalUser

pytestmark = [
    pytest.mark.os_posix,
    pytest.mark.skipif(sys.platform == "win32", reason="pwd/grp are POSIX-only"),
]


def test_local_users_include_root() -> None:
    """iter_local_users yields typed records; root is uid 0, named root, and enabled."""
    users = {u.sid: u for u in iter_local_users()}
    assert users
    assert all(isinstance(u, LocalUser) for u in users.values())
    assert "0" in users  # the uid is carried in `sid`
    assert users["0"].name == "root"
    assert users["0"].enabled is True  # root has a real login shell


def test_local_groups_include_gid_zero() -> None:
    """iter_local_groups yields typed records including gid 0 (root/wheel)."""
    groups = {g.sid: g for g in iter_local_groups()}
    assert groups
    assert all(isinstance(g, LocalGroup) for g in groups.values())
    assert "0" in groups


def test_nologin_system_account_is_disabled() -> None:
    """At least one system account has a nologin/false shell, so `enabled` is False."""
    assert any(not u.enabled for u in iter_local_users())


def test_facade_dispatches_to_posix_backend() -> None:
    """On POSIX, build_ps().get_local_user() streams the pwd-backed records (uid as sid)."""
    first = build_ps().get_local_user().first()
    assert isinstance(first, LocalUser)
    assert first.sid.isdigit()  # a POSIX uid, not a Windows SID
