"""native local-account control (mutating): fake-backend unit tests + real integration.

The unit tests drive :class:`NativeLocalAccountController` against fake ``win32net``
/ ``win32security`` doubles (``os_agnostic``): the right SAM call is issued, the
right record returned (SID-carrying), enable/disable toggles the flag, and native
failures wrap into :class:`NativeCallError`.

The ``local_only`` + ``mutating`` test runs the full lifecycle against the REAL SAM
under scratch principals (``pwshpy_test_*``) it creates and removes; it runs only on
the disposable throwaway VM.
"""

# The fakes below deliberately mirror win32net's PascalCase API.
# ruff: noqa: N802

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from pwshpy.adapters.native import local_account_control as mod
from pwshpy.adapters.native.local_account_control import NativeLocalAccountController
from pwshpy.domain.errors import NativeCallError, PwshPyError

_UF_ACCOUNTDISABLE = 0x0002
_UF_NORMAL_ACCOUNT = 0x0200


class _FakeNet:
    """In-memory ``win32net`` double recording the SAM calls it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.flags = _UF_NORMAL_ACCOUNT
        self.fail: str | None = None

    def NetUserAdd(self, server: Any, level: int, info: dict[str, Any]) -> None:
        if self.fail == "add":
            raise OSError("access denied")
        self.flags = int(info["flags"])
        self.calls.append(("NetUserAdd", info["name"]))

    def NetUserDel(self, server: Any, name: str) -> None:
        self.calls.append(("NetUserDel", name))

    def NetUserGetInfo(self, server: Any, name: str, level: int) -> dict[str, Any]:
        return {"flags": self.flags, "full_name": "Full", "comment": "Comment"}

    def NetUserSetInfo(self, server: Any, name: str, level: int, info: dict[str, Any]) -> None:
        if "flags" in info:
            self.flags = int(info["flags"])
        self.calls.append(("NetUserSetInfo", name, level))

    def NetLocalGroupAdd(self, server: Any, level: int, info: dict[str, Any]) -> None:
        self.calls.append(("NetLocalGroupAdd", info["name"]))

    def NetLocalGroupDel(self, server: Any, name: str) -> None:
        self.calls.append(("NetLocalGroupDel", name))

    def NetLocalGroupAddMembers(self, server: Any, group: str, level: int, members: list[dict[str, Any]]) -> None:
        self.calls.append(("AddMembers", group, members[0]["domainandname"]))

    def NetLocalGroupDelMembers(self, server: Any, group: str, members: list[str]) -> None:
        self.calls.append(("DelMembers", group, members[0]))


class _FakeSecurity:
    """In-memory ``win32security`` double resolving any name to a fixed SID."""

    def LookupAccountName(self, system: Any, name: str) -> tuple[Any, str, int]:
        return ("sid-object", "DOMAIN", 1)

    def ConvertSidToStringSid(self, sid: Any) -> str:
        return "S-1-5-21-test"


def _use(monkeypatch: pytest.MonkeyPatch, net: _FakeNet) -> None:
    monkeypatch.setattr(mod, "_load", lambda: (net, _FakeSecurity()))


@pytest.mark.os_agnostic
def test_new_user_creates_and_returns_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """new_user adds the user, sets the full name, and returns a SID-carrying record."""
    net = _FakeNet()
    _use(monkeypatch, net)
    result = NativeLocalAccountController().new_user("u", full_name="Full", disabled=True)
    assert result.name == "u"
    assert result.sid == "S-1-5-21-test"
    assert result.enabled is False
    assert ("NetUserAdd", "u") in net.calls
    assert ("NetUserSetInfo", "u", 1011) in net.calls


@pytest.mark.os_agnostic
def test_remove_user_calls_netuserdel(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove_user issues NetUserDel."""
    net = _FakeNet()
    _use(monkeypatch, net)
    NativeLocalAccountController().remove_user("u")
    assert net.calls == [("NetUserDel", "u")]


@pytest.mark.os_agnostic
def test_set_user_enabled_clears_disable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabling a disabled user clears UF_ACCOUNTDISABLE and reports enabled."""
    net = _FakeNet()
    net.flags = _UF_NORMAL_ACCOUNT | _UF_ACCOUNTDISABLE
    _use(monkeypatch, net)
    result = NativeLocalAccountController().set_user_enabled("u", enabled=True)
    assert result.enabled is True
    assert net.flags & _UF_ACCOUNTDISABLE == 0


@pytest.mark.os_agnostic
def test_new_group_creates_and_returns_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """new_group adds the group and returns a SID-carrying record."""
    net = _FakeNet()
    _use(monkeypatch, net)
    result = NativeLocalAccountController().new_group("g", description="d")
    assert result.name == "g"
    assert result.sid == "S-1-5-21-test"
    assert net.calls == [("NetLocalGroupAdd", "g")]


@pytest.mark.os_agnostic
def test_group_membership_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """add/remove group member issue the matching NetLocalGroup*Members calls."""
    net = _FakeNet()
    _use(monkeypatch, net)
    controller = NativeLocalAccountController()
    controller.add_group_member("g", "u")
    controller.remove_group_member("g", "u")
    assert net.calls == [("AddMembers", "g", "u"), ("DelMembers", "g", "u")]


@pytest.mark.os_agnostic
def test_remove_group_calls_netlocalgroupdel(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove_group issues NetLocalGroupDel."""
    net = _FakeNet()
    _use(monkeypatch, net)
    NativeLocalAccountController().remove_group("g")
    assert net.calls == [("NetLocalGroupDel", "g")]


@pytest.mark.os_agnostic
def test_native_failure_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw SAM failure surfaces as NativeCallError."""
    net = _FakeNet()
    net.fail = "add"
    _use(monkeypatch, net)
    with pytest.raises(NativeCallError):
        NativeLocalAccountController().new_user("u")


# --- Real integration: run only on the disposable throwaway VM ---------------

_U = "pwshpy_test_u"
_G = "pwshpy_test_g"
_PW = "Pwshpy!23Test"  # nosec B105 - scratch password, throwaway VM only


def _cleanup(ps: Any) -> None:
    with contextlib.suppress(PwshPyError):
        ps.remove_local_group(_G)
    with contextlib.suppress(PwshPyError):
        ps.remove_local_user(_U)


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
def test_local_account_lifecycle() -> None:
    """Create a user + group, toggle enable, manage membership, then remove all (throwaway VM)."""
    from pwshpy.composition import build_ps

    ps = build_ps()
    _cleanup(ps)  # start from a clean slate even if a prior run left scraps
    try:
        created = ps.new_local_user(_U, password=_PW, full_name="pwshpy scratch", description="test user")
        assert created.name == _U
        assert created.sid.startswith("S-1-5-21")
        assert ps.get_local_user().where(lambda u: u.name == _U).first() is not None
        assert ps.disable_local_user(_U).enabled is False
        assert ps.enable_local_user(_U).enabled is True
        ps.new_local_group(_G, description="pwshpy scratch group")
        assert ps.get_local_group().where(lambda g: g.name == _G).first() is not None
        ps.add_local_group_member(_G, _U)
        ps.remove_local_group_member(_G, _U)
        ps.remove_local_group(_G)
        assert ps.get_local_group().where(lambda g: g.name == _G).first() is None
        ps.remove_local_user(_U)
        assert ps.get_local_user().where(lambda u: u.name == _U).first() is None
    finally:
        _cleanup(ps)
