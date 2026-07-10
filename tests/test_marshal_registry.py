"""Registry marshaling: native ``REG_*`` ids / hive tokens -> typed domain values.

These are pure, deterministic unit tests of the marshaling seam (no registry
backend), so they pin the exact type/hive/data mapping on every OS.
"""

from __future__ import annotations

import pytest

from pwshpy.adapters.native.marshal import (
    normalize_registry_data,
    to_registry_hive,
    to_registry_value_type,
)
from pwshpy.domain.enums import RegistryHive, RegistryValueType


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("type_int", "expected"),
    [
        (0, RegistryValueType.REG_NONE),
        (1, RegistryValueType.REG_SZ),
        (2, RegistryValueType.REG_EXPAND_SZ),
        (3, RegistryValueType.REG_BINARY),
        (4, RegistryValueType.REG_DWORD),
        (5, RegistryValueType.REG_DWORD_BIG_ENDIAN),
        (6, RegistryValueType.REG_LINK),
        (7, RegistryValueType.REG_MULTI_SZ),
        (11, RegistryValueType.REG_QWORD),
    ],
)
def test_known_value_types_map_exactly(type_int: int, expected: RegistryValueType) -> None:
    """Each native REG_* id maps to its :class:`RegistryValueType` member."""
    assert to_registry_value_type(type_int) is expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize("unknown", [-1, 99, 12345])
def test_unknown_value_type_falls_back_to_none(unknown: int) -> None:
    """An unrecognized REG_* id degrades to REG_NONE, never raises."""
    assert to_registry_value_type(unknown) is RegistryValueType.REG_NONE


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("HKLM", RegistryHive.HKLM),
        ("hklm", RegistryHive.HKLM),
        ("HKEY_LOCAL_MACHINE", RegistryHive.HKLM),
        ("hkey_current_user", RegistryHive.HKCU),
        ("HKCU", RegistryHive.HKCU),
        ("HKCR", RegistryHive.HKCR),
        ("HKU", RegistryHive.HKU),
        ("HKCC", RegistryHive.HKCC),
    ],
)
def test_hive_tokens_map_short_and_long_case_insensitive(token: str, expected: RegistryHive) -> None:
    """Short (``HKLM``) and long (``HKEY_LOCAL_MACHINE``) hive names both resolve."""
    assert to_registry_hive(token) is expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize("bad", ["", "HKXX", "SOFTWARE", "HKEY_BOGUS"])
def test_unknown_hive_raises_key_error(bad: str) -> None:
    """An unknown hive token raises KeyError (the adapter maps it to NativeCallError)."""
    with pytest.raises(KeyError):
        to_registry_hive(bad)


@pytest.mark.os_agnostic
def test_binary_data_becomes_a_hex_string() -> None:
    """REG_BINARY bytes normalize to a lowercase hex string (JSON-safe)."""
    assert normalize_registry_data(bytes([0, 255, 16])) == "00ff10"
    assert normalize_registry_data(b"") == ""


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("text", "text"),
        (42, 42),
        (["a", "b"], ["a", "b"]),
        (None, None),
    ],
)
def test_non_binary_data_passes_through(
    value: str | int | list[str] | None, expected: str | int | list[str] | None
) -> None:
    """Strings, ints, REG_MULTI_SZ lists and None pass through unchanged."""
    assert normalize_registry_data(value) == expected
