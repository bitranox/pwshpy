"""Shared marshaling seam for native values -> domain types.

One place converts foreign native representations into the domain object model,
so native adapters stay thin and the mapping is tested once.  .NET has its own
PSObject marshaling; both feed the same records.  The conversions are pure; the one
exception is :func:`skip_or_raise`, the shared skip-or-raise rule for an item a
listing cannot read, which logs a WARNING in its lenient mode.

Contents:
    * :func:`epoch_to_datetime` - POSIX timestamp -> aware UTC ``datetime``.
    * :func:`to_process_status` - raw status string -> :class:`ProcessStatus`.
    * :func:`to_task_run_level` / :func:`com_date_to_datetime` / :func:`to_task_result` -
      Task Scheduler principal run level, COM dates and ``LastTaskResult``.
    * :func:`to_task_duration` / :func:`to_task_multiple_instances` /
      :func:`from_task_multiple_instances` - Task Scheduler ``ExecutionTimeLimit`` and
      ``MultipleInstances``.
    * :func:`to_integrity_level` - mandatory-label RID -> :class:`IntegrityLevel`.
    * :func:`skip_or_raise` - the lenient/strict contract for an item a listing cannot read.
    * :func:`is_com_error` / :func:`com_hresult` / :func:`com_outer_hresult` /
      :func:`com_error_text` - recognise a pywin32 COM/Win32 error and read its unsigned
      HRESULT (unwrapped or as carried) and its text.
    * :func:`native_error` / :func:`native_error_for_hresult` - the typed
      :class:`~pwshpy.domain.errors.NativeCallError` for a caught native failure or a bare
      HRESULT, upgraded to :class:`~pwshpy.domain.errors.NativeAccessDeniedError` for a
      refusal for lack of rights.
"""

from __future__ import annotations

import errno
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from typing import Final, cast

from ...domain.enums import (
    AceType,
    AddressFamily,
    ConnectionState,
    EventLevel,
    IntegrityLevel,
    ProcessStatus,
    RegistryHive,
    RegistryValueType,
    ServiceKind,
    ServiceStartType,
    ServiceState,
    TaskMultipleInstances,
    TaskRunLevel,
    TaskState,
    TransportProtocol,
)
from ...domain.errors import NativeAccessDeniedError, NativeCallError


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


def to_task_run_level(value: int) -> TaskRunLevel:
    """Map ``TASK_RUNLEVEL`` (0 limited, 1 highest) to :class:`TaskRunLevel`.

    Example:
        >>> to_task_run_level(1) is TaskRunLevel.HIGHEST, to_task_run_level(0) is TaskRunLevel.LIMITED
        (True, True)
    """
    return TaskRunLevel.HIGHEST if value == 1 else TaskRunLevel.LIMITED


#: Lower bound of each mandatory integrity band (winnt.h SECURITY_MANDATORY_*_RID), highest first.
#: Bands, not exact values: a token can carry an in-between RID such as MEDIUM_PLUS (0x2100).
_INTEGRITY_BANDS = (
    (0x5000, IntegrityLevel.PROTECTED),
    (0x4000, IntegrityLevel.SYSTEM),
    (0x3000, IntegrityLevel.HIGH),
    (0x2000, IntegrityLevel.MEDIUM),
    (0x1000, IntegrityLevel.LOW),
)


def to_integrity_level(rid: int) -> IntegrityLevel:
    """Map a mandatory-label RID (the last part of ``S-1-16-<rid>``) to :class:`IntegrityLevel`.

    Example:
        >>> to_integrity_level(0x3000).value, to_integrity_level(0x2100).value, to_integrity_level(0).value
        ('High', 'Medium', 'Untrusted')
    """
    for floor, level in _INTEGRITY_BANDS:
        if rid >= floor:
            return level
    return IntegrityLevel.UNTRUSTED


