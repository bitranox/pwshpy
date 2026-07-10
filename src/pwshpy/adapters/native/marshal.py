"""Shared marshaling seam for native native values → domain types.

One place converts foreign native representations into the domain object model,
so native adapters stay thin and the mapping is tested once.  .NET has its own
PSObject marshaling; both feed the same records.

Contents:
    * :func:`epoch_to_datetime` — POSIX timestamp → aware UTC ``datetime``.
    * :func:`to_process_status` — raw status string → :class:`ProcessStatus`.
"""

from __future__ import annotations

import re
import socket
from datetime import datetime, timezone

from ...domain.enums import (
    AceType,
    AddressFamily,
    ConnectionState,
    EventLevel,
    ProcessStatus,
    RegistryHive,
    RegistryValueType,
    ServiceKind,
    ServiceStartType,
    ServiceState,
    TaskState,
    TransportProtocol,
)


def epoch_to_datetime(value: float | None) -> datetime | None:
    """Convert a POSIX timestamp to an aware UTC ``datetime`` (``None`` passes through).

    Example:
        >>> epoch_to_datetime(0)
        datetime.datetime(1970, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
        >>> epoch_to_datetime(None) is None
        True
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def to_process_status(raw: str | None) -> ProcessStatus:
    """Map a native status string to :class:`ProcessStatus`, falling back to ``UNKNOWN``.

    Example:
        >>> to_process_status("running") is ProcessStatus.RUNNING
        True
        >>> to_process_status("something-new") is ProcessStatus.UNKNOWN
        True
        >>> to_process_status(None) is ProcessStatus.UNKNOWN
        True
    """
    if raw is None:
        return ProcessStatus.UNKNOWN
    try:
        return ProcessStatus(raw)
    except ValueError:
        return ProcessStatus.UNKNOWN


def to_process_name(raw: str | None, *, strip_exe: bool) -> str:
    """Normalize a psutil process name toward ``Get-Process`` semantics.

    psutil reports the executable file name (``python.exe`` on Windows); the
    PowerShell ``Get-Process`` reference reports the base name (``python``).
    When ``strip_exe`` is set (the adapter passes ``sys.platform == 'win32'``) a
    single trailing ``.exe`` is removed so pwshpy matches Get-Process; otherwise
    the name is returned verbatim (a Wine ``foo.exe`` on Linux keeps its suffix).

    Example:
        >>> to_process_name("python.exe", strip_exe=True)
        'python'
        >>> to_process_name("python.exe", strip_exe=False)
        'python.exe'
        >>> to_process_name(None, strip_exe=True)
        ''
    """
    name = raw or ""
    if strip_exe and name.lower().endswith(".exe"):
        return name[:-4]
    return name


def to_connection_state(raw: str | None) -> ConnectionState:
    """Map a native TCP-state string to :class:`ConnectionState` (fallback ``NONE``).

    Example:
        >>> to_connection_state("LISTEN") is ConnectionState.LISTEN
        True
        >>> to_connection_state("something-odd") is ConnectionState.NONE
        True
        >>> to_connection_state(None) is ConnectionState.NONE
        True
    """
    if raw is None:
        return ConnectionState.NONE
    try:
        return ConnectionState(raw)
    except ValueError:
        return ConnectionState.NONE


def to_transport_protocol(socket_type: int) -> TransportProtocol:
    """Map a socket type to :class:`TransportProtocol` (stream is TCP, else UDP).

    Example:
        >>> import socket
        >>> to_transport_protocol(socket.SOCK_STREAM) is TransportProtocol.TCP
        True
        >>> to_transport_protocol(socket.SOCK_DGRAM) is TransportProtocol.UDP
        True
    """
    return TransportProtocol.TCP if socket_type == socket.SOCK_STREAM else TransportProtocol.UDP


def to_address_family(family: int) -> AddressFamily:
    """Map a socket address family to :class:`AddressFamily` (fallback ``OTHER``).

    Example:
        >>> import socket
        >>> to_address_family(socket.AF_INET) is AddressFamily.IPV4
        True
        >>> to_address_family(socket.AF_INET6) is AddressFamily.IPV6
        True
        >>> to_address_family(socket.AF_UNSPEC) is AddressFamily.OTHER
        True
    """
    if family == socket.AF_INET:
        return AddressFamily.IPV4
    if family == socket.AF_INET6:
        return AddressFamily.IPV6
    return AddressFamily.OTHER


# Native win32 REG_* type ids -> RegistryValueType (see winreg REG_* constants).
_REGISTRY_TYPE_BY_INT: dict[int, RegistryValueType] = {
    0: RegistryValueType.REG_NONE,
    1: RegistryValueType.REG_SZ,
    2: RegistryValueType.REG_EXPAND_SZ,
    3: RegistryValueType.REG_BINARY,
    4: RegistryValueType.REG_DWORD,
    5: RegistryValueType.REG_DWORD_BIG_ENDIAN,
    6: RegistryValueType.REG_LINK,
    7: RegistryValueType.REG_MULTI_SZ,
    8: RegistryValueType.REG_RESOURCE_LIST,
    9: RegistryValueType.REG_FULL_RESOURCE_DESCRIPTOR,
    10: RegistryValueType.REG_RESOURCE_REQUIREMENTS_LIST,
    11: RegistryValueType.REG_QWORD,
}

# Both short (HKLM) and long (HKEY_LOCAL_MACHINE) hive tokens, upper-cased.
_REGISTRY_HIVE_BY_NAME: dict[str, RegistryHive] = {
    "HKEY_CLASSES_ROOT": RegistryHive.HKCR,
    "HKCR": RegistryHive.HKCR,
    "HKEY_CURRENT_USER": RegistryHive.HKCU,
    "HKCU": RegistryHive.HKCU,
    "HKEY_LOCAL_MACHINE": RegistryHive.HKLM,
    "HKLM": RegistryHive.HKLM,
    "HKEY_USERS": RegistryHive.HKU,
    "HKU": RegistryHive.HKU,
    "HKEY_CURRENT_CONFIG": RegistryHive.HKCC,
    "HKCC": RegistryHive.HKCC,
}


def to_registry_value_type(value_type: int) -> RegistryValueType:
    """Map a native ``REG_*`` type id to :class:`RegistryValueType` (fallback ``REG_NONE``).

    Example:
        >>> to_registry_value_type(1) is RegistryValueType.REG_SZ
        True
        >>> to_registry_value_type(11) is RegistryValueType.REG_QWORD
        True
        >>> to_registry_value_type(999) is RegistryValueType.REG_NONE
        True
    """
    return _REGISTRY_TYPE_BY_INT.get(value_type, RegistryValueType.REG_NONE)


_REGISTRY_TYPE_TO_INT: dict[RegistryValueType, int] = {v: k for k, v in _REGISTRY_TYPE_BY_INT.items()}


def from_registry_value_type(value_type: RegistryValueType) -> int:
    """Map a :class:`RegistryValueType` back to its native ``REG_*`` type id (for writes).

    Example:
        >>> from_registry_value_type(RegistryValueType.REG_SZ)
        1
        >>> from_registry_value_type(RegistryValueType.REG_QWORD)
        11
    """
    return _REGISTRY_TYPE_TO_INT[value_type]


def to_registry_hive(name: str) -> RegistryHive:
    """Map a hive token (short ``HKLM`` or long ``HKEY_LOCAL_MACHINE``) to :class:`RegistryHive`.

    Raises ``KeyError`` for an unknown hive; the adapter maps that to a clean
    :class:`~pwshpy.domain.errors.NativeCallError`.

    Example:
        >>> to_registry_hive("HKLM") is RegistryHive.HKLM
        True
        >>> to_registry_hive("hkey_current_user") is RegistryHive.HKCU
        True
    """
    return _REGISTRY_HIVE_BY_NAME[name.upper()]


def normalize_registry_data(data: bytes | int | str | list[str] | None) -> str | int | list[str] | None:
    """Normalize a raw registry datum to a JSON-safe value (``bytes`` -> hex string).

    Example:
        >>> normalize_registry_data(bytes([0, 255]))
        '00ff'
        >>> normalize_registry_data(["a", "b"])
        ['a', 'b']
        >>> normalize_registry_data(4)
        4
    """
    if isinstance(data, bytes):
        return data.hex()
    return data


# win32 SERVICE_* current-state ids -> ServiceState.
_SERVICE_STATE_BY_INT: dict[int, ServiceState] = {
    1: ServiceState.STOPPED,
    2: ServiceState.START_PENDING,
    3: ServiceState.STOP_PENDING,
    4: ServiceState.RUNNING,
    5: ServiceState.CONTINUE_PENDING,
    6: ServiceState.PAUSE_PENDING,
    7: ServiceState.PAUSED,
}

# win32 SERVICE_*_START ids -> ServiceStartType.
_SERVICE_START_TYPE_BY_INT: dict[int, ServiceStartType] = {
    0: ServiceStartType.BOOT,
    1: ServiceStartType.SYSTEM,
    2: ServiceStartType.AUTOMATIC,
    3: ServiceStartType.MANUAL,
    4: ServiceStartType.DISABLED,
}

# Primary service-type bits, checked in priority order (interactive flag last).
_SERVICE_KIND_BITS: tuple[tuple[int, ServiceKind], ...] = (
    (0x01, ServiceKind.KERNEL_DRIVER),
    (0x02, ServiceKind.FILE_SYSTEM_DRIVER),
    (0x04, ServiceKind.ADAPTER),
    (0x08, ServiceKind.RECOGNIZER_DRIVER),
    (0x10, ServiceKind.WIN32_OWN_PROCESS),
    (0x20, ServiceKind.WIN32_SHARE_PROCESS),
    (0x100, ServiceKind.INTERACTIVE_PROCESS),
)


def to_service_state(state: int) -> ServiceState:
    """Map a win32 ``SERVICE_*`` state id to :class:`ServiceState` (fallback ``STOPPED``).

    Example:
        >>> to_service_state(4) is ServiceState.RUNNING
        True
        >>> to_service_state(1) is ServiceState.STOPPED
        True
    """
    return _SERVICE_STATE_BY_INT.get(state, ServiceState.STOPPED)


def to_service_start_type(start_type: int) -> ServiceStartType:
    """Map a win32 ``SERVICE_*_START`` id to :class:`ServiceStartType` (fallback ``MANUAL``).

    Example:
        >>> to_service_start_type(2) is ServiceStartType.AUTOMATIC
        True
        >>> to_service_start_type(4) is ServiceStartType.DISABLED
        True
    """
    return _SERVICE_START_TYPE_BY_INT.get(start_type, ServiceStartType.MANUAL)


_SERVICE_START_TYPE_TO_INT: dict[ServiceStartType, int] = {v: k for k, v in _SERVICE_START_TYPE_BY_INT.items()}


def from_service_start_type(start_type: ServiceStartType) -> int:
    """Map a :class:`ServiceStartType` back to its win32 ``SERVICE_*_START`` id (for ChangeServiceConfig).

    Example:
        >>> from_service_start_type(ServiceStartType.AUTOMATIC)
        2
        >>> from_service_start_type(ServiceStartType.DISABLED)
        4
    """
    return _SERVICE_START_TYPE_TO_INT[start_type]


def to_service_kind(service_type: int) -> ServiceKind:
    """Map win32 service-type flags to the primary :class:`ServiceKind`.

    Drivers win over process types; the interactive-process flag is used only
    when no other bit is set.

    Example:
        >>> to_service_kind(0x20) is ServiceKind.WIN32_SHARE_PROCESS
        True
        >>> to_service_kind(0x10 | 0x100) is ServiceKind.WIN32_OWN_PROCESS
        True
    """
    for bit, kind in _SERVICE_KIND_BITS:
        if service_type & bit:
            return kind
    return ServiceKind.WIN32_OWN_PROCESS


# win32 event Level ids -> EventLevel (mirrors .NET StandardEventLevel).
_EVENT_LEVEL_BY_INT: dict[int, EventLevel] = {
    0: EventLevel.LOG_ALWAYS,
    1: EventLevel.CRITICAL,
    2: EventLevel.ERROR,
    3: EventLevel.WARNING,
    4: EventLevel.INFORMATION,
    5: EventLevel.VERBOSE,
}

_EVENT_TIME_RE = re.compile(r"^(?P<base>.*T\d{2}:\d{2}:\d{2})(?:\.(?P<frac>\d+))?(?P<tz>.*)$")


def to_event_level(level: int) -> EventLevel:
    """Map a win32 event ``Level`` id to :class:`EventLevel` (fallback ``INFORMATION``).

    Example:
        >>> to_event_level(2) is EventLevel.ERROR
        True
        >>> to_event_level(99) is EventLevel.INFORMATION
        True
    """
    return _EVENT_LEVEL_BY_INT.get(level, EventLevel.INFORMATION)


def parse_event_time(system_time: str | None) -> datetime | None:
    """Parse an event XML ``SystemTime`` (ISO 8601, 100-ns precision) to an aware UTC datetime.

    The FILETIME fraction has up to 7 digits; Python's datetime caps at 6, so it
    is truncated.

    Example:
        >>> parse_event_time("2026-07-07T15:03:14.2206901Z")
        datetime.datetime(2026, 7, 7, 15, 3, 14, 220690, tzinfo=datetime.timezone.utc)
        >>> parse_event_time(None) is None
        True
    """
    if not system_time:
        return None
    text = system_time.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = _EVENT_TIME_RE.match(text)
    if match is None:  # pragma: no cover - defensive; EvtRender always emits ISO
        return None
    frac = match.group("frac")
    iso = match.group("base") + (f".{frac[:6]}" if frac else "") + match.group("tz")
    try:
        return datetime.fromisoformat(iso)
    except ValueError:  # pragma: no cover - defensive
        return None


# Task Scheduler TASK_STATE ids -> TaskState.
_TASK_STATE_BY_INT: dict[int, TaskState] = {
    0: TaskState.UNKNOWN,
    1: TaskState.DISABLED,
    2: TaskState.QUEUED,
    3: TaskState.READY,
    4: TaskState.RUNNING,
}


def to_task_state(state: int) -> TaskState:
    """Map a Task Scheduler ``TASK_STATE`` id to :class:`TaskState` (fallback ``UNKNOWN``).

    Example:
        >>> to_task_state(3) is TaskState.READY
        True
        >>> to_task_state(99) is TaskState.UNKNOWN
        True
    """
    return _TASK_STATE_BY_INT.get(state, TaskState.UNKNOWN)


# Win32 ACCESS_DENIED_* ACE type ids (all other DACL ACE types grant access).
_DENY_ACE_TYPES = frozenset({1, 6, 10, 12})


def to_ace_type(ace_type: int) -> AceType:
    """Map a Win32 DACL ACE type id to :class:`AceType` (denied types -> DENY, else ALLOW).

    Example:
        >>> to_ace_type(0) is AceType.ALLOW
        True
        >>> to_ace_type(1) is AceType.DENY
        True
    """
    return AceType.DENY if ace_type in _DENY_ACE_TYPES else AceType.ALLOW


__all__ = [
    "epoch_to_datetime",
    "from_registry_value_type",
    "from_service_start_type",
    "normalize_registry_data",
    "parse_event_time",
    "to_ace_type",
    "to_address_family",
    "to_connection_state",
    "to_event_level",
    "to_process_name",
    "to_process_status",
    "to_registry_hive",
    "to_registry_value_type",
    "to_service_kind",
    "to_service_start_type",
    "to_service_state",
    "to_task_state",
    "to_transport_protocol",
]
