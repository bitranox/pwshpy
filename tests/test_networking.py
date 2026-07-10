"""native networking inventory: os_agnostic tests against real psutil.

Loopback always exists, so the assertions are deterministic on every OS. UDP
enumeration can legitimately yield nothing under a non-root macOS session; the
test only asserts type/protocol for whatever it does yield.
"""

from __future__ import annotations

import pytest

from pwshpy.adapters.native.networking import (
    iter_net_adapters,
    iter_net_ip_addresses,
    iter_net_udp_endpoints,
)
from pwshpy.domain.enums import TransportProtocol
from pwshpy.domain.records import NetAdapter, NetConnection, NetIpAddress


@pytest.mark.os_agnostic
def test_net_adapters_yield_typed_records() -> None:
    """Every interface marshals into a NetAdapter with a bool is_up."""
    adapters = list(iter_net_adapters())
    assert adapters, "a host always has at least a loopback interface"
    assert all(isinstance(a, NetAdapter) for a in adapters)
    assert all(isinstance(a.is_up, bool) for a in adapters)


@pytest.mark.os_agnostic
def test_net_ip_addresses_include_loopback() -> None:
    """The loopback IP (127.0.0.1 or ::1) is always bound and typed."""
    addresses = list(iter_net_ip_addresses())
    assert all(isinstance(a, NetIpAddress) for a in addresses)
    assert any(a.address in ("127.0.0.1", "::1") for a in addresses)


@pytest.mark.os_agnostic
def test_net_udp_endpoints_are_udp_records() -> None:
    """Whatever UDP sockets are visible marshal into UDP NetConnection records."""
    for conn in iter_net_udp_endpoints():
        assert isinstance(conn, NetConnection)
        assert conn.protocol is TransportProtocol.UDP