# Task Scheduler "no value" sentinels, kept together in one place. Measured on a real Task
# Scheduler (Windows 11 25H2, 2026-10-21): a task that never ran reads LastRunTime 1999-11-30
# 00:00 (Get-ScheduledTaskInfo prints the same date, so it is the service's own sentinel, not a
# pywin32 artefact) and LastTaskResult SCHED_S_TASK_HAS_NOT_RUN; a task with no next run reads
# NextRunTime as the VT_DATE zero 1899-12-30; a task that is running reads LastTaskResult
# SCHED_S_TASK_RUNNING until it finishes.
_COM_DATE_NEVER_BEFORE_YEAR = 1900  # any COM date before this is the VT_DATE zero (1899-12-30)
_TASK_NEVER_RAN = datetime(1999, 11, 30)  # LastRunTime of a task that never ran
_TASK_HAS_NOT_RUN = 0x41303  # SCHED_S_TASK_HAS_NOT_RUN: LastTaskResult of a task that never ran
_TASK_RUNNING = 0x41301  # SCHED_S_TASK_RUNNING: LastTaskResult while the task runs (no result yet)
_TASK_NO_RESULT = frozenset({_TASK_HAS_NOT_RUN, _TASK_RUNNING})


def com_date_to_datetime(value: datetime | None) -> datetime | None:
    """Map a Task Scheduler COM date (local wall time) to aware UTC; the "never" sentinels -> None.

    The repeated hour at a DST fall-back resolves to the DST offset (fold=0): a VT_DATE cannot
    carry which of the two instants it meant.

    Example:
        >>> com_date_to_datetime(datetime(1899, 12, 30)) is None, com_date_to_datetime(datetime(1999, 11, 30)) is None
        (True, True)
        >>> labelled = com_date_to_datetime(datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc))
        >>> labelled == datetime(2026, 10, 21, 12, 0).astimezone(timezone.utc)
        True
    """
    if value is None:
        return None
    # A VT_DATE has no zone and holds local wall time, yet pywin32 hands it back labelled with a
    # UTC-like TimeZoneInfo('GMT Standard Time'). Honouring that label put every run time off by
    # the host's UTC offset (measured: 18:53 "UTC" for a run at 16:53 UTC on a CEST host), so the
    # label is dropped and the wall time read as local.
    wall = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second, value.microsecond)
    if wall.year < _COM_DATE_NEVER_BEFORE_YEAR or wall == _TASK_NEVER_RAN:
        return None
    return wall.astimezone(timezone.utc)


def to_task_result(value: int) -> int | None:
    """Map ``LastTaskResult`` to an unsigned 32-bit code; has-not-run / still-running -> None.

    Example:
        >>> to_task_result(0), to_task_result(0x41303), to_task_result(0x41301)
        (0, None, None)
    """
    code = value & 0xFFFFFFFF  # pywin32 can hand an HRESULT back signed; Get-ScheduledTaskInfo prints it unsigned
    return None if code in _TASK_NO_RESULT else code


# The ISO 8601 duration subset Task Scheduler stores: it normalises a written PT{n}S into days,
# hours, minutes and seconds (PT90S reads back PT1M30S, 2**31-1 s reads back P24855DT3H14M7S;
# measured on Windows 11, 2026-10-25). Years and months are refused: their length depends on
# the calendar. [0-9] with a digit cap, not \d: \d takes non-ASCII digits, and an uncapped run
# reaches int()'s 4300-digit limit.
_TASK_DURATION = re.compile(
    r"P(?:(?P<days>[0-9]{1,10})D)?(?:T(?=[0-9])(?:(?P<hours>[0-9]{1,12})H)?"
    r"(?:(?P<minutes>[0-9]{1,14})M)?(?:(?P<seconds>[0-9]{1,16})S)?)?"
)


def to_task_duration(value: str) -> timedelta | None:
    """Parse a Task Scheduler duration (``PnDTnHnMnS``, whole seconds) into a ``timedelta``.

    Returns None for anything outside that form - a fraction, years or months, weeks, an
    empty string, a value beyond ``timedelta`` - so an unknown value is reported unknown,
    never guessed. ``"PT0S"`` is ``timedelta(0)``, which Task Scheduler reads as "no limit".

    Example:
        >>> to_task_duration("PT1M30S"), to_task_duration("PT0S"), to_task_duration("P1Y")
        (datetime.timedelta(seconds=90), datetime.timedelta(0), None)
    """
    match = _TASK_DURATION.fullmatch(value)
    if match is None or not any(match.groupdict().values()):
        return None
    parts = {name: int(digits) for name, digits in match.groupdict().items() if digits is not None}
    try:
        return timedelta(**parts)
    except OverflowError:  # more days than timedelta holds
        return None


