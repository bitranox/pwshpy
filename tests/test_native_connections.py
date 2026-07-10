"""native psutil network-connection source and its marshaling — portable."""

from __future__ import annotations

import socket
import sys

import pytest

from pwshpy.adapters.native import iter_connections
from pwshpy.adapters.native.marshal import to_connection_state, to_transport_protocol
from pwshpy.domain.enums import ConnectionState, TransportProtocol
from pwshpy.domain.records import NetConnection


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("LISTEN", ConnectionState.LISTEN),
        ("ESTABLISHED", ConnectionState.ESTABLISHED),
        ("TIME_WAIT", ConnectionState.TIME_WAIT),
    ],
)
def test_to_connection_state_maps_known(raw: str, expected: ConnectionState) -> None:
    """Known TCP states map to the matching enum member."""
    assert to_connection_state(raw) is expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize("raw", ["weird", "", None])
def test_to_connection_state_falls_back_to_none(raw: str | None) -> None:
    """Unknown or missing states fall back to NONE, never raising."""
    assert to_connection_state(raw) is ConnectionState.NONE


@pytest.mark.os_agnostic
def test_to_transport_protocol_distinguishes_tcp_udp() -> None:
    """Stream sockets are TCP; datagram sockets are UDP."""
    assert to_transport_protocol(socket.SOCK_STREAM) is TransportProtocol.TCP
    assert to_transport_protocol(socket.SOCK_DGRAM) is TransportProtocol.UDP


@pytest.mark.os_agnostic
def test_iter_connections_yields_net_connection_records() -> None:
    """Every yielded item is a typed NetConnection (may be empty in sandboxes)."""
    for conn in iter_connections():
        assert isinstance(conn, NetConnection)
        assert isinstance(conn.local_port, int)
        assert isinstance(conn.protocol, TransportProtocol)


@pytest.mark.os_agnostic
@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="psutil.net_connections needs root on macOS; a non-root runner enumerates no sockets",
)
def test_iter_connections_observes_a_listening_socket() -> None:
    """A socket we open as LISTEN is reflected in the enumeration."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        bound_port = srv.getsockname()[1]
        ports = {conn.local_port for conn in iter_connections() if conn.status is ConnectionState.LISTEN}
    assert bound_port in ports
