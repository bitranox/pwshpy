"""native networking inventory over ``psutil`` (portable, cross-platform).

Typed facade over the untyped ``psutil`` surface (the ``# pyright: ignore`` for its
missing stubs is confined here).  Mirrors the read-only ``Get-NetAdapter`` /
``Get-NetIPAddress`` / ``Get-NetUDPEndpoint`` cmdlets, marshaling into typed records.

Contents:
    * :func:`iter_net_adapters` - one :class:`NetAdapter` per interface.
    * :func:`iter_net_ip_addresses` - one :class:`NetIpAddress` per bound IP.
    * :func:`iter_net_udp_endpoints` - one :class:`NetConnection` per UDP socket.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import psutil as _psutil_module  # pyright: ignore[reportMissingTypeStubs]

from ...domain.enums import AddressFamily, ConnectionState, TransportProtocol
from ...domain.records import NetAdapter, NetConnection, NetIpAddress

_psutil: Any = _psutil_module
_ACCESS_DENIED: type[BaseException] = _psutil.AccessDenied
_AF_LINK: Any = getattr(_psutil_module, "AF_LINK", None)


def iter_net_adapters() -> Iterator[NetAdapter]:
    """Yield a :class:`NetAdapter` for every network interface (like Get-NetAdapter).

    Example:
        >>> from pwshpy.domain.records import NetAdapter
        >>> all(isinstance(a, NetAdapter) for a in iter_net_adapters())
        True
    """
    stats = _psutil.net_if_stats()
    all_addrs = _psutil.net_if_addrs()
    for name, stat in stats.items():
        mac = ""
        for addr in all_addrs.get(name, []):
            if _AF_LINK is not None and addr.family == _AF_LINK:
                mac = str(addr.address)
                break
        yield NetAdapter.model_construct(
            name=str(name),
            is_up=bool(stat.isup),
            speed_mbps=int(stat.speed) or None,
            mtu=int(stat.mtu),
            mac_address=mac,
        )


def iter_net_ip_addresses() -> Iterator[NetIpAddress]:
    """Yield a :class:`NetIpAddress` for every IPv4/IPv6 address bound to an interface.

    Example:
        >>> from pwshpy.domain.records import NetIpAddress
        >>> all(isinstance(ip, NetIpAddress) for ip in iter_net_ip_addresses())
        True
    """
    for name, addr_list in _psutil.net_if_addrs().items():
        for addr in addr_list:
            if addr.family == socket.AF_INET:
                family = AddressFamily.IPV4
            elif addr.family == socket.AF_INET6:
                family = AddressFamily.IPV6
            else:
                continue  # link-layer / MAC addresses belong to get_net_adapter
            yield NetIpAddress.model_construct(
                interface=str(name),
                address=str(addr.address),
                family=family,
                netmask=str(addr.netmask or ""),
            )


def iter_net_udp_endpoints() -> Iterator[NetConnection]:
    """Yield a :class:`NetConnection` for every UDP socket (like Get-NetUDPEndpoint).

    UDP is connectionless, so ``status`` is always ``NONE`` and ``remote_*`` empty.
    On a platform that needs elevation to enumerate sockets (macOS non-root), a
    non-privileged caller yields nothing rather than erroring.

    Example:
        >>> from pwshpy.domain.records import NetConnection
        >>> all(isinstance(c, NetConnection) for c in iter_net_udp_endpoints())
        True
    """
    try:
        sockets = _psutil.net_connections(kind="udp")
    except (_ACCESS_DENIED, PermissionError):
        return
    for conn in sockets:
        local = conn.laddr
        yield NetConnection.model_construct(
            protocol=TransportProtocol.UDP,
            local_address=str(local.ip) if local else "",
            local_port=int(local.port) if local else 0,
            remote_address=None,
            remote_port=None,
            status=ConnectionState.NONE,
            pid=None if conn.pid is None else int(conn.pid),
        )


__all__ = ["iter_net_adapters", "iter_net_ip_addresses", "iter_net_udp_endpoints"]