# Task Scheduler TASK_INSTANCES_POLICY ids <-> TaskMultipleInstances.
_TASK_INSTANCES_BY_INT: dict[int, TaskMultipleInstances] = {
    0: TaskMultipleInstances.PARALLEL,
    1: TaskMultipleInstances.QUEUE,
    2: TaskMultipleInstances.IGNORE_NEW,
    3: TaskMultipleInstances.STOP_EXISTING,
}
_TASK_INSTANCES_TO_INT: dict[TaskMultipleInstances, int] = {v: k for k, v in _TASK_INSTANCES_BY_INT.items()}


def to_task_multiple_instances(value: int) -> TaskMultipleInstances | None:
    """Map a ``TASK_INSTANCES_POLICY`` id to :class:`TaskMultipleInstances`; an unknown id -> None.

    Example:
        >>> to_task_multiple_instances(2) is TaskMultipleInstances.IGNORE_NEW, to_task_multiple_instances(9)
        (True, None)
    """
    return _TASK_INSTANCES_BY_INT.get(value)


def from_task_multiple_instances(policy: TaskMultipleInstances) -> int:
    """Map a :class:`TaskMultipleInstances` to the ``TASK_INSTANCES_POLICY`` id ITaskSettings takes.

    Example:
        >>> from_task_multiple_instances(TaskMultipleInstances.STOP_EXISTING)
        3
    """
    return _TASK_INSTANCES_TO_INT[policy]


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


def skip_or_raise(what: str, exc: Exception, *, strict: bool, log: logging.Logger) -> None:
    """Raise NativeCallError naming ``what`` under ``strict``; otherwise log a WARNING on ``log``.

    The one contract every read listing applies to an item it cannot read (a folder, a task, a
    unit): the lenient default still skips it, so a non-elevated caller lists what it CAN read,
    but never silently - a skipped item reads exactly like "no such item" to a caller that looks
    it up, so the log says what was not read and why. ``log`` is the calling adapter's own module
    logger, so the warning carries that adapter's name, not this module's.

    Args:
        what: Names the item and the read that failed, e.g. ``"cannot load service unit 'x'"``.
        exc: The failure; chained as the cause of the raised error.
        strict: True raises; False logs and returns, letting the caller skip the item.
        log: The logger the WARNING goes to.

    Raises:
        NativeCallError: Under ``strict``, as ``"<what>: <exc>"``.

    Example:
        >>> try:
        ...     skip_or_raise("cannot read 'x'", OSError("denied"), strict=True, log=logging.getLogger("demo"))
        ... except NativeCallError as exc:
        ...     print(exc)
        cannot read 'x': denied
    """
    if strict:
        raise NativeCallError(f"{what}: {exc}") from exc
    log.warning("skipped (pass strict=True to raise instead) - %s: %s", what, exc)


#: pywin32's ``pywintypes.com_error`` and ``pywintypes.error``. Matched by class name AND module
#: because pywintypes is importable only on Windows, and a portable module must not import it;
#: the name alone would admit ``struct.error``, ``zlib.error`` and every other ``error`` class.
_COM_ERROR_NAMES = frozenset({"com_error", "error"})
_PYWINTYPES_MODULE = "pywintypes"
_HRESULT_MASK = 0xFFFFFFFF
#: DISP_E_EXCEPTION: a late-bound IDispatch call reports that the callee raised, with the
#: callee's own status in excepinfo[5] (the scode) and its text in excepinfo[2]; the outer code
#: alone would read "Exception occurred" for every such failure.
_DISP_E_EXCEPTION = 0x80020009
_EXCEPINFO_DESCRIPTION = 2
_EXCEPINFO_SCODE = 5
_ARG_EXCEPINFO = 2
#: HRESULT_FROM_WIN32 (winerror.h): a Win32 code in 1..0xFFFF takes FACILITY_WIN32 and the
#: failure bit; 0 stays success, and anything wider is already an HRESULT.
_FACILITY_WIN32_FAILURE = 0x80070000
_WIN32_CODE_MAX = 0xFFFF
#: Where each class keeps its text in ``args``: com_error is (hresult, strerror, excepinfo,
#: argerror), error is (winerror, funcname, strerror).
_STRERROR_ARG = {"com_error": 1, "error": 2}


