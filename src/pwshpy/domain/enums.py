"""Type-safe domain enums for output formats and deployment targets."""

from __future__ import annotations

from enum import Enum


class OutputFormat(str, Enum):
    """Output format options for configuration display.

    Defines valid output format choices for the config command.
    Inherits from str to allow direct string comparison and Click integration.

    Attributes:
        HUMAN: Human-readable TOML-like output format.
        JSON: Machine-readable JSON output format.

    Example:
        >>> OutputFormat.HUMAN.value
        'human'
        >>> OutputFormat.JSON == "json"
        True
    """

    HUMAN = "human"
    JSON = "json"


class DeployTarget(str, Enum):
    """Configuration deployment target layers.

    Defines valid target layers for configuration file deployment.
    Inherits from str to allow direct string comparison and Click integration.

    Attributes:
        APP: System-wide application configuration (requires privileges).
        HOST: System-wide host-specific configuration (requires privileges).
        USER: User-specific configuration (~/.config on Linux).

    Example:
        >>> DeployTarget.USER.value
        'user'
        >>> DeployTarget.APP == "app"
        True
    """

    APP = "app"
    HOST = "host"
    USER = "user"


class RunnerFormat(str, Enum):
    """Which self-extracting runner ``pack_script`` emits.

    ``PS1`` is a PowerShell runner (Windows PowerShell 5.1 and pwsh 7); ``SH`` is a strict-POSIX
    ``/bin/sh`` runner that works under every major shell's ``sh`` (dash, busybox ash, macOS sh,
    bash). ``AUTO`` picks by the output file's extension (``.sh`` -> SH, else PS1).

    Example:
        >>> RunnerFormat.SH.value
        'sh'
        >>> RunnerFormat("ps1") is RunnerFormat.PS1
        True
    """

    PS1 = "ps1"
    SH = "sh"
    AUTO = "auto"


class ProcessStatus(str, Enum):
    """Operating-system process states, as reported by the native binding.

    Values mirror ``psutil``'s ``STATUS_*`` constants (a fixed, well-known set),
    so the strict-data rule is honored — process state is an enum, never a raw
    string.  Unrecognized states marshal to :attr:`UNKNOWN` rather than raising.

    Example:
        >>> ProcessStatus.RUNNING.value
        'running'
        >>> ProcessStatus("sleeping") is ProcessStatus.SLEEPING
        True
    """

    RUNNING = "running"
    SLEEPING = "sleeping"
    DISK_SLEEP = "disk-sleep"
    STOPPED = "stopped"
    TRACING_STOP = "tracing-stop"
    ZOMBIE = "zombie"
    DEAD = "dead"
    WAKE_KILL = "wake-kill"
    WAKING = "waking"
    PARKED = "parked"
    IDLE = "idle"
    LOCKED = "locked"
    WAITING = "waiting"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


class AddressFamily(str, Enum):
    """IP address family of a resolved DNS record.

    Example:
        >>> AddressFamily.IPV4.value
        'IPv4'
    """

    IPV4 = "IPv4"
    IPV6 = "IPv6"
    OTHER = "other"


class TransportProtocol(str, Enum):
    """Transport-layer protocol of a network connection.

    Example:
        >>> TransportProtocol.TCP.value
        'tcp'
    """

    TCP = "tcp"
    UDP = "udp"


class ConnectionState(str, Enum):
    """TCP connection state, as reported by the native binding.

    Values mirror ``psutil``'s ``CONN_*`` constants (a fixed set); UDP sockets
    and unrecognized states marshal to :attr:`NONE`.

    Example:
        >>> ConnectionState.LISTEN.value
        'LISTEN'
        >>> ConnectionState("ESTABLISHED") is ConnectionState.ESTABLISHED
        True
    """

    ESTABLISHED = "ESTABLISHED"
    SYN_SENT = "SYN_SENT"
    SYN_RECV = "SYN_RECV"
    FIN_WAIT1 = "FIN_WAIT1"
    FIN_WAIT2 = "FIN_WAIT2"
    TIME_WAIT = "TIME_WAIT"
    CLOSE = "CLOSE"
    CLOSE_WAIT = "CLOSE_WAIT"
    LAST_ACK = "LAST_ACK"
    LISTEN = "LISTEN"
    CLOSING = "CLOSING"
    NONE = "NONE"


