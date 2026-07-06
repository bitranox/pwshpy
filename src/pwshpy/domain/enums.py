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


__all__ = [
    "AddressFamily",
    "ConnectionState",
    "DeployTarget",
    "OutputFormat",
    "ProcessStatus",
    "TransportProtocol",
]
