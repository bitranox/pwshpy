"""Shared marshaling seam for Tier-A native values → domain types.

One place converts foreign native representations into the domain object model,
so Tier-A adapters stay thin and the mapping is tested once.  Tier B has its own
PSObject marshaling; both feed the same records.

Contents:
    * :func:`epoch_to_datetime` — POSIX timestamp → aware UTC ``datetime``.
    * :func:`to_process_status` — raw status string → :class:`ProcessStatus`.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime

from ...domain.enums import AddressFamily, ConnectionState, ProcessStatus, TransportProtocol


def epoch_to_datetime(value: float | None) -> datetime | None:
    """Convert a POSIX timestamp to an aware UTC ``datetime`` (``None`` passes through).

    Example:
        >>> epoch_to_datetime(0)
        datetime.datetime(1970, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
        >>> epoch_to_datetime(None) is None
        True
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def to_process_status(raw: str | None) -> ProcessStatus:
    """Map a native status string to :class:`ProcessStatus`, falling back to ``UNKNOWN``.

    Example:
        >>> to_process_status("running") is ProcessStatus.RUNNING
        True
        >>> to_process_status("something-new") is ProcessStatus.UNKNOWN
        True
        >>> to_process_status(None) is ProcessStatus.UNKNOWN
        True
    """
    if raw is None:
        return ProcessStatus.UNKNOWN
    try:
        return ProcessStatus(raw)
    except ValueError:
        return ProcessStatus.UNKNOWN


def to_connection_state(raw: str | None) -> ConnectionState:
    """Map a native TCP-state string to :class:`ConnectionState` (fallback ``NONE``).

    Example:
        >>> to_connection_state("LISTEN") is ConnectionState.LISTEN
        True
        >>> to_connection_state("something-odd") is ConnectionState.NONE
        True
        >>> to_connection_state(None) is ConnectionState.NONE
        True
    """
    if raw is None:
        return ConnectionState.NONE
    try:
        return ConnectionState(raw)
    except ValueError:
        return ConnectionState.NONE


def to_transport_protocol(socket_type: int) -> TransportProtocol:
    """Map a socket type to :class:`TransportProtocol` (stream is TCP, else UDP).

    Example:
        >>> import socket
        >>> to_transport_protocol(socket.SOCK_STREAM) is TransportProtocol.TCP
        True
        >>> to_transport_protocol(socket.SOCK_DGRAM) is TransportProtocol.UDP
        True
    """
    return TransportProtocol.TCP if socket_type == socket.SOCK_STREAM else TransportProtocol.UDP


def to_address_family(family: int) -> AddressFamily:
    """Map a socket address family to :class:`AddressFamily` (fallback ``OTHER``).

    Example:
        >>> import socket
        >>> to_address_family(socket.AF_INET) is AddressFamily.IPV4
        True
        >>> to_address_family(socket.AF_INET6) is AddressFamily.IPV6
        True
        >>> to_address_family(socket.AF_UNSPEC) is AddressFamily.OTHER
        True
    """
    if family == socket.AF_INET:
        return AddressFamily.IPV4
    if family == socket.AF_INET6:
        return AddressFamily.IPV6
    return AddressFamily.OTHER


__all__ = [
    "epoch_to_datetime",
    "to_address_family",
    "to_connection_state",
    "to_process_status",
    "to_transport_protocol",
]