def is_com_error(exc: BaseException) -> bool:
    """Return whether ``exc`` is a pywin32 COM/Win32 error (``pywintypes.com_error`` or ``.error``).

    A caller uses it to tell a native failure, which it wraps in
    :class:`~pwshpy.domain.errors.NativeCallError`, from any other exception, which is not
    pwshpy's to translate and propagates unchanged.

    Args:
        exc: Any exception a pywin32 call raised.

    Returns:
        True when the exception's class is named ``com_error`` or ``error`` and is defined in
        ``pywintypes``.

    Example:
        >>> class com_error(Exception): ...
        >>> com_error.__module__ = "pywintypes"
        >>> is_com_error(com_error())
        True
        >>> import struct
        >>> is_com_error(struct.error("a class named error, but not pywin32's"))
        False
    """
    cls = type(exc)
    return cls.__name__ in _COM_ERROR_NAMES and cls.__module__ == _PYWINTYPES_MODULE


def _as_int(value: object) -> int | None:
    """``value`` when it is an int (a bool is not a status code), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _arg(exc: BaseException, index: int) -> object:
    """``exc.args[index]``, or ``None`` when ``args`` is shorter."""
    return exc.args[index] if len(exc.args) > index else None


def _own_hresult(exc: BaseException) -> int | None:
    """The HRESULT the error itself carries, unsigned and not unwrapped; ``None`` for none.

    ``.hresult`` first; ``args[0]`` only for a ``com_error``, whose first argument is an
    HRESULT (a ``pywintypes.error`` keeps a Win32 code there, see :func:`_win32_code`).
    """
    code = _as_int(getattr(exc, "hresult", None))
    if code is None and type(exc).__name__ == "com_error":
        code = _as_int(_arg(exc, 0))
    return None if code is None else code & _HRESULT_MASK


def _win32_code(exc: BaseException) -> int | None:
    """The Win32 code an error carries: ``.winerror``, else a ``pywintypes.error``'s ``args[0]``."""
    code = _as_int(getattr(exc, "winerror", None))
    if code is None and type(exc).__name__ == "error":
        code = _as_int(_arg(exc, 0))
    return code


def _callee_excepinfo(exc: BaseException, own: int | None) -> tuple[object, ...] | None:
    """The full excepinfo of a DISP_E_EXCEPTION (``.excepinfo``, else ``args[2]``), else ``None``."""
    if own != _DISP_E_EXCEPTION:
        return None
    info: object = getattr(exc, "excepinfo", None)
    if info is None:
        info = _arg(exc, _ARG_EXCEPINFO)
    if not isinstance(info, tuple):
        return None
    fields = cast("tuple[object, ...]", info)  # pywin32's excepinfo is an untyped 6-tuple
    return fields if len(fields) > _EXCEPINFO_SCODE else None


def _callee_scode(fields: tuple[object, ...] | None) -> int | None:
    """The callee's nonzero status from an excepinfo, unsigned; ``None`` when it names none."""
    scode = None if fields is None else _as_int(fields[_EXCEPINFO_SCODE])
    if scode is None:
        return None
    return (scode & _HRESULT_MASK) or None


def _win32_to_hresult(code: int) -> int:
    """HRESULT_FROM_WIN32: ``0x8007xxxx`` for a Win32 code, 0 stays 0, anything else masked."""
    if 0 < code <= _WIN32_CODE_MAX:
        return _FACILITY_WIN32_FAILURE | code
    return code & _HRESULT_MASK


def com_outer_hresult(exc: BaseException) -> int | None:
    """Return the HRESULT a pywin32 error itself carries, unsigned, WITHOUT unwrapping.

    For a DISP_E_EXCEPTION this is ``0x80020009`` itself, where :func:`com_hresult` gives the
    callee's status; a caller compares the two to tell a message which code it shows. A Win32
    code is mapped as HRESULT_FROM_WIN32, as in :func:`com_hresult`.

    Args:
        exc: Any exception a pywin32 call raised.

    Returns:
        The unsigned HRESULT, or ``None`` when ``exc`` is not a pywin32 error or carries no
        int code.

    Example:
        >>> class com_error(Exception):
        ...     hresult = -2147352567
        ...     excepinfo = (0, "source", "No network", None, 0, -2145107924)
        >>> com_error.__module__ = "pywintypes"
        >>> f"0x{com_outer_hresult(com_error()):08X}"
        '0x80020009'
        >>> com_outer_hresult(ValueError()) is None
        True
    """
    if not is_com_error(exc):
        return None
    own = _own_hresult(exc)
    if own is not None:
        return own
    win32 = _win32_code(exc)
    return None if win32 is None else _win32_to_hresult(win32)


