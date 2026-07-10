"""native network-connection source over ``psutil`` (portable, cross-platform).

Typed facade over the untyped ``psutil`` surface — the single ``# pyright: ignore``
for its missing stubs is confined here.  Mirrors ``Get-NetTCPConnection``: each
socket marshals into a :class:`NetConnection` via ``model_construct`` on the hot
path.

Contents:
    * :func:`iter_connections` — yield one :class:`NetConnection` per open socket.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psutil as _psutil_module  # pyright: ignore[reportMissingTypeStubs]

from ...domain.records import NetConnection
from .marshal import to_connection_state, to_transport_protocol

# Funnel the untyped module through an explicit ``Any`` alias (see psutil_process).
_psutil: Any = _psutil_module
# Enumerating every socket needs elevation on some platforms (macOS as a non-root user); the
# exception type is not visible through the Any funnel, so pin it here for a typed ``except``.
_ACCESS_DENIED: type[BaseException] = _psutil.AccessDenied


def _endpoint(addr: Any) -> tuple[str | None, int | None]:
    """Split a psutil ``addr`` (``addr(ip, port)`` or empty tuple) into (ip, port)."""
    if not addr:
        return None, None
    return str(addr.ip), int(addr.port)


def _to_net_connection(conn: Any) -> NetConnection:
    """Marshal one psutil ``sconn`` tuple into a :class:`NetConnection`."""
    local_ip, local_port = _endpoint(conn.laddr)
    remote_ip, remote_port = _endpoint(conn.raddr)
    return NetConnection.model_construct(
        protocol=to_transport_protocol(conn.type),
        local_address=local_ip or "",
        local_port=local_port or 0,
        remote_address=remote_ip,
        remote_port=remote_port,
        status=to_connection_state(conn.status),
        pid=None if conn.pid is None else int(conn.pid),
    )


def iter_connections() -> Iterator[NetConnection]:
    """Yield a :class:`NetConnection` for every open inet socket visible to the caller.

    Some sockets owned by other users expose no PID without elevation; those
    records simply carry ``pid=None`` rather than being dropped.  On a platform
    that requires elevation to enumerate all sockets (macOS as a non-root user), a
    non-privileged caller yields no connections rather than erroring.

    Example:
        >>> from pwshpy.domain.records import NetConnection
        >>> all(isinstance(c, NetConnection) for c in iter_connections())
        True
    """
    try:
        connections = _psutil.net_connections(kind="inet")
    except (_ACCESS_DENIED, PermissionError):
        return
    for conn in connections:
        yield _to_net_connection(conn)


__all__ = ["iter_connections"]
