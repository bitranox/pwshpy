"""Typed domain records - the ``PSObject``-like object model.

Both backends marshal into the **same** records so the :mod:`~pwshpy.domain.pipeline`
composes over either source.  :class:`PSRecord` is the Pydantic base; concrete
records (e.g. :class:`ProcessInfo`) add typed fields.

Records produced by trusted native APIs are built with
:meth:`pydantic.BaseModel.model_construct` (validation skipped) on the hot path;
full validation is reserved for genuine external boundaries (.NET PSObject
marshaling and user-supplied data), per the strict-data rule.

Contents:
    * :class:`PSRecord` - frozen Pydantic base record with JSON helpers.
    * :class:`ProcessInfo` - a single operating-system process.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .enums import (
    AceType,
    AclEntryKind,
    AddressFamily,
    ConnectionState,
    EventLevel,
    ImageArchitecture,
    IntegrityLevel,
    ProcessStatus,
    RegistryHive,
    RegistryValueType,
    ServiceKind,
    ServiceStartType,
    ServiceState,
    TaskRunLevel,
    TaskState,
    TransportProtocol,
)
from .errors import NativeCallError
from .scheduling import TaskSettings


class PSRecord(BaseModel):
    """Immutable base for every typed record flowing through the pipeline.

    Frozen so records behave as value objects.  ``to_dict`` yields a
    JSON-serializable mapping (``datetime`` -> ISO string, enum -> its value),
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

        A mapping key holding a lone surrogate (a byte that was not valid UTF-8, decoded with
        ``surrogateescape``) is kept as it is, exactly like such a value, so the output layer can
        write each such byte as U+FFFD and name the field. pydantic-core alone does neither: it
        rewrote a dict field's own key into three U+FFFD without a word (two keys differing only
        in their bad bytes then collapsed into one entry), and raised ``UnicodeEncodeError`` for a
        key deeper in an open bag. Such keys stand in as placeholders while pydantic dumps and are
        put back afterwards; a record without one is ``model_dump(mode="json")`` unchanged.

        Returns:
            The record's fields in their JSON form (``datetime`` -> ISO text, enum -> its value).

        Example:
            >>> class Tag(PSRecord):
            ...     name: str
            >>> Tag(name="a").to_dict()
            {'name': 'a'}
            >>> class Bag(PSRecord):
            ...     data: dict[str, Any]
            >>> Bag(data={"k" + chr(0xDCFF): 1}).to_dict() == {"data": {"k" + chr(0xDCFF): 1}}
            True
        """
        if not _holds_surrogate_key(self):
            return self.model_dump(mode="json")
        mask = _KeyMask(self)
        masked = cast("PSRecord", mask.mask(self))
        return cast("dict[str, Any]", mask.unmask(masked.model_dump(mode="json")))


#: Every UTF-16 surrogate code point; as a lone code point in a ``str`` it is an undecodable byte.
_SURROGATE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")


def _has_surrogate(key: object) -> bool:
    """Whether ``key`` is a string holding a lone surrogate (an ASCII key never does)."""
    return isinstance(key, str) and not key.isascii() and _SURROGATE.search(key) is not None


def _children(value: object) -> Iterator[tuple[object, object]]:
    """``(key, item)`` for each member of a record, mapping, list or tuple (key None for a non-mapping)."""
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield None, getattr(value, name)
    elif isinstance(value, Mapping):
        yield from cast("Mapping[object, object]", value).items()
    elif isinstance(value, (list, tuple)):
        for item in cast("list[object] | tuple[object, ...]", value):
            yield None, item


def _holds_surrogate_key(value: object) -> bool:
    """Whether a mapping anywhere in ``value`` (nested records included) has a surrogate key."""
    return any(_has_surrogate(key) or _holds_surrogate_key(item) for key, item in _children(value))


def _string_keys(value: object) -> Iterator[str]:
    """Every string mapping key anywhere in ``value``."""
    for key, item in _children(value):
        if isinstance(key, str):
            yield key
        yield from _string_keys(item)


class _KeyMask:
    """Swaps each surrogate key for a placeholder pydantic can dump, and back again afterwards.

    A placeholder is a NUL plus a number, chosen so it equals no key anywhere in the record: a real
    key that happens to look like one is never mistaken for a stand-in.
    """

    def __init__(self, record: PSRecord) -> None:
        self._taken = set(_string_keys(record))
        self._originals: dict[str, str] = {}

    def mask(self, value: object) -> object:
        """``value`` with every surrogate key replaced by a placeholder, records copied as needed."""
        if isinstance(value, BaseModel):
            update = {name: self.mask(getattr(value, name)) for name in type(value).model_fields}
            return value.model_copy(update=update)
        if isinstance(value, Mapping):
            mapping = cast("Mapping[object, object]", value)
            return {self._stand_in(key): self.mask(item) for key, item in mapping.items()}
        if isinstance(value, list):
            return [self.mask(item) for item in cast("list[object]", value)]
        if isinstance(value, tuple):
            return tuple(self.mask(item) for item in cast("tuple[object, ...]", value))
        return value

    def unmask(self, value: object) -> object:
        """The dumped ``value`` with each placeholder key turned back into its original key."""
        if isinstance(value, dict):
            mapping = cast("dict[object, object]", value)
            return {self._original(key): self.unmask(item) for key, item in mapping.items()}
        if isinstance(value, list):
            return [self.unmask(item) for item in cast("list[object]", value)]
        return value

    def _stand_in(self, key: object) -> object:
        if not _has_surrogate(key):
            return key
        number = len(self._originals)
        while (placeholder := f"{chr(0)}{number}") in self._taken:
            number += 1
        self._taken.add(placeholder)
        self._originals[placeholder] = cast("str", key)
        return placeholder

    def _original(self, key: object) -> object:
        return self._originals.get(key, key) if isinstance(key, str) else key


class ProcessInfo(PSRecord):
    """A single operating-system process (the native ``Get-Process`` record).

    ``name`` is the name psutil reports, which differs from ``Get-Process``'s
    ``ProcessName`` for two Windows pseudo-processes: the kernel's memory
    compression store is ``MemCompression`` (``Get-Process``: ``Memory Compression``)
    and PID 0 is ``System Idle Process`` (``Get-Process``: ``Idle``).

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


