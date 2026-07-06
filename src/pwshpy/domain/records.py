"""Typed domain records — the ``PSObject``-like object model.

Both tiers marshal into the **same** records so the :mod:`~pwshpy.domain.pipeline`
composes over either source.  :class:`PSRecord` is the Pydantic base; concrete
records (e.g. :class:`ProcessInfo`) add typed fields.

Records produced by trusted Tier-A native APIs are built with
:meth:`pydantic.BaseModel.model_construct` (validation skipped) on the hot path;
full validation is reserved for genuine external boundaries (Tier-B PSObject
marshaling and user-supplied data), per the strict-data rule.

Contents:
    * :class:`PSRecord` — frozen Pydantic base record with JSON helpers.
    * :class:`ProcessInfo` — a single operating-system process.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from .enums import AddressFamily, ConnectionState, ProcessStatus, TransportProtocol


class PSRecord(BaseModel):
    """Immutable base for every typed record flowing through the pipeline.

    Frozen so records behave as value objects.  ``to_dict`` yields a
    JSON-serializable mapping (``datetime`` → ISO string, enum → its value),
    ready for ``orjson`` or a rich table.

    Example:
        >>> class Point(PSRecord):
        ...     x: int
        ...     y: int
        >>> Point(x=1, y=2).to_dict()
        {'x': 1, 'y': 2}
    """

    model_config = ConfigDict(frozen=True)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this record's fields.

        Example:
            >>> class Tag(PSRecord):
            ...     name: str
            >>> Tag(name="a").to_dict()
            {'name': 'a'}
        """
        return self.model_dump(mode="json")


class ProcessInfo(PSRecord):
    """A single operating-system process (the Tier-A ``Get-Process`` record).

    Example:
        >>> proc = ProcessInfo(pid=1, ppid=0, name="init", status=ProcessStatus.SLEEPING)
        >>> proc.pid, proc.name, proc.status.value
        (1, 'init', 'sleeping')
        >>> proc.username is None
        True
    """

    pid: int
    ppid: int | None = None
    name: str
    status: ProcessStatus = ProcessStatus.UNKNOWN
    username: str | None = None
    memory_rss: int | None = None
    create_time: datetime | None = None


class NetConnection(PSRecord):
    """A single network connection (the Tier-A ``Get-NetTCPConnection`` record).

    Example:
        >>> conn = NetConnection(
        ...     protocol=TransportProtocol.TCP,
        ...     local_address="127.0.0.1",
        ...     local_port=8080,
        ...     status=ConnectionState.LISTEN,
        ... )
        >>> conn.local_port, conn.status.value, conn.remote_address
        (8080, 'LISTEN', None)
    """

    protocol: TransportProtocol
    local_address: str
    local_port: int
    remote_address: str | None = None
    remote_port: int | None = None
    status: ConnectionState = ConnectionState.NONE
    pid: int | None = None


class DiskUsage(PSRecord):
    """A mounted filesystem and its usage (the Tier-A ``Get-Volume`` record).

    Example:
        >>> disk = DiskUsage(
        ...     device="/dev/sda1", mountpoint="/", fstype="ext4",
        ...     total=100, used=40, free=60, percent=40.0,
        ... )
        >>> disk.mountpoint, disk.free, disk.percent
        ('/', 60, 40.0)
    """

    device: str
    mountpoint: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float


class SystemUptime(PSRecord):
    """System boot time and elapsed uptime (the Tier-A ``Get-Uptime`` record).

    Example:
        >>> from datetime import UTC, datetime
        >>> up = SystemUptime(boot_time=datetime(2020, 1, 1, tzinfo=UTC), uptime_seconds=3600.0)
        >>> up.uptime_seconds
        3600.0
    """

    boot_time: datetime
    uptime_seconds: float


class DnsRecord(PSRecord):
    """A resolved DNS address (the Tier-A ``Resolve-DnsName`` record).

    Example:
        >>> rec = DnsRecord(name="localhost", address="127.0.0.1", family=AddressFamily.IPV4)
        >>> rec.address, rec.family.value
        ('127.0.0.1', 'IPv4')
    """

    name: str
    address: str
    family: AddressFamily


class ConnectionTest(PSRecord):
    """The result of a TCP reachability probe (the Tier-A ``Test-Connection`` record).

    Example:
        >>> probe = ConnectionTest(host="localhost", port=80, reachable=False, error="refused")
        >>> probe.reachable, probe.latency_ms
        (False, None)
    """

    host: str
    port: int
    reachable: bool
    latency_ms: float | None = None
    error: str | None = None


class EnvVar(PSRecord):
    """An environment variable (the Tier-A ``env:`` drive record).

    Example:
        >>> EnvVar(name="PATH", value="/usr/bin").name
        'PATH'
    """

    name: str
    value: str


__all__ = [
    "ConnectionTest",
    "DiskUsage",
    "DnsRecord",
    "EnvVar",
    "NetConnection",
    "PSRecord",
    "ProcessInfo",
    "SystemUptime",
]
