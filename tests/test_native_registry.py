"""Registry native adapter over ``lib_registry`` (structural, cross-backend).

Exact value/type marshaling is unit-tested in ``test_marshal_registry``; the
exact live-Windows behaviour is pinned against real ``pwsh`` in
``test_registry_pwsh_oracle``.  Here we drive the adapter end to end against
whatever backend ``lib_registry`` uses - the real registry on Windows, the
in-memory ``fake_winreg`` minimal registry on Linux/macOS - asserting the
structural contract and the error mapping, deterministically on every OS.
"""

from __future__ import annotations

import pytest

from pwshpy.adapters.native.registry import iter_registry_keys, iter_registry_values
from pwshpy.domain.enums import RegistryHive, RegistryValueType
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import RegistryKey, RegistryValue

# Present on any real Windows install AND in fake_winreg's minimal test registry.
_STABLE_KEY = "HKLM/SOFTWARE/Microsoft/Windows NT/CurrentVersion"
_STABLE_SUBKEY_PATH = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"
_STABLE_PARENT = "HKLM/SOFTWARE"


@pytest.mark.os_agnostic
def test_values_yield_typed_records_under_the_requested_hive() -> None:
    """iter_registry_values yields RegistryValue records tagged with the parsed hive/key."""
    values = list(iter_registry_values(_STABLE_KEY))
    assert values, "the CurrentVersion key always has values"
    for value in values:
        assert isinstance(value, RegistryValue)
        assert value.hive is RegistryHive.HKLM
        assert isinstance(value.type, RegistryValueType)
        assert value.key == _STABLE_SUBKEY_PATH


@pytest.mark.os_agnostic
def test_subkeys_yield_typed_records() -> None:
    """iter_registry_keys yields RegistryKey records with a non-empty leaf name."""
    keys = list(iter_registry_keys(_STABLE_PARENT))
    assert keys, "HKLM\\SOFTWARE always has subkeys"
    for key in keys:
        assert isinstance(key, RegistryKey)
        assert key.hive is RegistryHive.HKLM
        assert key.key == "SOFTWARE"
        assert key.name


@pytest.mark.os_agnostic
def test_forward_and_back_slashes_are_equivalent() -> None:
    """``/`` and ``\\`` separators resolve to the same key."""
    forward = {v.name for v in iter_registry_values(_STABLE_KEY)}
    backslash = {v.name for v in iter_registry_values(_STABLE_KEY.replace("/", "\\"))}
    assert forward == backslash


@pytest.mark.os_agnostic
def test_long_hive_name_is_accepted() -> None:
    """The long ``HKEY_LOCAL_MACHINE`` form is accepted like the short ``HKLM``."""
    values = list(iter_registry_values(_STABLE_KEY.replace("HKLM", "HKEY_LOCAL_MACHINE")))
    assert values
    assert all(v.hive is RegistryHive.HKLM for v in values)


@pytest.mark.os_agnostic
def test_unknown_hive_raises_native_error() -> None:
    """A key with an unrecognized hive prefix surfaces a NativeCallError."""
    with pytest.raises(NativeCallError):
        list(iter_registry_values("BOGUS/SOFTWARE"))


@pytest.mark.os_agnostic
def test_missing_key_raises_native_error() -> None:
    """A non-existent key surfaces lib_registry's failure as a NativeCallError."""
    with pytest.raises(NativeCallError):
        list(iter_registry_keys("HKLM/SOFTWARE/pwshpy_does_not_exist_1a2b3c"))