def com_hresult(exc: BaseException) -> int | None:
    """Return the HRESULT a pywin32 error carries, as an unsigned 32-bit int.

    pywin32 reports an HRESULT signed (``0x80070002`` arrives as ``-2147024894``), so it is
    masked to 32 bits, ready for ``0x%08X``. A DISP_E_EXCEPTION (``0x80020009``) is unwrapped
    to the callee's own status in ``excepinfo[5]`` when that is a nonzero int. A
    ``pywintypes.error`` carries a Win32 code on ``.winerror`` (or as ``args[0]``), which is
    mapped as HRESULT_FROM_WIN32 (5 becomes ``0x80070005``). ``None`` means no status could be
    read: either ``exc`` is not a pywin32 error (see :func:`is_com_error`, which a caller asks
    first when the two cases must be told apart) or it carries no int code.

    Args:
        exc: Any exception a pywin32 call raised.

    Returns:
        The unsigned HRESULT, or ``None``.

    Example:
        >>> class com_error(Exception):
        ...     hresult = -2147024894
        >>> com_error.__module__ = "pywintypes"
        >>> f"0x{com_hresult(com_error()):08X}"
        '0x80070002'
        >>> class error(Exception):
        ...     winerror = 5
        >>> error.__module__ = "pywintypes"
        >>> f"0x{com_hresult(error()):08X}"
        '0x80070005'
        >>> com_hresult(ValueError()) is None
        True
    """
    outer = com_outer_hresult(exc)
    if outer is None:
        return None
    scode = _callee_scode(_callee_excepinfo(exc, outer))
    return outer if scode is None else scode


def com_error_text(exc: BaseException) -> str:
    """Return the text a pywin32 error carries, for a message beside its HRESULT.

    For a DISP_E_EXCEPTION that is the callee's own description (``excepinfo[2]``) when it is
    a non-empty str, since the outer text only says "Exception occurred"; otherwise the
    error's ``strerror``. The text is localized, so it is for people: never decide on it.

    Args:
        exc: Any exception a pywin32 call raised.

    Returns:
        The text, or ``""`` when ``exc`` is not a pywin32 error or carries none.

    Example:
        >>> class com_error(Exception):
        ...     hresult = -2147352567
        ...     strerror = "Exception occurred."
        ...     excepinfo = (0, "source", "No network", None, 0, -2145107924)
        >>> com_error.__module__ = "pywintypes"
        >>> com_error_text(com_error())
        'No network'
        >>> com_error_text(ValueError("not pywin32's"))
        ''
    """
    if not is_com_error(exc):
        return ""
    fields = _callee_excepinfo(exc, com_outer_hresult(exc))
    description = None if fields is None else fields[_EXCEPINFO_DESCRIPTION]
    if isinstance(description, str) and description:
        return description
    text: object = getattr(exc, "strerror", None)
    if text is None:
        text = _arg(exc, _STRERROR_ARG[type(exc).__name__])
    return text if isinstance(text, str) else ""


#: E_ACCESSDENIED, which is also HRESULT_FROM_WIN32(ERROR_ACCESS_DENIED = 5).
E_ACCESSDENIED: Final = 0x80070005
#: The POSIX codes a refused-for-rights call reports; both map to PermissionError in CPython.
_DENIED_ERRNOS = frozenset({errno.EACCES, errno.EPERM})
#: psutil.AccessDenied is not an OSError; matched by name AND module, like the pywin32 classes,
#: so a portable module never imports psutil just to recognise it.
_PSUTIL_DENIED = "AccessDenied"
_PSUTIL_MODULE = "psutil"