class RegistryHive(str, Enum):
    """A Windows registry root hive.

    A ``str`` enum keyed by the short hive name, so a record serializes the
    hive as ``"HKLM"`` (how regedit / PowerShell present it) rather than the
    opaque numeric win32 ``HKEY_*`` constant.

    Example:
        >>> RegistryHive.HKLM.value
        'HKLM'
        >>> RegistryHive("HKCU") is RegistryHive.HKCU
        True
    """

    HKCR = "HKCR"
    HKCU = "HKCU"
    HKLM = "HKLM"
    HKU = "HKU"
    HKCC = "HKCC"


class RegistryValueType(str, Enum):
    """The type of a registry value (the win32 ``REG_*`` kinds).

    A ``str`` enum keyed by the canonical ``REG_*`` name so a record serializes
    the type as ``"REG_SZ"`` (matching regedit / PowerShell) rather than the raw
    numeric constant.  Unknown native types marshal to :attr:`REG_NONE`.

    Example:
        >>> RegistryValueType.REG_SZ.value
        'REG_SZ'
        >>> RegistryValueType("REG_DWORD") is RegistryValueType.REG_DWORD
        True
    """

    REG_NONE = "REG_NONE"
    REG_SZ = "REG_SZ"
    REG_EXPAND_SZ = "REG_EXPAND_SZ"
    REG_BINARY = "REG_BINARY"
    REG_DWORD = "REG_DWORD"
    REG_DWORD_BIG_ENDIAN = "REG_DWORD_BIG_ENDIAN"
    REG_LINK = "REG_LINK"
    REG_MULTI_SZ = "REG_MULTI_SZ"
    REG_RESOURCE_LIST = "REG_RESOURCE_LIST"
    REG_FULL_RESOURCE_DESCRIPTOR = "REG_FULL_RESOURCE_DESCRIPTOR"
    REG_RESOURCE_REQUIREMENTS_LIST = "REG_RESOURCE_REQUIREMENTS_LIST"
    REG_QWORD = "REG_QWORD"


class ServiceState(str, Enum):
    """Current state of a Windows service (mirrors ``Get-Service`` ``Status``).

    A ``str`` enum keyed by the .NET ``ServiceControllerStatus`` name so a record
    serializes as ``"Running"`` (as Get-Service shows it), not the numeric win32
    ``SERVICE_*`` state.

    Example:
        >>> ServiceState.RUNNING.value
        'Running'
    """

    STOPPED = "Stopped"
    START_PENDING = "StartPending"
    STOP_PENDING = "StopPending"
    RUNNING = "Running"
    CONTINUE_PENDING = "ContinuePending"
    PAUSE_PENDING = "PausePending"
    PAUSED = "Paused"


class ServiceStartType(str, Enum):
    """Start mode of a Windows service (mirrors ``Get-Service`` ``StartType``).

    A ``str`` enum keyed by the .NET ``ServiceStartMode`` name.

    Example:
        >>> ServiceStartType.AUTOMATIC.value
        'Automatic'
    """

    BOOT = "Boot"
    SYSTEM = "System"
    AUTOMATIC = "Automatic"
    MANUAL = "Manual"
    DISABLED = "Disabled"


class ServiceKind(str, Enum):
    """Service type of a Windows service (mirrors ``Get-Service`` ``ServiceType``).

    A ``str`` enum keyed by the .NET ``ServiceType`` name (the primary process /
    driver type; the interactive-process flag is not represented separately).

    Example:
        >>> ServiceKind.WIN32_SHARE_PROCESS.value
        'Win32ShareProcess'
    """

    KERNEL_DRIVER = "KernelDriver"
    FILE_SYSTEM_DRIVER = "FileSystemDriver"
    ADAPTER = "Adapter"
    RECOGNIZER_DRIVER = "RecognizerDriver"
    WIN32_OWN_PROCESS = "Win32OwnProcess"
    WIN32_SHARE_PROCESS = "Win32ShareProcess"
    INTERACTIVE_PROCESS = "InteractiveProcess"


class EventLevel(str, Enum):
    """Severity level of a Windows event (mirrors the .NET ``StandardEventLevel``).

    A ``str`` enum keyed by the canonical English level name.  ``Get-WinEvent``'s
    ``LevelDisplayName`` is localized (e.g. German ``Informationen``), so pwshpy
    maps the numeric event ``Level`` to this stable name instead.

    Example:
        >>> EventLevel.INFORMATION.value
        'Information'
    """

    LOG_ALWAYS = "LogAlways"
    CRITICAL = "Critical"
    ERROR = "Error"
    WARNING = "Warning"
    INFORMATION = "Information"
    VERBOSE = "Verbose"


