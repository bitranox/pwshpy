"""Public package surface for pwshpy.

A genuinely Pythonic PowerShell: typed records and a fluent, lazy pipeline over
native Windows bindings (Tier A), with an in-process PowerShell 7.6 SDK fallback
(Tier B) — never a ``pwsh.exe`` subprocess.

Typical use::

    from pwshpy import ps
    for proc in ps.processes().where(lambda p: p.name == "python").take(5):
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
from .domain.enums import AddressFamily, ConnectionState, ProcessStatus, TransportProtocol
from .domain.errors import (
    ConfigurationError,
    FeatureUnavailableError,
    NativeCallError,
    PlatformUnsupportedError,
    PowerShellError,
    PwshPyError,
)
from .domain.pipeline import Pipeline
from .domain.records import (
    ConnectionTest,
    DiskUsage,
    DnsRecord,
    EnvVar,
    NetConnection,
    ProcessInfo,
    PSRecord,
    SystemUptime,
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
    "NetConnection",
    "DiskUsage",
    "SystemUptime",
    "DnsRecord",
    "ConnectionTest",
    "EnvVar",
    "ProcessStatus",
    "ConnectionState",
    "TransportProtocol",
    "AddressFamily",
    # Errors
    "PwshPyError",
    "NativeCallError",
    "PowerShellError",
    "FeatureUnavailableError",
    "PlatformUnsupportedError",
    "ConfigurationError",
    # Config + metadata
    "get_config",
    "print_info",
]