def _cause_hresult(cause: BaseException) -> int | None:
    """The unsigned HRESULT a caught error carries: pywin32 first, then an OSError's winerror."""
    if is_com_error(cause):
        return com_hresult(cause)
    # getattr, not cause.winerror: CPython sets winerror only on Windows (typeshed agrees).
    winerror = _as_int(getattr(cause, "winerror", None)) if isinstance(cause, OSError) else None
    return None if winerror is None else _win32_to_hresult(winerror)


def _is_denied(cause: BaseException, hresult: int | None) -> bool:
    """Whether the cause is a refusal for lack of rights, judged by code and type, never text."""
    if hresult is not None:
        return hresult == E_ACCESSDENIED
    if isinstance(cause, OSError):
        return cause.errno in _DENIED_ERRNOS
    cls = type(cause)
    return cls.__name__ == _PSUTIL_DENIED and cls.__module__.split(".")[0] == _PSUTIL_MODULE


def native_error(
    message: str, cause: BaseException, *, kind: type[NativeCallError] = NativeCallError
) -> NativeCallError:
    """Return the typed pwshpy error for a caught native failure; the caller raises it ``from cause``.

    The single place an adapter turns a caught exception into a
    :class:`~pwshpy.domain.errors.NativeCallError`, so every wrapped failure carries its HRESULT
    and a refusal for lack of rights becomes a
    :class:`~pwshpy.domain.errors.NativeAccessDeniedError` (a ``PermissionError`` too). The
    decision reads codes and types only, never the localized message text.

    Args:
        message: The human-readable description.
        cause: The exception the native call raised.
        kind: A more specific subclass the call site means (e.g. ``ElevationDeclinedError``);
            it is kept as is, never replaced by the access-denied type.

    Returns:
        The error to raise.

    Example:
        >>> import errno
        >>> type(native_error("read failed", PermissionError(errno.EACCES, "x"))).__name__
        'NativeAccessDeniedError'
        >>> native_error("lookup failed", KeyError("k")).hresult is None
        True
    """
    hresult = _cause_hresult(cause)
    # An explicit kind is the call site's own judgement (a declined UAC prompt is a decision,
    # not a missing right), so only the default is ever upgraded.
    if kind is not NativeCallError or not _is_denied(cause, hresult):
        return kind(message, hresult=hresult)
    denied = NativeAccessDeniedError(message, hresult=hresult)
    # Only errno: NativeAccessDeniedError.__reduce__ carries errno across pickle and no other
    # OSError slot, so a strerror or filename set here would be lost on a multiprocessing pipe.
    denied.errno = cause.errno if isinstance(cause, OSError) else None
    return denied


def native_error_for_hresult(message: str, hresult: int | None) -> NativeCallError:
    """Return the typed error for an HRESULT that crossed a process boundary as a plain int.

    Args:
        message: The human-readable description.
        hresult: The unsigned HRESULT, or ``None``.

    Returns:
        A :class:`~pwshpy.domain.errors.NativeAccessDeniedError` for E_ACCESSDENIED, else a
        :class:`~pwshpy.domain.errors.NativeCallError`, either with ``hresult`` set.

    Example:
        >>> type(native_error_for_hresult("x", 0x80070005)).__name__
        'NativeAccessDeniedError'
        >>> type(native_error_for_hresult("x", None)).__name__
        'NativeCallError'
    """
    cls = NativeAccessDeniedError if hresult == E_ACCESSDENIED else NativeCallError
    return cls(message, hresult=hresult)


__all__ = [
    "E_ACCESSDENIED",
    "com_date_to_datetime",
    "com_error_text",
    "com_hresult",
    "com_outer_hresult",
    "epoch_to_datetime",
    "from_registry_value_type",
    "from_service_start_type",
    "from_task_multiple_instances",
    "is_com_error",
    "native_error",
    "native_error_for_hresult",
    "normalize_registry_data",
    "parse_event_time",
    "skip_or_raise",
    "to_ace_type",
    "to_address_family",
    "to_connection_state",
    "to_event_level",
    "to_integrity_level",
    "to_process_name",
    "to_process_status",
    "to_registry_hive",
    "to_registry_value_type",
    "to_service_kind",
    "to_service_start_type",
    "to_service_state",
    "to_task_duration",
    "to_task_multiple_instances",
    "to_task_result",
    "to_task_run_level",
    "to_task_state",
    "to_transport_protocol",
]
