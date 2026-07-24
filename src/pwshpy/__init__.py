"""Public package surface for pwshpy.

A genuinely Pythonic PowerShell: typed records and a fluent, lazy pipeline over
native Windows bindings (native), with an in-process PowerShell 7.6 SDK fallback
(.NET) — never a ``pwsh.exe`` subprocess.

Typical use::

    from pwshpy import ps
    for proc in ps.get_process().where(lambda p: p.name == "python").take(5):
        print(proc.pid, proc.name)

This module re-exports the stable public API, routing through the proper
architectural layers (domain records/pipeline/errors, the wired ``ps`` facade,
and package metadata).
"""

from __future__ import annotations

# Metadata
from .__init__conf__ import print_info

# Composition exports (wired adapters + the ps facade)
from .composition import Ps, build_ps, get_config, ps

# Domain object model
from .domain.enums import (
    AceType,
    AddressFamily,
    ConnectionState,
    EventLevel,
    ProcessStatus,
    RegistryHive,
    RegistryValueType,
    RunnerFormat,
    ServiceKind,
    ServiceStartType,
    ServiceState,
    TaskState,
    TransportProtocol,
    WellKnownSid,
)
from .domain.errors import (
    ConfigurationError,
    ElevationRequiredError,
    FeatureUnavailableError,
    NativeCallError,
    PackError,
    PlatformUnsupportedError,
    PowerShellError,
    PwshPyError,
)
from .domain.packing import PackOptions
from .domain.pipeline import Pipeline
from .domain.records import (
    AclEntry,
    CimInstance,
    CommandInfo,
    CommandParameter,
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
    PackedScript,
    ProcessInfo,
    ProcessResult,
    PSInvocationResult,
    PSObjectRecord,
    PSRecord,
    RegistryKey,
    RegistryValue,
    ScheduledTaskInfo,
    ServiceInfo,
    SystemUptime,
    WebResponse,
)

__all__ = [
    # Facade
    "ps",
    "Ps",
    "build_ps",
    # Object model
    "Pipeline",
    "PSRecord",
    "ProcessInfo",
    "PackOptions",
    "RunnerFormat",
    "PackedScript",
    "ProcessResult",
    "NetAdapter",
    "NetConnection",
    "NetIpAddress",
    "DiskUsage",
    "SystemUptime",
    "WebResponse",
    "DnsRecord",
    "ComputerInfo",
    "ConnectionTest",
    "Credential",
    "FileSystemItem",
    "Hotfix",
    "EnvVar",
    "RegistryKey",
    "RegistryValue",
    "ServiceInfo",
    "EventLogEntry",
    "CimInstance",
    "CommandInfo",
    "CommandParameter",
    "ScheduledTaskInfo",
    "LocalUser",
    "LocalGroup",
    "AclEntry",
    "PSObjectRecord",
    "PSInvocationResult",
    "ProcessStatus",
    "ConnectionState",
    "TransportProtocol",
    "AddressFamily",
    "RegistryHive",
    "RegistryValueType",
    "ServiceState",
    "ServiceStartType",
    "ServiceKind",
    "EventLevel",
    "TaskState",
    "WellKnownSid",
    "AceType",
    # Errors
    "PwshPyError",
    "NativeCallError",
    "PowerShellError",
    "FeatureUnavailableError",
    "PlatformUnsupportedError",
    "ElevationRequiredError",
    "ConfigurationError",
    "PackError",
    # Config + metadata
    "get_config",
    "print_info",
]