class TaskState(str, Enum):
    """State of a Windows scheduled task (mirrors the Task Scheduler ``TASK_STATE``).

    Example:
        >>> TaskState.READY.value
        'Ready'
    """

    UNKNOWN = "Unknown"
    DISABLED = "Disabled"
    QUEUED = "Queued"
    READY = "Ready"
    RUNNING = "Running"


class WellKnownSid(str, Enum):
    """Stable, locale-independent SIDs for well-known Windows principals.

    The enum value **is** the SID string, so a locale-independent filter reads
    ``record.sid == WellKnownSid.ADMINISTRATORS`` even where the localized name is
    ``Administratoren``.  See ``docs/locale-and-identity.md``.

    Example:
        >>> WellKnownSid.ADMINISTRATORS.value
        'S-1-5-32-544'
    """

    EVERYONE = "S-1-1-0"
    LOCAL_SYSTEM = "S-1-5-18"
    LOCAL_SERVICE = "S-1-5-19"
    NETWORK_SERVICE = "S-1-5-20"
    AUTHENTICATED_USERS = "S-1-5-11"
    ADMINISTRATORS = "S-1-5-32-544"
    USERS = "S-1-5-32-545"
    GUESTS = "S-1-5-32-546"
    POWER_USERS = "S-1-5-32-547"
    REMOTE_DESKTOP_USERS = "S-1-5-32-555"


class AceType(str, Enum):
    """Type of a DACL access-control entry: grant or deny.

    Example:
        >>> AceType.ALLOW.value
        'Allow'
    """

    ALLOW = "Allow"
    DENY = "Deny"


class AclEntryKind(str, Enum):
    """The category of an ACL entry - portable across Windows DACLs and POSIX ACLs.

    A Windows DACL ACE is always a ``TRUSTEE`` (a SID granted/denied rights). POSIX ACLs split
    into the owning user (``OWNER``), named users (``USER``), the owning group (``OWNING_GROUP``),
    named groups (``GROUP``), the ``MASK`` (which caps the effective rights of named entries and
    the owning group), and ``OTHER`` (everyone else).

    Example:
        >>> AclEntryKind.OWNER.value, AclEntryKind.TRUSTEE.value
        ('Owner', 'Trustee')
    """

    TRUSTEE = "Trustee"
    OWNER = "Owner"
    USER = "User"
    OWNING_GROUP = "OwningGroup"
    GROUP = "Group"
    MASK = "Mask"
    OTHER = "Other"


class FileItemType(str, Enum):
    """What ``new_item`` creates: a file or a directory (like New-Item -ItemType).

    Example:
        >>> FileItemType.DIRECTORY.value
        'directory'
        >>> FileItemType("file") is FileItemType.FILE
        True
    """

    FILE = "file"
    DIRECTORY = "directory"


class ImportKind(str, Enum):
    """Where an imported module comes from, as seen by the script packer.

    Only :attr:`LOCAL` modules are embedded in a pack: the standard library
    travels with the interpreter uv provisions, and third-party distributions
    are declared in the entry's PEP 723 block (or via ``--with``), never guessed
    from an import name.

    Attributes:
        STDLIB: Part of the standard library for the packing interpreter.
        LOCAL: A module resolvable to a file under the pack root; gets embedded.
        EXTERNAL: Neither of the above; left to uv's dependency resolution.

    Example:
        >>> ImportKind.LOCAL.value
        'local'
        >>> ImportKind("stdlib") is ImportKind.STDLIB
        True
    """

    STDLIB = "stdlib"
    LOCAL = "local"
    EXTERNAL = "external"


__all__ = [
    "AceType",
    "AclEntryKind",
    "AddressFamily",
    "ConnectionState",
    "DeployTarget",
    "EventLevel",
    "FileItemType",
    "ImportKind",
    "OutputFormat",
    "ProcessStatus",
    "RunnerFormat",
    "RegistryHive",
    "RegistryValueType",
    "ServiceKind",
    "ServiceStartType",
    "ServiceState",
    "TaskState",
    "TransportProtocol",
    "WellKnownSid",
]