class ProcessResult(PSRecord):
    """The typed result of running an external program (the ``ps.exec`` record).

    A list-based, **shell-free** run: ``argv`` is passed verbatim (no quoting or
    ``--%`` stop-parsing hell), ``exit_code`` is ALWAYS populated (unlike
    PowerShell's ``$LASTEXITCODE``, which is not set for a native command in a
    pipeline), and ``stderr`` is plain captured data - never mistaken for a
    terminating error.  :meth:`check` raises on a nonzero exit, the opt-in
    equivalent of ``$ErrorActionPreference='Stop'``.

    Example:
        >>> r = ProcessResult(argv=["echo", "hi"], exit_code=0, stdout="hi\\n")
        >>> r.succeeded, r.check() is r
        (True, True)
        >>> ProcessResult(argv=["false"], exit_code=1).succeeded
        False
    """

    argv: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0

    @property
    def succeeded(self) -> bool:
        """``True`` when the process exited zero.

        Example:
            >>> ProcessResult(argv=["x"], exit_code=0).succeeded
            True
        """
        return self.exit_code == 0

    def check(self) -> ProcessResult:
        """Return ``self`` if the process succeeded, else raise :class:`NativeCallError`.

        Example:
            >>> ProcessResult(argv=["x"], exit_code=0).check().exit_code
            0
        """
        if self.exit_code != 0:
            raise NativeCallError(
                f"Command {self.argv!r} exited with code {self.exit_code}: {self.stderr.strip() or '(no stderr)'}"
            )
        return self


class Hotfix(PSRecord):
    """An installed Windows update/hotfix (the ``get_hotfix`` record - Get-Hotfix).

    Example:
        >>> h = Hotfix(hotfix_id="KB5001234", description="Security Update", installed_by="admin")
        >>> h.hotfix_id, h.description
        ('KB5001234', 'Security Update')
    """

    hotfix_id: str
    description: str = ""
    installed_on: str = ""
    installed_by: str = ""


class ComputerInfo(PSRecord):
    """A summary of the local computer (the ``get_computer_info`` record - Get-ComputerInfo).

    Example:
        >>> c = ComputerInfo(hostname="pc1", os_name="Windows", os_version="10.0.26200",
        ...                  architecture="AMD64", cpu_count=8, total_memory_bytes=17_000_000_000)
        >>> c.hostname, c.cpu_count
        ('pc1', 8)
    """

    hostname: str
    os_name: str = ""
    os_version: str = ""
    architecture: str = ""
    cpu_count: int | None = None
    total_memory_bytes: int | None = None
    manufacturer: str = ""
    model: str = ""


class WebResponse(PSRecord):
    """An HTTP response (the ``invoke_web_request`` record - Invoke-WebRequest).

    ``text`` is the decoded body; ``headers`` a plain dict; ``status_code`` the HTTP
    status.  ``invoke_rest_method`` instead returns the parsed JSON directly.

    Example:
        >>> r = WebResponse(url="http://x/", status_code=200, text="ok", headers={"Content-Type": "text/plain"})
        >>> r.status_code, r.text, r.headers["Content-Type"]
        (200, 'ok', 'text/plain')
    """

    url: str
    status_code: int
    text: str = ""
    headers: dict[str, str] = Field(default_factory=dict)


