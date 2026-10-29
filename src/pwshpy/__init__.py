"""Public package surface for pwshpy.

A genuinely Pythonic PowerShell: typed records and a fluent, lazy pipeline over
native OS bindings (win32 on Windows, the honest Linux backends on Linux), with an
in-process PowerShell 7.6 SDK fallback (.NET) - never a ``pwsh.exe`` subprocess.

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
    ImageArchitecture,
    IntegrityLevel,
    KnownFolder,
    ProcessStatus,
    RegistryHive,
    RegistryValueType,
    RunnerFormat,
    ServiceKind,
    ServiceStartType,
    ServiceState,
    TaskLogonType,
    TaskMultipleInstances,
    TaskRunLevel,
    TaskState,
    TransportProtocol,
    WellKnownSid,
)
from .domain.errors import (
    ConfigurationError,
    ElevationDeclinedError,
    ElevationRequiredError,
    FeatureUnavailableError,
    NativeAccessDeniedError,
    NativeCallError,
    OperationTimeoutError,
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
    CurrentIdentity,
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
    ShortcutInfo,
    SystemUptime,
    UpdateCategory,
    WebResponse,
    WindowsImage,
    WindowsUpdate,
)
from .domain.scheduling import (
    BootTrigger,
    DailyTrigger,
    LogonTrigger,
    OnceTrigger,
    TaskPrincipal,
    TaskSettings,
    TaskTrigger,
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
    "UpdateCategory",
    "WebResponse",
    "WindowsImage",
    "WindowsUpdate",
    "DnsRecord",
    "ComputerInfo",
    "CurrentIdentity",
    "ConnectionTest",
    "Credential",
    "FileSystemItem",
    "Hotfix",
    "EnvVar",
    "RegistryKey",
    "RegistryValue",
    "ServiceInfo",
    "ShortcutInfo",
    "EventLogEntry",
    "CimInstance",
    "CommandInfo",
    "CommandParameter",
    "ScheduledTaskInfo",
    "TaskPrincipal",
    "TaskSettings",
    "TaskTrigger",
    "OnceTrigger",
    "DailyTrigger",
    "BootTrigger",
    "LogonTrigger",
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
    "TaskRunLevel",
    "TaskLogonType",
    "TaskMultipleInstances",
    "ImageArchitecture",
    "IntegrityLevel",
    "KnownFolder",
    "WellKnownSid",
    "AceType",
    # Errors
    "PwshPyError",
    "NativeAccessDeniedError",
    "NativeCallError",
    "ElevationDeclinedError",
    "OperationTimeoutError",
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
