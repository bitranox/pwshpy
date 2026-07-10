"""native registry source over ``lib_registry`` (Windows-only at runtime).

Typed facade over the untyped ``lib_registry`` surface.  Mirrors
``Get-ItemProperty`` (the values under a key) and ``Get-ChildItem`` (the
immediate subkeys); each row marshals into a
:class:`~pwshpy.domain.records.RegistryValue` /
:class:`~pwshpy.domain.records.RegistryKey` via ``model_construct``.

``lib_registry`` is a win32-gated runtime dependency, imported lazily so that
``import pwshpy`` stays clean on a portable (non-Windows) install.  When it is
absent the source raises
:class:`~pwshpy.domain.errors.PlatformUnsupportedError`.  In dev/test it is
present on every OS and transparently uses the in-memory ``fake_winreg`` backend,
so the whole subsystem is tested hermetically (even on Linux).

Contents:
    * :func:`iter_registry_values` — yield one :class:`RegistryValue` per value under a key.
    * :func:`iter_registry_keys` — yield one :class:`RegistryKey` per immediate subkey.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

from ...domain.enums import RegistryHive
from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import RegistryKey, RegistryValue
from .marshal import normalize_registry_data, to_registry_hive, to_registry_value_type


def load_registry_backend() -> tuple[Any, type[BaseException]]:
    """Import ``lib_registry`` lazily; absent means a portable (non-Windows) install.

    Returns the ``Registry`` class plus the ``RegistryError`` base so callers can
    map any native registry failure into a :class:`NativeCallError`.
    """
    try:
        lib: Any = importlib.import_module("lib_registry")
        exceptions: Any = importlib.import_module("lib_registry.exceptions")
    except ImportError as exc:  # pragma: no cover - only on a portable install
        raise PlatformUnsupportedError(
            "The registry subsystem requires lib_registry (Windows only). Install "
            "pwshpy on Windows, or add the [dev] extra for hermetic fake_winreg tests."
        ) from exc
    return lib.Registry, exceptions.RegistryError


def normalize_key(key: str) -> str:
    """Accept ``/`` or ``\\`` separators; return a hive-prefixed backslash path."""
    return key.replace("/", "\\").strip("\\")


def split_hive(normalized: str) -> tuple[RegistryHive, str]:
    """Split ``HKLM\\SOFTWARE\\X`` into its :class:`RegistryHive` and the path below it."""
    hive_token, _, sub_key = normalized.partition("\\")
    try:
        hive = to_registry_hive(hive_token)
    except KeyError as exc:
        raise NativeCallError(f"Unknown registry hive: {hive_token!r}") from exc
    return hive, sub_key


def iter_registry_values(key: str) -> Iterator[RegistryValue]:
    """Yield a :class:`RegistryValue` for every value under registry ``key``.

    ``key`` is a hive-prefixed path (``HKLM/SOFTWARE/...``); ``/`` and ``\\``
    separators and short/long hive names are both accepted.

    Example:
        >>> from pwshpy.domain.records import RegistryValue
        >>> key = "HKLM/SOFTWARE/Microsoft/Windows NT/CurrentVersion"
        >>> all(isinstance(v, RegistryValue) for v in iter_registry_values(key))
        True
    """
    registry_cls, registry_error = load_registry_backend()
    normalized = normalize_key(key)
    hive, sub_key = split_hive(normalized)
    reg: Any = registry_cls()
    try:
        for value_name, value_data, value_type in reg.values(normalized):
            yield RegistryValue.model_construct(
                hive=hive,
                key=sub_key,
                name=str(value_name),
                type=to_registry_value_type(int(value_type)),
                data=normalize_registry_data(value_data),
            )
    except registry_error as exc:
        raise NativeCallError(str(exc) or type(exc).__name__) from exc


def iter_registry_keys(key: str) -> Iterator[RegistryKey]:
    """Yield a :class:`RegistryKey` for every immediate subkey of registry ``key``.

    Example:
        >>> from pwshpy.domain.records import RegistryKey
        >>> all(isinstance(k, RegistryKey) for k in iter_registry_keys("HKLM/SOFTWARE"))
        True
    """
    registry_cls, registry_error = load_registry_backend()
    normalized = normalize_key(key)
    hive, sub_key = split_hive(normalized)
    reg: Any = registry_cls()
    try:
        for subkey_name in reg.subkeys(normalized):
            yield RegistryKey.model_construct(hive=hive, key=sub_key, name=str(subkey_name))
    except registry_error as exc:
        raise NativeCallError(str(exc) or type(exc).__name__) from exc


__all__ = ["iter_registry_keys", "iter_registry_values", "load_registry_backend", "normalize_key", "split_hive"]
