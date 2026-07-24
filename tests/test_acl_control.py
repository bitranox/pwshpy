"""native ACL control (mutating): fake-backend unit tests + real integration.

The unit tests drive :class:`NativeAclController` against an in-memory
``win32security`` double (``os_agnostic``): ACEs are added/removed on a fake DACL,
the owner is set, ``SetFileSecurity`` is written back, and native failures wrap.

The ``local_only`` + ``mutating`` test round-trips a real DACL on a temp file
(add ACE -> read via ps.acl -> remove -> set owner); it runs only on the
disposable throwaway VM.
"""

# The fakes below deliberately mirror win32security's PascalCase API.
# ruff: noqa: N802

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from pwshpy.adapters.native import acl_control as mod
from pwshpy.adapters.native.acl_control import NativeAclController
from pwshpy.domain.enums import AceType
from pwshpy.domain.errors import NativeCallError

# In the fakes a SID is represented by its string form, so Convert* round-trips it.


class _FakeDacl:
    def __init__(self) -> None:
        self.aces: list[dict[str, Any]] = []

    def GetAceCount(self) -> int:
        return len(self.aces)

    def GetAce(self, index: int) -> tuple[tuple[int, int], int, str]:
        a = self.aces[index]
        return ((a["type"], a["flags"]), a["mask"], a["sid"])

    def AddAccessAllowedAce(self, revision: int, mask: int, sid: str) -> None:
        self.aces.append({"type": 0, "flags": 0, "mask": mask, "sid": sid})

    def AddAccessDeniedAce(self, revision: int, mask: int, sid: str) -> None:
        self.aces.append({"type": 1, "flags": 0, "mask": mask, "sid": sid})

    def DeleteAce(self, index: int) -> None:
        del self.aces[index]


class _FakeSD:
    def __init__(self, dacl: _FakeDacl | None) -> None:
        self._dacl = dacl
        self.owner: str | None = None

    def GetSecurityDescriptorDacl(self) -> _FakeDacl | None:
        return self._dacl

    def SetSecurityDescriptorDacl(self, present: int, dacl: _FakeDacl, defaulted: int) -> None:
        self._dacl = dacl

    def SetSecurityDescriptorOwner(self, sid: str, defaulted: int) -> None:
        self.owner = sid


class _FakeSecurity:
    ACL_REVISION = 2
    DACL_SECURITY_INFORMATION = 4
    OWNER_SECURITY_INFORMATION = 1

    def __init__(self, *, dacl: _FakeDacl | None = None, null_dacl: bool = False) -> None:
        self.sd = _FakeSD(None if null_dacl else (dacl if dacl is not None else _FakeDacl()))
        self.set_file_calls: list[tuple[str, int]] = []
        self.fail: str | None = None

    def GetFileSecurity(self, path: str, flags: int) -> _FakeSD:
        if self.fail == "get":
            raise OSError("access denied")
        return self.sd

    def SetFileSecurity(self, path: str, flags: int, sd: _FakeSD) -> None:
        self.set_file_calls.append((path, flags))

    def ACL(self) -> _FakeDacl:
        return _FakeDacl()

    def LookupAccountName(self, system: Any, name: str) -> tuple[str, str, int]:
        return (name, "DOM", 1)

    def ConvertStringSidToSid(self, sid_string: str) -> str:
        return sid_string

    def ConvertSidToStringSid(self, sid: str) -> str:
        return sid


def _use(monkeypatch: pytest.MonkeyPatch, security: _FakeSecurity) -> None:
    monkeypatch.setattr(mod, "_load", lambda: security)


@pytest.mark.os_agnostic
def test_add_allowed_ace(monkeypatch: pytest.MonkeyPatch) -> None:
    """add_ace appends an allow ACE and writes the DACL back."""
    sec = _FakeSecurity()
    _use(monkeypatch, sec)
    NativeAclController().add_ace("C:/x", "S-1-5-32-545", 0x120089)
    aces = sec.sd.GetSecurityDescriptorDacl().aces  # type: ignore[union-attr]
    assert aces == [{"type": 0, "flags": 0, "mask": 0x120089, "sid": "S-1-5-32-545"}]
    assert ("C:/x", sec.DACL_SECURITY_INFORMATION) in sec.set_file_calls


