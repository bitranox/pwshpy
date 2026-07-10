"""native DNS resolver over stdlib socket — portable, hermetic (localhost)."""

from __future__ import annotations

import time

import pytest

import pwshpy.adapters.native.dns as dns_module
from pwshpy.adapters.native import resolve
from pwshpy.domain.enums import AddressFamily
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import DnsRecord


@pytest.mark.os_agnostic
def test_resolve_localhost_yields_loopback_record() -> None:
    """Resolving localhost yields a loopback address as a typed record."""
    records = list(resolve("localhost"))
    assert records
    assert all(isinstance(r, DnsRecord) for r in records)
    assert any(r.address in {"127.0.0.1", "::1"} for r in records)


@pytest.mark.os_agnostic
def test_resolve_deduplicates_addresses() -> None:
    """Each (family, address) pair appears at most once."""
    records = list(resolve("localhost"))
    keys = [(r.family, r.address) for r in records]
    assert len(keys) == len(set(keys))


@pytest.mark.os_agnostic
def test_resolve_marks_address_family() -> None:
    """Loopback records carry a concrete IPv4/IPv6 family."""
    for record in resolve("localhost"):
        assert record.family in {AddressFamily.IPV4, AddressFamily.IPV6}


@pytest.mark.os_agnostic
def test_resolve_unresolvable_raises_native_call_error() -> None:
    """An unresolvable name raises the typed NativeCallError."""
    with pytest.raises(NativeCallError):
        list(resolve("nonexistent.invalid."))


@pytest.mark.os_agnostic
def test_resolve_with_timeout_happy_path() -> None:
    """A generous timeout still resolves localhost normally."""
    records = list(resolve("localhost", timeout=5.0))
    assert records
    assert all(isinstance(r, DnsRecord) for r in records)


@pytest.mark.os_agnostic
def test_resolve_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lookup slower than the timeout raises NativeCallError (mentioning the timeout)."""

    def _slow_getaddrinfo(*_args: object, **_kwargs: object) -> list[object]:
        time.sleep(2.0)
        return []

    monkeypatch.setattr(dns_module.socket, "getaddrinfo", _slow_getaddrinfo)
    with pytest.raises(NativeCallError, match="timed out"):
        list(resolve("slow.example.", timeout=0.05))
