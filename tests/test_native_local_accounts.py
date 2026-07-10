"""Local-accounts native adapter over win32net / win32security.

A fake ``win32net`` + ``win32security`` (injected at the adapter's load seam)
exercises the marshaling and the SID resolution on every OS; real structural
tests read the live accounts on Windows.  Exact live behaviour is pinned against
Get-LocalUser / Get-LocalGroup in ``test_local_accounts_pwsh_oracle``.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from pwshpy.adapters.native import local_accounts as la_mod
from pwshpy.adapters.native.local_accounts import iter_local_groups, iter_local_users
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import LocalGroup, LocalUser

_SID_BY_OBJ = {"sid-obj-Alice": "S-1-5-21-1", "sid-obj-Guest": "S-1-5-21-501", "sid-obj-Admins": "S-1-5-32-544"}


class _FakeNet:
    def NetUserEnum(  # noqa: N802 - win32 API name
        self, server: Any, level: int, filt: int, resume: int, pref_len: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        return (
            [
                {"name": "Alice", "flags": 0, "full_name": "Alice A", "comment": "admin user"},
                {"name": "Guest", "flags": 0x0002, "full_name": "", "comment": "built-in"},  # UF_ACCOUNTDISABLE
            ],
            2,
            0,
        )

    def NetLocalGroupEnum(  # noqa: N802 - win32 API name
        self, server: Any, level: int, resume: int, pref_len: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        return ([{"name": "Admins", "comment": "the admins"}], 1, 0)


class _FakeSec:
    def LookupAccountName(self, system: Any, name: str) -> tuple[str, str, int]:  # noqa: N802 - win32 API name
        return (f"sid-obj-{name}", "DOM", 1)

    def ConvertSidToStringSid(self, sid: Any) -> str:  # noqa: N802 - win32 API name
        return _SID_BY_OBJ[sid]


@pytest.mark.os_agnostic
def test_iter_local_users_marshals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Users are marshaled with the resolved SID and the UF_ACCOUNTDISABLE-derived enabled flag."""
    monkeypatch.setattr(la_mod, "_load", lambda: (_FakeNet(), _FakeSec()))
    users = list(iter_local_users())
    assert [u.name for u in users] == ["Alice", "Guest"]

    alice = users[0]
    assert alice.sid == "S-1-5-21-1"
    assert alice.enabled is True
    assert alice.full_name == "Alice A"
    assert alice.description == "admin user"

    guest = users[1]
    assert guest.enabled is False  # UF_ACCOUNTDISABLE set
    assert guest.sid == "S-1-5-21-501"


@pytest.mark.os_agnostic
def test_iter_local_groups_marshals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Groups are marshaled with the resolved SID and comment."""
    monkeypatch.setattr(la_mod, "_load", lambda: (_FakeNet(), _FakeSec()))
    groups = list(iter_local_groups())
    assert [g.name for g in groups] == ["Admins"]
    assert groups[0].sid == "S-1-5-32-544"
    assert groups[0].description == "the admins"


@pytest.mark.os_agnostic
def test_sid_lookup_degrades_when_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An account whose SID cannot be looked up yields an empty SID, not an error."""

    class _BadSec:
        def LookupAccountName(self, system: Any, name: str) -> tuple[str, str, int]:  # noqa: N802 - win32 API name
            raise OSError("no mapping")

        def ConvertSidToStringSid(self, sid: Any) -> str:  # noqa: N802 - win32 API name
            return ""

    monkeypatch.setattr(la_mod, "_load", lambda: (_FakeNet(), _BadSec()))
    users = list(iter_local_users())
    assert users[0].sid == ""


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="win32net is Windows-only")
def test_iter_local_users_reads_real_accounts() -> None:
    """Against the real SAM, the adapter yields typed users with SIDs."""
    users = list(iter_local_users())
    assert users
    assert all(isinstance(u, LocalUser) for u in users)
    assert all(u.sid.startswith("S-1-") for u in users if u.sid)


class _PagedNet:
    """A win32net stand-in that returns two pages via the resume handle."""

    def __init__(self) -> None:
        self.calls = 0

    def NetUserEnum(  # noqa: N802 - win32 API name
        self, server: Any, level: int, filt: int, resume: int, pref_len: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        self.calls += 1
        page = "Page1" if self.calls == 1 else "Page2"
        next_resume = 1 if self.calls == 1 else 0
        return ([{"name": page, "flags": 0, "full_name": "", "comment": ""}], 2, next_resume)


@pytest.mark.os_agnostic
def test_iter_local_users_wraps_enum_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A NetUserEnum failure (access denied / SAM RPC) surfaces as NativeCallError."""

    class _BadNet(_FakeNet):
        def NetUserEnum(  # noqa: N802 - win32 API name
            self, server: Any, level: int, filt: int, resume: int, pref_len: int
        ) -> tuple[list[dict[str, Any]], int, int]:
            raise OSError("access denied")

    monkeypatch.setattr(la_mod, "_load", lambda: (_BadNet(), _FakeSec()))
    with pytest.raises(NativeCallError):
        list(iter_local_users())


@pytest.mark.os_agnostic
def test_iter_local_users_follows_resume_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resume handle drives a second enumeration call until it returns 0."""
    net = _PagedNet()
    monkeypatch.setattr(la_mod, "_load", lambda: (net, _FakeSec()))
    names = [user.name for user in iter_local_users()]
    assert names == ["Page1", "Page2"]
    assert net.calls == 2


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="win32net is Windows-only")
def test_iter_local_groups_reads_real_groups() -> None:
    """Against the real SAM, the adapter yields typed groups with SIDs."""
    groups = list(iter_local_groups())
    assert groups
    assert all(isinstance(g, LocalGroup) for g in groups)
    assert any(g.sid == "S-1-5-32-544" for g in groups)  # the Administrators group, whatever its localized name
