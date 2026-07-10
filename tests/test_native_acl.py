"""ACL native adapter over win32security.

A fake ``win32security`` (injected at the adapter's load seam) exercises the ACE
marshaling, SID resolution, null-DACL handling, and error wrapping on every OS; a
real structural test reads C:\\Windows on Windows.  Exact live behaviour is pinned
against Get-Acl in ``test_acl_pwsh_oracle``.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from pwshpy.adapters.native import acl as acl_mod
from pwshpy.adapters.native.acl import iter_acl
from pwshpy.domain.enums import AceType
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import AclEntry


class _FakeDacl:
    def __init__(self, aces: list[tuple[tuple[int, int], int, str]]) -> None:
        self._aces = aces

    def GetAceCount(self) -> int:  # noqa: N802 - win32 API name
        return len(self._aces)

    def GetAce(self, index: int) -> tuple[tuple[int, int], int, str]:  # noqa: N802 - win32 API name
        return self._aces[index]


class _FakeDescriptor:
    def __init__(self, owner: str | None, dacl: _FakeDacl | None) -> None:
        self._owner = owner
        self._dacl = dacl

    def GetSecurityDescriptorOwner(self) -> str | None:  # noqa: N802 - win32 API name
        return self._owner

    def GetSecurityDescriptorDacl(self) -> _FakeDacl | None:  # noqa: N802 - win32 API name
        return self._dacl


class _FakeSecurity:
    OWNER_SECURITY_INFORMATION = 1
    DACL_SECURITY_INFORMATION = 4

    def __init__(self, descriptor: _FakeDescriptor) -> None:
        self._descriptor = descriptor

    def GetFileSecurity(self, path: str, flags: int) -> _FakeDescriptor:  # noqa: N802 - win32 API name
        return self._descriptor

    def ConvertSidToStringSid(self, sid: Any) -> str:  # noqa: N802 - win32 API name
        return f"S-{sid}"

    def LookupAccountSid(self, system: Any, sid: Any) -> tuple[str, str, int]:  # noqa: N802 - win32 API name
        return (f"name-{sid}", "DOM", 1)


@pytest.mark.os_agnostic
def test_iter_acl_marshals_aces(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each DACL ACE becomes an AclEntry with SID, type, mask, inherited flag, and name."""
    aces = [((0, 0), 2032127, "sidA"), ((1, 0x10), 100, "sidB")]  # allow / deny+inherited
    descriptor = _FakeDescriptor(owner="ownerSid", dacl=_FakeDacl(aces))
    monkeypatch.setattr(acl_mod, "_load", lambda: _FakeSecurity(descriptor))

    entries = list(iter_acl("C:\\Test"))
    assert len(entries) == 2

    allow = entries[0]
    assert allow.path == "C:\\Test"
    assert allow.owner_sid == "S-ownerSid"
    assert allow.trustee_sid == "S-sidA"
    assert allow.access_type is AceType.ALLOW
    assert allow.rights == 2032127
    assert allow.inherited is False
    assert allow.trustee_name == "DOM\\name-sidA"

    deny = entries[1]
    assert deny.access_type is AceType.DENY
    assert deny.inherited is True  # INHERITED_ACE (0x10) set


@pytest.mark.os_agnostic
def test_iter_acl_null_dacl_yields_implicit_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A null DACL (unprotected: everyone full access) yields one synthetic Everyone/Allow entry."""
    monkeypatch.setattr(acl_mod, "_load", lambda: _FakeSecurity(_FakeDescriptor("ownerSid", None)))
    entries = list(iter_acl("C:\\X"))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.trustee_sid == "S-1-1-0"  # Everyone
    assert entry.access_type is AceType.ALLOW
    assert entry.owner_sid == "S-ownerSid"


@pytest.mark.os_agnostic
def test_iter_acl_empty_dacl_yields_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A present-but-empty DACL (nobody has access) yields no entries - distinct from a null DACL."""
    monkeypatch.setattr(acl_mod, "_load", lambda: _FakeSecurity(_FakeDescriptor("o", _FakeDacl([]))))
    assert list(iter_acl("C:\\X")) == []


@pytest.mark.os_agnostic
def test_iter_acl_handles_none_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A descriptor with no owner SID yields owner_sid='' rather than crashing."""
    descriptor = _FakeDescriptor(None, _FakeDacl([((0, 0), 1, "sidA")]))
    monkeypatch.setattr(acl_mod, "_load", lambda: _FakeSecurity(descriptor))
    entry = next(iter_acl("C:\\X"))
    assert entry.owner_sid == ""


@pytest.mark.os_agnostic
def test_iter_acl_name_degrades_but_keeps_sid(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable trustee name degrades to '' while the SID is preserved."""

    class _NoName(_FakeSecurity):
        def LookupAccountSid(self, system: Any, sid: Any) -> tuple[str, str, int]:  # noqa: N802 - win32 API name
            raise OSError("no mapping")

    descriptor = _FakeDescriptor("o", _FakeDacl([((0, 0), 1, "s")]))
    monkeypatch.setattr(acl_mod, "_load", lambda: _NoName(descriptor))
    entry = next(iter_acl("C:\\X"))
    assert entry.trustee_name == ""
    assert entry.trustee_sid == "S-s"


@pytest.mark.os_agnostic
def test_iter_acl_wraps_missing_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GetFileSecurity failure surfaces as NativeCallError."""

    class _Boom(_FakeSecurity):
        def GetFileSecurity(self, path: str, flags: int) -> _FakeDescriptor:  # noqa: N802 - win32 API name
            raise OSError("path not found")

    monkeypatch.setattr(acl_mod, "_load", lambda: _Boom(_FakeDescriptor("o", None)))
    with pytest.raises(NativeCallError):
        list(iter_acl("C:\\missing"))


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="win32security is Windows-only")
def test_iter_acl_reads_real_windows_dir() -> None:
    """Against the real C:\\Windows DACL, the adapter yields SID-identified entries."""
    entries = list(iter_acl(r"C:\Windows"))
    assert entries
    assert all(isinstance(e, AclEntry) for e in entries)
    assert all(e.trustee_sid.startswith("S-1-") for e in entries)
    assert any(e.trustee_sid == "S-1-5-32-544" for e in entries)  # Administrators, whatever its localized name
