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

from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..domain.enums import AceType, DeployTarget, FileItemType, OutputFormat, RegistryValueType, ServiceStartType
from ..domain.records import (
    AclEntry,
    CimInstance,
    CommandInfo,
    ComputerInfo,
    ConnectionTest,
    Credential,
    DiskUsage,
    DnsRecord,
    EnvVar,
    EventLogEntry,
    FileSystemItem,
    Hotfix,
    LocalGroup,
    LocalUser,
    NetAdapter,
    NetConnection,
    NetIpAddress,
    ProcessInfo,
    ProcessResult,
    PSInvocationResult,
    PSRecord,
    RegistryKey,
    RegistryValue,
    ScheduledTaskInfo,
    ServiceInfo,
    SystemUptime,
    WebResponse,
)

if TYPE_CHECKING:
    from lib_layered_config import Config


class ProcessSource(Protocol):
    """Enumerate the running processes as typed :class:`ProcessInfo` records (native)."""

    def __call__(self) -> Iterator[ProcessInfo]: ...


class NetConnectionSource(Protocol):
    """Enumerate open sockets as typed :class:`NetConnection` records (native)."""

    def __call__(self) -> Iterator[NetConnection]: ...


class NetAdapterSource(Protocol):
    """Enumerate network interfaces as typed :class:`NetAdapter` records (native)."""

    def __call__(self) -> Iterator[NetAdapter]: ...


class NetIpAddressSource(Protocol):
    """Enumerate bound IP addresses as typed :class:`NetIpAddress` records (native)."""

    def __call__(self) -> Iterator[NetIpAddress]: ...


class NetUdpEndpointSource(Protocol):
    """Enumerate UDP sockets as typed :class:`NetConnection` records (native)."""

    def __call__(self) -> Iterator[NetConnection]: ...


class DiskSource(Protocol):
    """Enumerate mounted filesystems as typed :class:`DiskUsage` records (native)."""

    def __call__(self) -> Iterator[DiskUsage]: ...


class UptimeSource(Protocol):
    """Return system boot time + elapsed uptime as a :class:`SystemUptime` (native)."""

    def __call__(self) -> SystemUptime: ...


class EnvironmentSource(Protocol):
    """Enumerate environment variables as typed :class:`EnvVar` records (native)."""

    def __call__(self) -> Iterator[EnvVar]: ...


class DnsResolver(Protocol):
    """Resolve a host name to :class:`DnsRecord` addresses (native)."""

    def __call__(self, name: str, *, timeout: float | None = ...) -> Iterator[DnsRecord]: ...


class ConnectionTester(Protocol):
    """Probe TCP reachability of a host, returning a :class:`ConnectionTest` (native)."""

    def __call__(self, host: str, *, port: int = ..., timeout: float = ...) -> ConnectionTest: ...