class NetAdapter(PSRecord):
    """A network interface (the ``get_net_adapter`` record - Get-NetAdapter).

    Example:
        >>> a = NetAdapter(name="eth0", is_up=True, speed_mbps=1000, mtu=1500, mac_address="00:11:22:33:44:55")
        >>> a.name, a.is_up, a.speed_mbps
        ('eth0', True, 1000)
    """

    name: str
    is_up: bool
    speed_mbps: int | None = None
    mtu: int | None = None
    mac_address: str = ""


class NetIpAddress(PSRecord):
    """One IP address bound to an interface (the ``get_net_ip_address`` record - Get-NetIPAddress).

    Example:
        >>> ip = NetIpAddress(interface="eth0", address="10.0.0.5", family=AddressFamily.IPV4, netmask="255.255.255.0")
        >>> ip.address, ip.family.value
        ('10.0.0.5', 'IPv4')
    """

    interface: str
    address: str
    family: AddressFamily
    netmask: str = ""


class FileSystemItem(PSRecord):
    """A file or directory (the ``get_child_item`` / ``get_item`` record - Get-ChildItem/Get-Item).

    ``path`` is the full path; ``name`` the leaf; ``is_directory`` distinguishes the two;
    ``size`` is the byte length (``None`` for a directory) and ``modified`` the mtime.

    Example:
        >>> from datetime import datetime, timezone
        >>> f = FileSystemItem(path="/tmp/a.txt", name="a.txt", is_directory=False, size=12)
        >>> f.name, f.is_directory, f.size
        ('a.txt', False, 12)
    """

    path: str
    name: str
    is_directory: bool
    size: int | None = None
    modified: datetime | None = None


class Credential(PSRecord):
    """A username + secret pair (the ``ps.get_credential`` / credential-vault record).

    The ``secret`` is a Pydantic :class:`~pydantic.SecretStr`, so it is **masked**
    in ``repr``, ``str``, and ``to_dict()`` (JSON/JSONL/logs never leak it) - the
    honest replacement for PowerShell's plaintext-password-in-a-script habit.
    Retrieve the real value deliberately with ``credential.secret.get_secret_value()``.

    Example:
        >>> c = Credential(target="db", username="svc", secret=SecretStr("hunter2"))
        >>> c.to_dict()["secret"]
        '**********'
        >>> c.secret.get_secret_value()
        'hunter2'
    """

    username: str
    secret: SecretStr
    target: str = ""


class NetConnection(PSRecord):
    """A single network connection (the native ``Get-NetTCPConnection`` record).

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
    """A mounted filesystem and its usage (the native ``Get-Volume`` record).

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
    """System boot time and elapsed uptime (the native ``Get-Uptime`` record).

    Example:
        >>> from datetime import datetime, timezone
        >>> up = SystemUptime(boot_time=datetime(2020, 1, 1, tzinfo=timezone.utc), uptime_seconds=3600.0)
        >>> up.uptime_seconds
        3600.0
    """

    boot_time: datetime
    uptime_seconds: float


class DnsRecord(PSRecord):
    """A resolved DNS address (the native ``Resolve-DnsName`` record).

    Example:
        >>> rec = DnsRecord(name="localhost", address="127.0.0.1", family=AddressFamily.IPV4)
        >>> rec.address, rec.family.value
        ('127.0.0.1', 'IPv4')
    """

    name: str
    address: str
    family: AddressFamily


