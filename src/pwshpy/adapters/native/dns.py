"""Tier-A DNS resolver over the stdlib ``socket`` (portable, cross-platform).

Mirrors ``Resolve-DnsName``: resolves a host name to its addresses and marshals
each into a :class:`DnsRecord`.  ``socket`` is fully typed, so no pyright
suppression is needed here; the only boundary concern is mapping the native
``gaierror`` into the typed :class:`NativeCallError`.

``socket.getaddrinfo`` cannot be cancelled, so an optional ``timeout`` is enforced
by running the lookup on a daemon thread and abandoning it on expiry (the honored
design requirement that blocking native calls take a timeout).

Contents:
    * :func:`resolve` - yield one :class:`DnsRecord` per resolved address.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from typing import Any

from ...domain.errors import NativeCallError
from ...domain.records import DnsRecord
from .marshal import to_address_family


def _resolve_addrinfo(name: str, timeout: float | None) -> list[Any]:
    """Return ``getaddrinfo`` results for ``name``, bounded by ``timeout`` if given.

    Raises:
        NativeCallError: On resolution failure (wraps ``socket.gaierror``) or when
            ``timeout`` elapses before the lookup completes.
    """
    if timeout is None:
        try:
            return socket.getaddrinfo(name, None)
        except socket.gaierror as exc:
            raise NativeCallError(f"DNS resolution failed for {name!r}: {exc}") from exc

    results: list[Any] = []
    errors: list[OSError] = []

    def _worker() -> None:
        try:
            results.append(socket.getaddrinfo(name, None))
        except OSError as exc:  # gaierror is an OSError subclass
            errors.append(exc)

    thread = threading.Thread(target=_worker, name=f"pwshpy-dns-{name}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise NativeCallError(f"DNS resolution for {name!r} timed out after {timeout}s")
    if errors:
        raise NativeCallError(f"DNS resolution failed for {name!r}: {errors[0]}") from errors[0]
    return results[0]


def resolve(name: str, *, timeout: float | None = None) -> Iterator[DnsRecord]:
    """Yield a :class:`DnsRecord` for each unique address ``name`` resolves to.

    Args:
        name: The host name to resolve.
        timeout: Optional seconds to wait for resolution. ``None`` uses the OS
            resolver's own timeout.

    Raises:
        NativeCallError: If the name cannot be resolved or ``timeout`` elapses.

    Example:
        >>> records = list(resolve("localhost", timeout=5.0))
        >>> all(isinstance(r, DnsRecord) for r in records)
        True
        >>> any(r.address in {"127.0.0.1", "::1"} for r in records)
        True
    """
    infos = _resolve_addrinfo(name, timeout)
    seen: set[tuple[int, str]] = set()
    for family, _type, _proto, _canonname, sockaddr in infos:
        address = str(sockaddr[0])
        key = (int(family), address)
        if key in seen:
            continue
        seen.add(key)
        yield DnsRecord.model_construct(name=name, address=address, family=to_address_family(int(family)))


__all__ = ["resolve"]
