"""native registry CONTROL over ``lib_registry`` (Windows-only at runtime, **mutating**).

Set / Remove a registry value and New / Remove a registry key - the mutating
counterpart of the read-only :mod:`~pwshpy.adapters.native.registry` source.  It
shares ``lib_registry`` (real ``winreg`` on Windows, the in-memory ``fake_winreg``
backend on other OSes), reusing the same path-normalization + hive-split helpers,
so writes and reads round-trip against one store.

**MUTATING** - see CLAUDE.md "Development Safety".  Real integration tests scope
themselves to an ``HKCU\\Software\\pwshpy_test`` scratch key (created + removed),
which is the sanctioned local-mutation namespace.

Contents:
    * :class:`NativeRegistryController` - set_value / remove_value / create_key / remove_key.
"""

from __future__ import annotations

from typing import Any

from ...domain.enums import RegistryValueType
from ...domain.errors import NativeCallError
from ...domain.records import RegistryKey, RegistryValue
from .marshal import from_registry_value_type
from .registry import load_registry_backend, normalize_key, split_hive

_RegistryData = str | int | list[str] | None


class NativeRegistryController:
    """Mutating registry control over ``lib_registry`` (Set/Remove value, New/Remove key)."""

    def set_value(self, key: str, name: str, data: _RegistryData, value_type: RegistryValueType) -> RegistryValue:
        """Create or overwrite the value ``name`` under ``key``; return the written record."""
        registry_cls, registry_error = load_registry_backend()
        normalized = normalize_key(key)
        hive, sub_key = split_hive(normalized)
        reg: Any = registry_cls()
        try:
            reg.set_value(normalized, name, data, from_registry_value_type(value_type))
        except registry_error as exc:
            raise NativeCallError(str(exc) or type(exc).__name__) from exc
        return RegistryValue.model_construct(hive=hive, key=sub_key, name=name, type=value_type, data=data)

    def remove_value(self, key: str, name: str) -> None:
        """Delete the value ``name`` under ``key``."""
        registry_cls, registry_error = load_registry_backend()
        normalized = normalize_key(key)
        reg: Any = registry_cls()
        try:
            reg.delete_value(normalized, name)
        except registry_error as exc:
            raise NativeCallError(str(exc) or type(exc).__name__) from exc

    def create_key(self, key: str) -> RegistryKey:
        """Create registry ``key`` (with any missing parents); return the created key."""
        registry_cls, registry_error = load_registry_backend()
        normalized = normalize_key(key)
        hive, sub_key = split_hive(normalized)
        reg: Any = registry_cls()
        try:
            reg.create_key(normalized, parents=True)
        except registry_error as exc:
            raise NativeCallError(str(exc) or type(exc).__name__) from exc
        parent, _, name = sub_key.rpartition("\\")
        return RegistryKey.model_construct(hive=hive, key=parent, name=name)

    def remove_key(self, key: str, *, recursive: bool = False) -> None:
        """Delete registry ``key`` (``recursive`` also removes its subkeys)."""
        registry_cls, registry_error = load_registry_backend()
        normalized = normalize_key(key)
        reg: Any = registry_cls()
        try:
            reg.delete_key(normalized, delete_subkeys=recursive)
        except registry_error as exc:
            raise NativeCallError(str(exc) or type(exc).__name__) from exc


__all__ = ["NativeRegistryController"]
