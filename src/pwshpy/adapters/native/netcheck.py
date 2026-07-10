"""native TCP reachability probe over the stdlib ``socket`` (portable).

Mirrors ``Test-Connection`` (TCP-connect form): attempts a bounded connection to
``host:port`` and returns a :class:`ConnectionTest` describing the outcome — it
never raises for an unreachable target, it reports ``reachable=False`` with the
reason.  Honors the design's timeout requirement for blocking native calls.

Contents:
    * :func:`test_connection` — probe one ``host:port`` and return the result.
"""

from __future__ import annotations

import socket
import time

from ...domain.records import ConnectionTest


def test_connection(host: str, *, port: int = 443, timeout: float = 5.0) -> ConnectionTest:
    """Probe TCP reachability of ``host:port`` within ``timeout`` seconds.

    Args:
        host: Target host name or address.
        port: Target TCP port.
        timeout: Maximum seconds to wait for the connection.

    Example:
        >>> import socket
        >>> with socket.socket() as srv:
        ...     srv.bind(("127.0.0.1", 0))
        ...     srv.listen()
        ...     probe = test_connection("127.0.0.1", port=srv.getsockname()[1])
        >>> probe.reachable
        True
        >>> probe.latency_ms is not None
        True
    """
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=max(timeout, 0.0)):
            latency_ms = (time.perf_counter() - start) * 1000.0
    except OSError as exc:
        return ConnectionTest.model_construct(host=host, port=port, reachable=False, latency_ms=None, error=str(exc))
    return ConnectionTest.model_construct(host=host, port=port, reachable=True, latency_ms=latency_ms, error=None)


__all__ = ["test_connection"]