@pytest.mark.os_agnostic
def test_add_denied_ace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DENY access_type adds an access-denied ACE."""
    sec = _FakeSecurity()
    _use(monkeypatch, sec)
    NativeAclController().add_ace("C:/x", "S-1-1-0", 1, access_type=AceType.DENY)
    assert sec.sd.GetSecurityDescriptorDacl().aces[0]["type"] == 1  # type: ignore[union-attr]


@pytest.mark.os_agnostic
def test_add_ace_creates_dacl_when_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """A null DACL is replaced by a fresh ACL holding the new ACE."""
    sec = _FakeSecurity(null_dacl=True)
    _use(monkeypatch, sec)
    NativeAclController().add_ace("C:/x", "S-1-1-0", 1)
    assert sec.sd.GetSecurityDescriptorDacl().aces[0]["sid"] == "S-1-1-0"  # type: ignore[union-attr]


@pytest.mark.os_agnostic
def test_remove_ace_by_trustee(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove_ace deletes the trustee's ACEs and keeps the others."""
    dacl = _FakeDacl()
    dacl.aces = [
        {"type": 0, "flags": 0, "mask": 1, "sid": "S-1-1-0"},
        {"type": 0, "flags": 0, "mask": 2, "sid": "S-1-5-32-545"},
    ]
    sec = _FakeSecurity(dacl=dacl)
    _use(monkeypatch, sec)
    NativeAclController().remove_ace("C:/x", "S-1-1-0")
    assert [a["sid"] for a in sec.sd.GetSecurityDescriptorDacl().aces] == ["S-1-5-32-545"]  # type: ignore[union-attr]


@pytest.mark.os_agnostic
def test_remove_ace_filtered_by_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """With access_type set, only the matching ACE type is removed."""
    dacl = _FakeDacl()
    dacl.aces = [
        {"type": 0, "flags": 0, "mask": 1, "sid": "S-1-1-0"},
        {"type": 1, "flags": 0, "mask": 1, "sid": "S-1-1-0"},
    ]
    sec = _FakeSecurity(dacl=dacl)
    _use(monkeypatch, sec)
    NativeAclController().remove_ace("C:/x", "S-1-1-0", access_type=AceType.DENY)
    assert [a["type"] for a in sec.sd.GetSecurityDescriptorDacl().aces] == [0]  # type: ignore[union-attr]


@pytest.mark.os_agnostic
def test_set_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """set_owner writes the owner SID and back-writes OWNER_SECURITY_INFORMATION."""
    sec = _FakeSecurity()
    _use(monkeypatch, sec)
    NativeAclController().set_owner("C:/x", "S-1-5-32-544")
    assert sec.sd.owner == "S-1-5-32-544"
    assert ("C:/x", sec.OWNER_SECURITY_INFORMATION) in sec.set_file_calls


@pytest.mark.os_agnostic
def test_native_failure_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw win32security failure surfaces as NativeCallError."""
    sec = _FakeSecurity()
    sec.fail = "get"
    _use(monkeypatch, sec)
    with pytest.raises(NativeCallError):
        NativeAclController().add_ace("C:/x", "S-1-1-0", 1)


# --- Real integration: run only on the disposable throwaway VM ---------------

_GUESTS = "S-1-5-32-546"  # BUILTIN\Guests - unlikely to be pre-present on a temp file
_ADMINS = "S-1-5-32-544"
_FILE_GENERIC_READ = 0x120089


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only ACL mutation; skip off Windows")
def test_acl_roundtrip_on_temp_file() -> None:
    """Add an ACE, confirm via ps.acl, remove it, and set the owner - on a temp file."""
    from pwshpy.composition import build_ps

    ps = build_ps()
    fd, path = tempfile.mkstemp(suffix="_pwshpy_acl")
    os.close(fd)
    try:
        ps.add_acl_ace(path, _GUESTS, _FILE_GENERIC_READ)
        assert _GUESTS in {e.trustee_sid for e in ps.get_acl(path)}
        ps.remove_acl_ace(path, _GUESTS)
        assert _GUESTS not in {e.trustee_sid for e in ps.get_acl(path)}
        ps.set_owner(path, _ADMINS)
        first = next(iter(ps.get_acl(path)))
        assert first.owner_sid == _ADMINS
    finally:
        with contextlib.suppress(OSError):
            Path(path).unlink()