class ProcessRunner(Protocol):
    """Run an external program from an argv list, returning a typed :class:`ProcessResult` (native)."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = ...,
        timeout: float | None = ...,
        env: Mapping[str, str] | None = ...,
        input_text: str | None = ...,
    ) -> ProcessResult: ...


class CredentialPrompter(Protocol):
    """Interactively read a credential without echoing the password (native, portable)."""

    def __call__(self, username: str | None = ..., *, prompt: str = ..., target: str = ...) -> Credential: ...


class CredentialStore(Protocol):
    """Persist/read/remove credentials in the OS credential vault (native, **mutating**)."""

    def save(self, target: str, username: str, secret: str) -> Credential: ...
    def load(self, target: str) -> Credential | None: ...
    def delete(self, target: str) -> None: ...


class FileSystem(Protocol):
    """File/directory operations as typed records (native, portable; read + **mutating**)."""

    def get_child_item(self, path: str, *, recurse: bool = ...) -> Iterator[FileSystemItem]: ...
    def get_item(self, path: str) -> FileSystemItem: ...
    def get_content(self, path: str, *, encoding: str = ...) -> str: ...
    def get_content_lines(self, path: str, *, encoding: str = ...) -> Iterator[str]: ...
    def test_path(self, path: str) -> bool: ...
    def new_item(self, path: str, *, item_type: FileItemType = ...) -> FileSystemItem: ...
    def copy_item(self, source: str, destination: str, *, recurse: bool = ...) -> None: ...
    def move_item(self, source: str, destination: str) -> None: ...
    def remove_item(self, path: str, *, recurse: bool = ...) -> None: ...


class TextWriter(Protocol):
    """Write text to a file with a predictable encoding/BOM/newline (native, UTF-8-no-BOM default)."""

    def __call__(
        self, path: str | Path, text: str, *, encoding: str = ..., newline: str = ..., bom: bool = ...
    ) -> Path: ...


class RecordWriter(Protocol):
    """Write typed records to a file as UTF-8 JSON/JSONL, never a BOM (native)."""

    def __call__(self, path: str | Path, records: Iterable[PSRecord], *, jsonl: bool = ...) -> Path: ...


class TextStreamWriter(Protocol):
    """Stream text chunks to a file with a predictable encoding, memory-bounded (native)."""

    def __call__(
        self, path: str | Path, chunks: Iterable[str], *, encoding: str = ..., newline: str = ..., bom: bool = ...
    ) -> Path: ...


class WebRequester(Protocol):
    """Perform an HTTP request, returning a typed :class:`WebResponse` (native, portable)."""

    def __call__(
        self,
        url: str,
        *,
        method: str = ...,
        headers: Mapping[str, str] | None = ...,
        body: str | None = ...,
        timeout: float = ...,
    ) -> WebResponse: ...


class RestInvoker(Protocol):
    """Perform an HTTP request and return the parsed JSON (native, portable)."""

    def __call__(
        self,
        url: str,
        *,
        method: str = ...,
        headers: Mapping[str, str] | None = ...,
        json_body: Any = ...,
        timeout: float = ...,
    ) -> Any: ...


class FileDownloader(Protocol):
    """Stream an HTTP download to a file, memory-bounded (native, portable)."""

    def __call__(self, url: str, dest: str | Path, *, chunk_size: int = ..., timeout: float = ...) -> Path: ...


class ProcessControl(Protocol):
    """Stop/wait for processes and restart/stop the computer (native, **mutating**)."""

    def stop_process(self, pid: int, *, force: bool = ...) -> None: ...
    def wait_process(self, pid: int, *, timeout: float | None = ...) -> int | None: ...
    def restart_computer(self, *, delay_seconds: int = ..., force: bool = ...) -> None: ...
    def stop_computer(self, *, delay_seconds: int = ..., force: bool = ...) -> None: ...


class HotfixSource(Protocol):
    """Enumerate installed Windows updates as typed :class:`Hotfix` records (native)."""

    def __call__(self) -> Iterator[Hotfix]: ...


class ComputerInfoSource(Protocol):
    """Return a :class:`ComputerInfo` summary of the local machine (native, portable)."""

    def __call__(self) -> ComputerInfo: ...


class ElevationCheck(Protocol):
    """Report whether the current process has administrative (elevated) rights (native)."""

    def __call__(self) -> bool: ...


class Elevator(Protocol):
    """Relaunch the current process elevated, forwarding argv/cwd (native, Windows UAC)."""

    def __call__(
        self,
        argv: Sequence[str] | None = ...,
        *,
        executable: str | None = ...,
        cwd: str | None = ...,
        wait: bool = ...,
    ) -> int | None: ...


class RegistryValueSource(Protocol):
    """Enumerate the values under a registry key as :class:`RegistryValue` records (native)."""

    def __call__(self, key: str) -> Iterator[RegistryValue]: ...


class RegistryKeySource(Protocol):
    """Enumerate the immediate subkeys of a registry key as :class:`RegistryKey` records (native)."""

    def __call__(self, key: str) -> Iterator[RegistryKey]: ...


class ServiceSource(Protocol):
    """Enumerate the Windows services as typed :class:`ServiceInfo` records (native)."""

    def __call__(self) -> Iterator[ServiceInfo]: ...


class ServiceController(Protocol):
    """Start/stop/restart Windows services and change their startup type (native, **mutating**)."""

    def start(self, name: str, *, timeout: float = ...) -> ServiceInfo: ...
    def stop(self, name: str, *, timeout: float = ...) -> ServiceInfo: ...
    def restart(self, name: str, *, timeout: float = ...) -> ServiceInfo: ...
    def set_startup(self, name: str, start_type: ServiceStartType) -> ServiceInfo: ...


class RegistryController(Protocol):
    """Set/remove registry values and create/remove registry keys (native, **mutating**)."""

    def set_value(
        self, key: str, name: str, data: str | int | list[str] | None, value_type: RegistryValueType
    ) -> RegistryValue: ...
    def remove_value(self, key: str, name: str) -> None: ...
    def create_key(self, key: str) -> RegistryKey: ...
    def remove_key(self, key: str, *, recursive: bool = ...) -> None: ...


class EventLogSource(Protocol):
    """Stream the entries of a Windows event log as :class:`EventLogEntry` records (native)."""

    def __call__(self, log_name: str) -> Iterator[EventLogEntry]: ...


class EventLogController(Protocol):
    """Clear a Windows event log (native, **mutating**)."""

    def clear(self, log_name: str, *, backup_path: str | None = ...) -> None: ...


class CimSource(Protocol):
    """Stream CIM/WMI instances of a class as :class:`CimInstance` records (native)."""

    def __call__(
        self, class_name: str, *, where: str | None = None, namespace: str = "root/cimv2"
    ) -> Iterator[CimInstance]: ...


class ScheduledTaskSource(Protocol):
    """Stream Windows scheduled tasks as :class:`ScheduledTaskInfo` records (native)."""

    def __call__(self, folder_path: str = "\\") -> Iterator[ScheduledTaskInfo]: ...


class ScheduledTaskController(Protocol):
    """Register/unregister, enable/disable and run/stop scheduled tasks (native, **mutating**)."""

    def enable(self, task_path: str) -> ScheduledTaskInfo: ...
    def disable(self, task_path: str) -> ScheduledTaskInfo: ...
    def run(self, task_path: str) -> None: ...
    def stop(self, task_path: str) -> None: ...
    def unregister(self, task_path: str) -> None: ...
    def register(
        self, task_path: str, *, program: str, arguments: str = ..., description: str = ...
    ) -> ScheduledTaskInfo: ...


class LocalUserSource(Protocol):
    """Enumerate the local Windows user accounts as :class:`LocalUser` records (native)."""

    def __call__(self) -> Iterator[LocalUser]: ...


class LocalGroupSource(Protocol):
    """Enumerate the local Windows groups as :class:`LocalGroup` records (native)."""

    def __call__(self) -> Iterator[LocalGroup]: ...


class LocalAccountController(Protocol):
    """Create/remove local users + groups, manage membership, enable/disable (native, **mutating**)."""

    def new_user(
        self,
        name: str,
        *,
        password: str = ...,
        full_name: str = ...,
        description: str = ...,
        disabled: bool = ...,
        timeout: float = ...,
    ) -> LocalUser: ...
    def remove_user(self, name: str, *, timeout: float = ...) -> None: ...
    def set_user_enabled(self, name: str, *, enabled: bool, timeout: float = ...) -> LocalUser: ...
    def new_group(self, name: str, *, description: str = ..., timeout: float = ...) -> LocalGroup: ...
    def remove_group(self, name: str, *, timeout: float = ...) -> None: ...
    def add_group_member(self, group: str, member: str, *, timeout: float = ...) -> None: ...
    def remove_group_member(self, group: str, member: str, *, timeout: float = ...) -> None: ...


class AclSource(Protocol):
    """Stream the DACL entries of a filesystem path as :class:`AclEntry` records (native)."""

    def __call__(self, path: str) -> Iterator[AclEntry]: ...


class AclController(Protocol):
    """Add/remove DACL entries and set the owner of a filesystem path (native, **mutating**)."""

    def add_ace(self, path: str, trustee: str, rights: int, *, access_type: AceType = ...) -> None: ...
    def remove_ace(self, path: str, trustee: str, *, access_type: AceType | None = ...) -> None: ...
    def set_owner(self, path: str, owner: str) -> None: ...


class PowerShellRunner(Protocol):
    """Run a PowerShell script in the hosted engine and marshal the result (.NET)."""

    def __call__(self, script: str, *, timeout: float | None = ...) -> list[Any]: ...


class CmdletRunner(Protocol):
    """Run a single cmdlet with safely bound parameters, returning the full result (.NET)."""

    def __call__(self, name: str, *args: Any, timeout: float | None = ..., **params: Any) -> PSInvocationResult: ...


class CommandIntrospector(Protocol):
    """Introspect a command's parameter metadata as a typed :class:`CommandInfo` (.NET)."""

    def __call__(self, name: str) -> CommandInfo: ...


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
    "AclController",
    "AclSource",
    "CmdletRunner",
    "CommandIntrospector",
    "ComputerInfoSource",
    "CredentialPrompter",
    "CredentialStore",
    "FileDownloader",
    "HotfixSource",
    "ProcessControl",
    "DeployConfiguration",
    "FileSystem",
    "ElevationCheck",
    "Elevator",
    "DisplayConfig",
    "GetConfig",
    "GetDefaultConfigPath",
    "ConnectionTester",
    "DiskSource",
    "DnsResolver",
    "EnvironmentSource",
    "EventLogController",
    "InitLogging",
    "LocalAccountController",
    "NetAdapterSource",
    "NetConnectionSource",
    "NetIpAddressSource",
    "NetUdpEndpointSource",
    "PowerShellRunner",
    "ProcessRunner",
    "RecordWriter",
    "RestInvoker",
    "TextStreamWriter",
    "TextWriter",
    "WebRequester",
    "CimSource",
    "EventLogSource",
    "LocalGroupSource",
    "LocalUserSource",
    "ProcessSource",
    "RegistryController",
    "RegistryKeySource",
    "RegistryValueSource",
    "ScheduledTaskController",
    "ScheduledTaskSource",
    "ServiceController",
    "ServiceSource",
    "UptimeSource",
]
