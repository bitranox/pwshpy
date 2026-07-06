"""Domain layer - pure business logic with no I/O or framework dependencies.

Contains the typed object model (records + enums), the lazy pipeline, and the
exception hierarchy that form the core of pwshpy.

Contents:
    * :mod:`.records` - Typed records (PSRecord base, ProcessInfo)
    * :mod:`.pipeline` - Lazy fluent pipeline over records
    * :mod:`.enums` - Domain enumerations (OutputFormat, DeployTarget, ProcessStatus)
    * :mod:`.errors` - Domain exception hierarchy (PwshPyError tree)
"""

from __future__ import annotations

from .enums import (
    ConnectionState,
    DeployTarget,
    OutputFormat,
    ProcessStatus,
    TransportProtocol,
)
from .errors import (
    ConfigurationError,
    FeatureUnavailableError,
    NativeCallError,
    PlatformUnsupportedError,
    PowerShellError,
    PwshPyError,
)
from .pipeline import Pipeline
from .records import NetConnection, ProcessInfo, PSRecord

__all__ = [
    # Records
    "PSRecord",
    "ProcessInfo",
    "NetConnection",
    # Pipeline
    "Pipeline",
    # Enums
    "ConnectionState",
    "DeployTarget",
    "OutputFormat",
    "ProcessStatus",
    "TransportProtocol",
    # Errors
    "PwshPyError",
    "NativeCallError",
    "PowerShellError",
    "FeatureUnavailableError",
    "PlatformUnsupportedError",
    "ConfigurationError",
]
