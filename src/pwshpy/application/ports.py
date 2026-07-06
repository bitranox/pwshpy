"""Application ports — callable Protocol definitions for adapter functions.

Each Protocol class defines a ``__call__`` method whose signature exactly
matches the corresponding adapter function.  Existing module-level functions
satisfy these protocols automatically via structural subtyping (PEP 544).

System Role:
    Sits between domain and adapters.  Infrastructure types (``Config``)
    are imported under ``TYPE_CHECKING`` only so that import-linter layer
    contracts remain satisfied at runtime.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..domain.enums import DeployTarget, OutputFormat
from ..domain.records import (
    ConnectionTest,
    DiskUsage,
    DnsRecord,
    EnvVar,
    NetConnection,
    ProcessInfo,
    SystemUptime,
)

if TYPE_CHECKING:
    from lib_layered_config import Config


class ProcessSource(Protocol):
    """Enumerate the running processes as typed :class:`ProcessInfo` records (Tier A)."""

    def __call__(self) -> Iterator[ProcessInfo]: ...


class NetConnectionSource(Protocol):
    """Enumerate open sockets as typed :class:`NetConnection` records (Tier A)."""

    def __call__(self) -> Iterator[NetConnection]: ...


class DiskSource(Protocol):
    """Enumerate mounted filesystems as typed :class:`DiskUsage` records (Tier A)."""

    def __call__(self) -> Iterator[DiskUsage]: ...


class UptimeSource(Protocol):
    """Return system boot time + elapsed uptime as a :class:`SystemUptime` (Tier A)."""

    def __call__(self) -> SystemUptime: ...


class EnvironmentSource(Protocol):
    """Enumerate environment variables as typed :class:`EnvVar` records (Tier A)."""

    def __call__(self) -> Iterator[EnvVar]: ...


class DnsResolver(Protocol):
    """Resolve a host name to :class:`DnsRecord` addresses (Tier A)."""

    def __call__(self, name: str, *, timeout: float | None = ...) -> Iterator[DnsRecord]: ...


class ConnectionTester(Protocol):
    """Probe TCP reachability of a host, returning a :class:`ConnectionTest` (Tier A)."""

    def __call__(self, host: str, *, port: int = ..., timeout: float = ...) -> ConnectionTest: ...


class PowerShellRunner(Protocol):
    """Run a PowerShell script in the hosted engine and marshal the result (Tier B)."""

    def __call__(self, script: str, *, timeout: float | None = ...) -> list[Any]: ...


class GetConfig(Protocol):
    """Load layered configuration with application defaults."""

    def __call__(
        self, *, profile: str | None = ..., start_dir: str | None = ..., dotenv_path: str | None = ...
    ) -> Config: ...


class GetDefaultConfigPath(Protocol):
    """Return the path to the bundled default configuration file."""

    def __call__(self) -> Path: ...


class DeployConfiguration(Protocol):
    """Deploy default configuration to specified target layers."""

    def __call__(
        self,
        *,
        targets: Sequence[DeployTarget],
        force: bool = ...,
        profile: str | None = ...,
        set_permissions: bool = ...,
        dir_mode: int | None = ...,
        file_mode: int | None = ...,
    ) -> list[Path]: ...


class DisplayConfig(Protocol):
    """Display the provided configuration in the requested format."""

    def __call__(
        self, config: Config, *, output_format: OutputFormat = ..., section: str | None = ..., profile: str | None = ...
    ) -> None: ...


class InitLogging(Protocol):
    """Initialize lib_log_rich runtime with the provided configuration."""

    def __call__(self, config: Config) -> None: ...


__all__ = [
    "DeployConfiguration",
    "DisplayConfig",
    "GetConfig",
    "GetDefaultConfigPath",
    "ConnectionTester",
    "DiskSource",
    "DnsResolver",
    "EnvironmentSource",
    "InitLogging",
    "NetConnectionSource",
    "PowerShellRunner",
    "ProcessSource",
    "UptimeSource",
]
