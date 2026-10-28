"""native registry control (mutating): fake-backend unit tests + real integration tests.

The unit tests drive :class:`NativeRegistryController` against a fake
``lib_registry`` backend (``os_agnostic``, hermetic on every OS): they assert the
right write is issued and the right record is returned, plus error wrapping.

The ``local_only`` + ``mutating`` test round-trips through the REAL registry under
``HKCU\\Software\\pwshpy_test`` (the sanctioned scratch namespace), creating +
removing everything it touches; it runs only on the disposable throwaway VM.
"""

from __future__ import annotations

import errno
from collections.abc import Callable
from typing import Any

import pytest

from pwshpy.adapters.native import registry_control as mod
from pwshpy.adapters.native.marshal import E_ACCESSDENIED, from_registry_value_type
from pwshpy.adapters.native.registry_control import NativeRegistryController
from pwshpy.domain.enums import RegistryValueType
from pwshpy.domain.errors import NativeAccessDeniedError, NativeCallError


class _FakeRegError(Exception):
    """Stand-in for lib_registry.exceptions.RegistryError."""


class _FakeRegistry:
    """In-memory ``lib_registry.Registry`` double recording the writes it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail: str | None = None

    def set_value(self, key: str, name: str, data: Any, value_type: int) -> None:
        if self.fail == "set":
            raise _FakeRegError("access denied")
        self.calls.append(("set_value", key, name, data, value_type))

    def delete_value(self, key: str, name: str) -> None:
        self.calls.append(("delete_value", key, name))

    def create_key(self, key: str, *, parents: bool = False) -> None:
        self.calls.append(("create_key", key, parents))

    def delete_key(self, key: str, *, delete_subkeys: bool = False) -> None:
        self.calls.append(("delete_key", key, delete_subkeys))


def _use(monkeypatch: pytest.MonkeyPatch, fake: _FakeRegistry) -> None:
    monkeypatch.setattr(mod, "load_registry_backend", lambda: ((lambda: fake), _FakeRegError))


@pytest.mark.os_agnostic
def test_set_value_writes_and_returns_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """set_value issues the native write (with the mapped type id) and returns the record."""
    fake = _FakeRegistry()
    _use(monkeypatch, fake)
    result = NativeRegistryController().set_value("HKCU/Software/pwshpy_test", "N", 42, RegistryValueType.REG_DWORD)
    assert result.name == "N"
    assert result.data == 42
    assert result.type is RegistryValueType.REG_DWORD
    assert fake.calls == [
        ("set_value", "HKCU\\Software\\pwshpy_test", "N", 42, from_registry_value_type(RegistryValueType.REG_DWORD))
    ]


@pytest.mark.os_agnostic
def test_remove_value_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove_value calls delete_value on the backend."""
    fake = _FakeRegistry()
    _use(monkeypatch, fake)
    NativeRegistryController().remove_value("HKCU/Software/pwshpy_test", "N")
    assert fake.calls == [("delete_value", "HKCU\\Software\\pwshpy_test", "N")]


@pytest.mark.os_agnostic
def test_create_key_returns_the_created_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_key creates with parents and returns a RegistryKey split into parent + name."""
    fake = _FakeRegistry()
    _use(monkeypatch, fake)
    result = NativeRegistryController().create_key("HKCU/Software/pwshpy_test/Sub")
    assert result.name == "Sub"
    assert result.key == "Software\\pwshpy_test"
    assert fake.calls == [("create_key", "HKCU\\Software\\pwshpy_test\\Sub", True)]


@pytest.mark.os_agnostic
def test_remove_key_passes_recursive_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove_key forwards the recursive flag as delete_subkeys."""
    fake = _FakeRegistry()
    _use(monkeypatch, fake)
    NativeRegistryController().remove_key("HKCU/Software/pwshpy_test", recursive=True)
    assert fake.calls == [("delete_key", "HKCU\\Software\\pwshpy_test", True)]


@pytest.mark.os_agnostic
def test_backend_failure_wrapped_in_native_call_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw RegistryError surfaces as NativeCallError."""
    fake = _FakeRegistry()
    fake.fail = "set"
    _use(monkeypatch, fake)
    with pytest.raises(NativeCallError):
        NativeRegistryController().set_value("HKCU/Software/pwshpy_test", "N", 1, RegistryValueType.REG_SZ)


class _WindowsPermissionError(PermissionError):
    """A PermissionError with winerror, which only Windows sets; pinned here on any OS."""

    winerror: int


class _DeniedRegistry:
    """A registry double whose every write fails as a denied ``winreg.OpenKey`` does (raw)."""

    def _deny(self, *args: object, **kwargs: object) -> None:
        exc = _WindowsPermissionError(errno.EACCES, "denied")
        exc.winerror = 5
        raise exc

    set_value = delete_value = create_key = delete_key = _deny


_CONTROL_CALLS: list[Callable[[NativeRegistryController], object]] = [
    lambda c: c.set_value("HKLM/SAM/SAM", "N", 1, RegistryValueType.REG_DWORD),
    lambda c: c.remove_value("HKLM/SAM/SAM", "N"),
    lambda c: c.create_key("HKLM/SAM/SAM/Sub"),
    lambda c: c.remove_key("HKLM/SAM/SAM"),
]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("call", _CONTROL_CALLS, ids=["set_value", "remove_value", "create_key", "remove_key"])
def test_denied_open_key_is_typed_access_denied(
    monkeypatch: pytest.MonkeyPatch, call: Callable[[NativeRegistryController], object]
) -> None:
    """A raw denied OpenKey surfaces as NativeAccessDeniedError carrying E_ACCESSDENIED."""
    monkeypatch.setattr(mod, "load_registry_backend", lambda: (_DeniedRegistry, _FakeRegError))
    with pytest.raises(NativeAccessDeniedError) as info:
        call(NativeRegistryController())
    assert (info.value.hresult, info.value.errno) == (E_ACCESSDENIED, errno.EACCES)


# --- Real integration: run only on the disposable throwaway VM ---------------

_SCRATCH_KEY = "HKCU/Software/pwshpy_test"


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
def test_registry_roundtrip_on_real_hive() -> None:
    """Create a scratch key, write/read/remove values, then remove the key - all restored."""
    from pwshpy.composition import build_ps

    ps = build_ps()
    try:
        ps.new_registry_key(_SCRATCH_KEY)
        ps.set_item_property(_SCRATCH_KEY, "Answer", 42, RegistryValueType.REG_DWORD)
        ps.set_item_property(_SCRATCH_KEY, "Name", "pwshpy", RegistryValueType.REG_SZ)
        values = {v.name: v.data for v in ps.get_item_property(_SCRATCH_KEY)}
        assert values.get("Answer") == 42
        assert values.get("Name") == "pwshpy"
        ps.remove_item_property(_SCRATCH_KEY, "Answer")
        remaining = {v.name for v in ps.get_item_property(_SCRATCH_KEY)}
        assert "Answer" not in remaining
        assert "Name" in remaining
    finally:
        ps.remove_registry_key(_SCRATCH_KEY, recursive=True)
    gone = ps.registry_keys("HKCU/Software").where(lambda k: k.name == "pwshpy_test").first()
    assert gone is None
