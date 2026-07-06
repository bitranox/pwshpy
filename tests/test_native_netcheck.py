"""Tier-A TCP reachability probe over stdlib socket — portable, hermetic."""

from __future__ import annotations

import socket

import pytest

from pwshpy.adapters.native import test_connection as probe_connection
from pwshpy.domain.records import ConnectionTest


@pytest.mark.os_agnostic
def test_probe_reaches_a_listening_socket() -> None:
    """A socket we open as LISTEN is reported reachable with a latency."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        probe = probe_connection("127.0.0.1", port=port, timeout=2.0)
    assert isinstance(probe, ConnectionTest)
    assert probe.reachable is True
    assert probe.latency_ms is not None
    assert probe.error is None


@pytest.mark.os_agnostic
def test_probe_reports_unreachable_without_raising() -> None:
    """A closed port yields reachable=False with an error, not an exception."""
    # Bind then close to obtain a port that is (almost certainly) now closed.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tmp:
        tmp.bind(("127.0.0.1", 0))
        closed_port = tmp.getsockname()[1]
    probe = probe_connection("127.0.0.1", port=closed_port, timeout=1.0)
    assert probe.reachable is False
    assert probe.latency_ms is None
    assert probe.error is not None