class ConnectionTest(PSRecord):
    """The result of a TCP reachability probe (the native ``Test-Connection`` record).

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
    """An environment variable (the native ``env:`` drive record).

    Example:
        >>> EnvVar(name="PATH", value="/usr/bin").name
        'PATH'
    """

    name: str
    value: str


class RegistryKey(PSRecord):
    """A single registry subkey (the native ``Get-ChildItem`` record).

    ``key`` is the parent path under ``hive`` that was enumerated; ``name`` is
    the immediate subkey's leaf name.

    Example:
        >>> RegistryKey(hive=RegistryHive.HKLM, key="SOFTWARE", name="Microsoft").name
        'Microsoft'
    """

    hive: RegistryHive
    key: str
    name: str


class RegistryValue(PSRecord):
    """A single registry value (the native ``Get-ItemProperty`` record).

    ``key`` is the value's key path under ``hive``; ``name`` is the value name
    (empty for a key's default value).  ``data`` is normalized to a JSON-safe
    form: ``REG_BINARY`` bytes become a hex string, ``REG_MULTI_SZ`` stays a
    list, numbers and strings pass through.

    Example:
        >>> val = RegistryValue(
        ...     hive=RegistryHive.HKLM, key="SOFTWARE", name="CurrentBuild",
        ...     type=RegistryValueType.REG_SZ, data="18363",
        ... )
        >>> val.hive.value, val.type.value, val.data
        ('HKLM', 'REG_SZ', '18363')
    """

    hive: RegistryHive
    key: str
    name: str
    type: RegistryValueType
    data: str | int | list[str] | None = None


class ServiceInfo(PSRecord):
    """A single service (the native ``Get-Service`` record; portable).

    win32service on Windows, systemd (D-Bus) on Linux. On Linux ``name`` is the unit name
    (``"sshd.service"``), ``display_name`` the unit Description, ``status`` maps the systemd
    ActiveState and ``start_type`` the UnitFileState; ``service_type`` is ``None`` (a Windows
    ServiceType with no systemd analogue).

    ``dependent_services`` is deliberately NOT a field: the services that depend
    on X are the inverse of every service's ``required_services`` and are cheaply
    derived in Python (see ``docs/powershell-switch-mapping.md``).

    Example:
        >>> svc = ServiceInfo(
        ...     name="Winmgmt", display_name="Windows Management Instrumentation",
        ...     status=ServiceState.RUNNING, service_type=ServiceKind.WIN32_SHARE_PROCESS,
        ...     start_type=ServiceStartType.AUTOMATIC, pid=1234,
        ... )
        >>> svc.name, svc.status.value, svc.pid, svc.required_services
        ('Winmgmt', 'Running', 1234, [])
    """

    name: str
    display_name: str
    status: ServiceState
    service_type: ServiceKind | None = None
    pid: int | None = None
    can_stop: bool = False
    can_pause_continue: bool = False
    start_type: ServiceStartType | None = None
    required_services: list[str] = Field(default_factory=list)


class EventLogEntry(PSRecord):
    """A single system event-log record (the native ``Get-WinEvent`` record; portable).

    win32evtlog on Windows, journald on Linux. ``level`` is the canonical English
    severity (``Get-WinEvent``'s ``LevelDisplayName`` and journald priorities are both
    normalized to it). ``message`` is the rendered text (on Windows the provider's
    message, as ``Get-WinEvent``'s ``Message`` renders it), else ``None`` - never an error
    text. A message whose inserts the provider cannot resolve (errors 15029 / 15030) is
    ``None``, where ``Get-WinEvent`` shows the partial text.
    On journald ``record_id`` / ``event_id`` are 0 (journald has no numeric event id;
    its identity is the string cursor) and ``user_id`` is the ``_UID``.

    Example:
        >>> from datetime import datetime, timezone
        >>> e = EventLogEntry(
        ...     log_name="System", record_id=4627, event_id=566,
        ...     level=EventLevel.INFORMATION, provider_name="Microsoft-Windows-Kernel-Power",
        ...     time_created=datetime(2026, 1, 1, tzinfo=timezone.utc), machine_name="host",
        ... )
        >>> e.event_id, e.level.value, e.user_id
        (566, 'Information', None)
    """

    log_name: str
    record_id: int
    event_id: int
    level: EventLevel
    provider_name: str
    time_created: datetime | None = None
    machine_name: str = ""
    user_id: str | None = None
    message: str | None = None


class CimInstance(PSRecord):
    """A generic CIM/WMI instance (the native ``Get-CimInstance`` record).

    A class name plus a bag of properties: WMI classes have wildly different
    shapes, so the properties are an open dict rather than fixed fields.  Values
    are normalized to JSON-friendly types (arrays become lists); CIM_DATETIME and
    64-bit numerics stay as the strings WMI reports.

    Example:
        >>> ci = CimInstance(class_name="Win32_OperatingSystem", properties={"Version": "10.0.26200"})
        >>> ci.class_name, ci.properties["Version"]
        ('Win32_OperatingSystem', '10.0.26200')
    """

    class_name: str
    properties: dict[str, Any] = Field(default_factory=dict)


class PSObjectRecord(PSRecord):
    """A marshaled .NET PowerShell object - what ``ps.run`` yields for a structured result.

    A wrapped primitive (a string, an int) comes back as the plain Python value;
    a structured PSObject becomes this record: its .NET ``type_name`` plus a bag of
    ``properties`` (like :class:`CimInstance`, PSObjects are heterogeneous).

    Example:
        >>> o = PSObjectRecord(type_name="System.Diagnostics.Process", properties={"Name": "pwsh"})
        >>> o.type_name, o.properties["Name"]
        ('System.Diagnostics.Process', 'pwsh')
    """

    type_name: str
    properties: dict[str, Any] = Field(default_factory=dict)


class PSInvocationResult(PSRecord):
    """The full result of one .NET invocation - output plus every side stream.

    ``ps.run`` returns only the output objects (and raises on an error); ``ps.cmdlet``
    returns this richer record so a caller can inspect the Warning/Verbose/Debug/
    Information streams and decide for itself whether an error stream is fatal.
    ``output`` holds marshaled objects (primitives, or :class:`PSObjectRecord` for
    structured results); the stream fields are the rendered text of each record.

    Example:
        >>> r = PSInvocationResult(output=[42], warnings=["heads up"], had_errors=False)
        >>> r.output, r.warnings, r.errors, r.had_errors
        ([42], ['heads up'], [], False)
    """

    output: list[Any] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    verbose: list[str] = Field(default_factory=list)
    debug: list[str] = Field(default_factory=list)
    information: list[str] = Field(default_factory=list)
    had_errors: bool = False


class CommandParameter(PSRecord):
    """One parameter of a PowerShell command (part of :class:`CommandInfo`).

    Example:
        >>> p = CommandParameter(name="Path", type="String", mandatory=True, aliases=["PSPath"])
        >>> p.name, p.mandatory, p.aliases
        ('Path', True, ['PSPath'])
    """

    name: str
    type: str = ""
    mandatory: bool = False
    aliases: list[str] = Field(default_factory=list)


def _no_parameters() -> list[CommandParameter]:
    """Typed default factory for :attr:`CommandInfo.parameters` (pyright infers the element type)."""
    return []


class CommandInfo(PSRecord):
    """Introspected metadata for a PowerShell command (the ``ps.get_command`` record, .NET).

    Answers "what parameters does this cmdlet take?" as typed data instead of the
    guesswork PowerShell's inconsistent cmdlet surface forces - names, .NET type
    names, and whether each is mandatory, straight from the SDK's ``CommandInfo``.

    Example:
        >>> c = CommandInfo(name="Get-Item", command_type="Cmdlet",
        ...                 parameters=[CommandParameter(name="Path", type="String")])
        >>> c.name, c.parameters[0].name
        ('Get-Item', 'Path')
    """

    name: str
    command_type: str = ""
    module: str = ""
    parameters: list[CommandParameter] = Field(default_factory=_no_parameters)


class ScheduledTaskInfo(PSRecord):
    """A scheduled task (the native ``Get-ScheduledTask`` record; portable).

    On Windows ``task_path`` is the containing Task Scheduler folder (ends with a
    backslash, like ``Get-ScheduledTask``) and ``task_name`` is the leaf name. On Linux a
    task is a systemd ``.timer`` unit: ``task_name`` is the unit base name (no ``.timer``),
    ``task_path`` is the flat root ``"/"``, and ``description`` comes from the unit.

    ``user_id`` is the principal's SID on Windows and its decimal uid on Linux; it is ``""``
    when unknown, when the principal is a group rather than a user, or when the account no
    longer resolves to a SID (the task is still listed). ``run_level`` is None where the
    backend has no run-level concept or it cannot be read.
    ``last_run_time`` / ``next_run_time`` are aware UTC (None = never ran / nothing scheduled);
    ``last_task_result`` is the last exit code or HRESULT (unsigned on Windows); 0 means it ran
    and succeeded. It is None until the task has run AND while a run is in progress: Windows
    reports no result until the run finishes, and Linux reads None for a running service too.
    ``last_run_time`` tells the two apart: it is None for a task that never ran (or whose run
    time cannot be read) and set once a run has started, an on-demand start included on both
    backends (Linux takes the later of the timer's last trigger and the service's last start).
    ``settings`` holds the task's effective :class:`~pwshpy.domain.scheduling.TaskSettings`
    (OS defaults included); it is None where the backend did not read them, and a field inside
    it is None where that one value cannot be read.

    Example:
        >>> t = ScheduledTaskInfo(task_name="Backup", task_path="Root", state=TaskState.READY, enabled=True)
        >>> t.task_name, t.state.value, t.enabled
        ('Backup', 'Ready', True)
        >>> t.user_id, t.run_level, t.last_task_result, t.settings
        ('', None, None, None)
    """

    task_name: str
    task_path: str
    state: TaskState
    enabled: bool = True
    author: str = ""
    description: str = ""
    user_id: str = ""
    run_level: TaskRunLevel | None = None
    last_run_time: datetime | None = None
    last_task_result: int | None = None
    next_run_time: datetime | None = None
    settings: TaskSettings | None = None


class LocalUser(PSRecord):
    """A local user account (the native ``Get-LocalUser`` record; portable).

    Identified by ``sid`` - the stable, locale-independent id: a Windows SID
    (``"S-1-5-..."``) on Windows, or the POSIX **uid** as a string (``"0"`` for root)
    on Linux/macOS. ``name`` is localized for built-in Windows accounts ("Gast" for
    Guest on German Windows). On POSIX ``enabled`` is inferred from the login shell and
    ``full_name`` is the first GECOS field.

    Example:
        >>> u = LocalUser(name="Guest", sid="S-1-5-21-1-2-3-501", enabled=False)
        >>> u.sid, u.enabled
        ('S-1-5-21-1-2-3-501', False)
    """

    name: str
    sid: str
    enabled: bool = True
    full_name: str = ""
    description: str = ""


class CurrentIdentity(PSRecord):
    """The account this process runs as, read from its token (the ``get_current_identity`` record).

    Portable, like ``whoami /user``.  ``sid`` is the canonical, locale-independent identity:
    the token user's SID on Windows (``"S-1-5-18"`` for SYSTEM), or the **effective uid** as a
    string on POSIX (``"0"`` for root), the same convention as :class:`LocalUser`.  ``account``
    (``DOMAIN\\name`` on Windows, the passwd name on POSIX), ``name`` and ``domain`` are
    display only: they are localized for built-in Windows accounts and empty when the SID or
    uid has no name (an orphaned SID, a container uid with no passwd entry).  The record never
    reads ``USERNAME``/``USERDOMAIN``, which under a SYSTEM scheduled task name the computer
    account rather than SYSTEM.

    ``is_system`` is SID ``S-1-5-18`` on Windows and euid 0 on POSIX; ``elevated`` is the token's
    elevation flag on Windows and euid 0 on POSIX (as :func:`ps.is_elevated` reports it).
    ``integrity_level`` is the token's mandatory label on Windows and ``None`` on POSIX, which has
    no such label; the field is kept so the record has one shape on every OS.

    Example:
        >>> me = CurrentIdentity(sid="S-1-5-18", account="NT AUTHORITY\\\\SYSTEM", name="SYSTEM",
        ...                      domain="NT AUTHORITY", is_system=True, elevated=True,
        ...                      integrity_level=IntegrityLevel.SYSTEM)
        >>> me.sid, me.is_system, me.integrity_level.value
        ('S-1-5-18', True, 'System')
    """

    sid: str
    account: str = ""
    name: str = ""
    domain: str = ""
    is_system: bool = False
    elevated: bool = False
    integrity_level: IntegrityLevel | None = None


class LocalGroup(PSRecord):
    """A local group (the native ``Get-LocalGroup`` record; portable).

    Identified by ``sid`` - a Windows SID, or the POSIX **gid** as a string on
    Linux/macOS. ``name`` is localized for built-in Windows groups ("Administratoren"
    for Administrators on German Windows).

    Example:
        >>> g = LocalGroup(name="Administrators", sid="S-1-5-32-544")
        >>> g.sid
        'S-1-5-32-544'
    """

    name: str
    sid: str
    description: str = ""


class AclEntry(PSRecord):
    """One access-control entry of a filesystem path (the native ``Get-Acl`` record; portable).

    One record per entry. The principal is identified by ``trustee_sid`` (stable,
    locale-independent): a Windows SID on Windows, or the POSIX **uid/gid** as a string on Linux
    (disambiguated by ``kind``); ``trustee_name`` is the readable name. ``owner_sid`` is the object
    owner (a SID, or the owner uid on POSIX).

    ``kind`` gives the entry category (see :class:`~pwshpy.domain.enums.AclEntryKind`). ``rights``
    is the raw platform permission value: a Win32 access mask on Windows, POSIX ``rwx`` bits
    (0-7) on Linux. ``permissions`` is the POSIX ``rwx`` symbolic string (e.g. ``"r-x"``) on Linux
    and empty on Windows, where ``rights`` (the mask) is authoritative - Windows rights do not
    reduce to ``rwx`` without losing detail. POSIX ACLs are allow-only, so ``access_type`` is
    always ``ALLOW`` there.

    Example:
        >>> e = AclEntry(path="P", owner_sid="S-1-5-18", trustee_sid="S-1-5-32-544",
        ...              access_type=AceType.ALLOW, rights=2032127)
        >>> e.trustee_sid, e.access_type.value, e.kind.value
        ('S-1-5-32-544', 'Allow', 'Trustee')
    """

    path: str
    owner_sid: str
    trustee_sid: str
    access_type: AceType
    rights: int
    trustee_name: str = ""
    inherited: bool = False
    kind: AclEntryKind = AclEntryKind.TRUSTEE
    permissions: str = ""


class PackedScript(PSRecord):
    """The manifest of a packed (or unpacked) self-extracting runner (``.ps1`` or ``.sh``).

    ``files`` lists the archive members as POSIX-style relative paths, sorted, so two
    packs of the same sources compare equal; a ``.sh`` lists its generated
    ``__pwshpy_pack_main__.py`` shim too.  ``payload_sha256`` is the digest of the
    embedded archive (a zip in a ``.ps1``, a tar in a ``.sh``): it keys the runner's
    extraction cache and anchors the integrity check the runner performs before executing
    anything.  ``uv_args`` are the arguments the runner hands ``uv run`` (``--with`` pairs).

    ``external_imports`` names the top-level imports that are neither standard library nor
    local, so a dependency missing from the entry's PEP 723 block surfaces at pack time
    rather than on the recipient's machine.  They are reported, never guessed at: an import
    name is usually not a distribution name (``yaml`` is PyYAML, ``cv2`` is opencv-python).
    ``has_script_metadata`` records whether the entry carried a PEP 723 block, which is what
    tells a caller that ``external_imports`` are declared somewhere rather than forgotten.

    Example:
        >>> m = PackedScript(output_path="tool.ps1", entry="tool.py", files=["tool.py"],
        ...                  payload_sha256="ab" * 32, payload_bytes=120)
        >>> m.entry, m.file_count, m.external_imports
        ('tool.py', 1, [])
    """

    output_path: str
    entry: str
    files: list[str]
    payload_sha256: str
    payload_bytes: int
    uv_args: list[str] = Field(default_factory=list)
    external_imports: list[str] = Field(default_factory=list)
    has_script_metadata: bool = False

    @property
    def file_count(self) -> int:
        """How many files the payload carries.

        Example:
            >>> PackedScript(output_path="o", entry="e", files=["a", "b"],
            ...              payload_sha256="x", payload_bytes=1).file_count
            2
        """
        return len(self.files)


class WindowsImage(PSRecord):
    """One image inside a WIM file or split ``.swm`` part (the native ``Get-WindowsImage`` record; portable).

    Read from the file's XML resource, so every field the XML may omit has an empty
    default rather than a guess.

    Attributes:
        index: The 1-based image index (``-Index``).
        name: The image name (``ImageName``).
        description: The image description (``ImageDescription``).
        edition_id: ``WINDOWS/EDITIONID``, e.g. ``Professional``.
        installation_type: ``WINDOWS/INSTALLATIONTYPE``, e.g. ``Client`` or ``Server``.
        architecture: The CPU architecture, :class:`~pwshpy.domain.enums.ImageArchitecture`.
        version: ``MAJOR.MINOR.BUILD.SPBUILD`` like ``Get-WindowsImage``'s ``Version``.
        build: The build number (``WINDOWS/VERSION/BUILD``).
        sp_build: The servicing build (``WINDOWS/VERSION/SPBUILD``).
        languages: The installed languages, in XML order.
        default_language: The default language tag.
        size_bytes: The image's total size in bytes (``TOTALBYTES``).

    Example:
        >>> WindowsImage(index=1, name="Pro", architecture=ImageArchitecture.X64, build=26200).build
        26200
    """

    index: int
    name: str
    description: str = ""
    edition_id: str = ""
    installation_type: str = ""
    architecture: ImageArchitecture = ImageArchitecture.UNKNOWN
    version: str = ""
    build: int = 0
    sp_build: int = 0
    languages: tuple[str, ...] = ()
    default_language: str = ""
    size_bytes: int = 0


class UpdateCategory(PSRecord):
    """One category of a Windows Update (part of :class:`WindowsUpdate`; the WUA ``ICategory``).

    ``category_id`` is the locale-independent identity: select Security or Definition updates
    by it, never by ``name``, which the Windows Update agent may localize.  ``type`` is the
    agent's own, untranslated kind identifier.  It is kept as the agent's string rather than
    an enum because Microsoft documents no closed set of values, and an enum would turn an
    unlisted kind into a guess.

    Attributes:
        category_id: The category GUID (``ICategory.CategoryID``), lower case, e.g.
            ``"0fa1201d-4330-4fa8-8ae9-b877473b6441"`` for the Security Updates classification.
        type: The category kind (``ICategory.Type``), e.g. ``"UpdateClassification"`` or
            ``"Product"``.
        name: The display name (``ICategory.Name``).

    Example:
        >>> UpdateCategory(category_id="0fa1201d-4330-4fa8-8ae9-b877473b6441",
        ...                type="UpdateClassification", name="Security Updates").type
        'UpdateClassification'
    """

    category_id: str
    type: str = ""
    name: str = ""


class WindowsUpdate(PSRecord):
    """One update from a Windows Update search (Microsoft.Update.Session; Windows-only).

    ``update_id`` plus ``revision_number`` is the update's stable identity.  ``title`` and
    ``categories`` are display text, localized by the Windows Update agent to the machine's
    language, so match on ``update_id`` or ``kb_article_ids``, never on them.  To select by
    category (Security Updates, Definition Updates, a product), match the ``category_id`` of an
    entry in ``category_details``.

    Attributes:
        update_id: The update's GUID (``Identity.UpdateID``).
        revision_number: The update's revision (``Identity.RevisionNumber``).
        title: The localized title.
        kb_article_ids: The KB numbers without the ``KB`` prefix, e.g. ``("5034441",)``.
        is_installed: Whether the update is installed.
        is_downloaded: Whether its files are already downloaded.
        is_hidden: Whether a user hid the update.
        is_mandatory: Whether the agent marks it mandatory.
        msrc_severity: The MSRC rating (``Critical``, ``Important``, ...), ``""`` when unrated.
        max_download_size: The largest download it may need, in bytes.
        categories: The localized category names.
        category_details: One :class:`UpdateCategory` per category (GUID, kind and name), in
            the same order as ``categories``.

    Example:
        >>> WindowsUpdate(update_id="u", title="t").kb_article_ids
        ()
    """

    update_id: str
    revision_number: int = 0
    title: str
    kb_article_ids: tuple[str, ...] = ()
    is_installed: bool = False
    is_downloaded: bool = False
    is_hidden: bool = False
    is_mandatory: bool = False
    msrc_severity: str = ""
    max_download_size: int = 0
    categories: tuple[str, ...] = ()
    category_details: tuple[UpdateCategory, ...] = ()


class ShortcutInfo(PSRecord):
    """A Windows shortcut (``.lnk``) as Windows stored it (the ``get_shortcut`` record).

    Each field holds what the shell's ``IShellLink`` reads back, so an unset field is ``""`` (an
    unset icon index ``0``), never None.  ``target`` is read with environment references
    expanded (``%SystemRoot%\\notepad.exe`` reads back as ``C:\\Windows\\notepad.exe``), the same
    value WScript's ``TargetPath`` reports; ``arguments``, ``working_directory`` and ``icon`` are
    stored and read back raw.  ``arguments`` is the single command-line string Windows keeps, not
    an argv list.

    Attributes:
        path: The ``.lnk`` file.
        target: What the shortcut runs or opens.
        arguments: The command-line arguments passed to ``target``.
        working_directory: The directory ``target`` starts in.
        icon: The file the icon is taken from.
        icon_index: Which icon in ``icon``; negative values select a resource id.
        description: The comment Explorer shows as the tooltip.
        run_as_administrator: Whether the shortcut starts ``target`` elevated (the link flag
            ``SLDF_RUNAS_USER``, Explorer's "Run as administrator" box).

    Example:
        >>> info = ShortcutInfo(path="C:\\\\Users\\\\me\\\\Desktop\\\\Tool.lnk", target="C:\\\\Tools\\\\tool.exe",
        ...                     arguments='--mode "a b"')
        >>> info.arguments, info.working_directory, info.icon_index, info.run_as_administrator
        ('--mode "a b"', '', 0, False)
    """

    path: str
    target: str
    arguments: str = ""
    working_directory: str = ""
    icon: str = ""
    icon_index: int = 0
    description: str = ""
    run_as_administrator: bool = False


__all__ = [
    "AclEntry",
    "CimInstance",
    "CommandInfo",
    "CommandParameter",
    "ComputerInfo",
    "ConnectionTest",
    "Credential",
    "CurrentIdentity",
    "FileSystemItem",
    "Hotfix",
    "DiskUsage",
    "DnsRecord",
    "EnvVar",
    "EventLogEntry",
    "LocalGroup",
    "LocalUser",
    "NetAdapter",
    "NetConnection",
    "NetIpAddress",
    "PackedScript",
    "PSInvocationResult",
    "PSObjectRecord",
    "PSRecord",
    "ProcessInfo",
    "ProcessResult",
    "RegistryKey",
    "RegistryValue",
    "ScheduledTaskInfo",
    "ServiceInfo",
    "ShortcutInfo",
    "SystemUptime",
    "UpdateCategory",
    "WebResponse",
    "WindowsImage",
    "WindowsUpdate",
]
