"""Native event log on Linux via the systemd journal (journald).

The portable counterpart to the win32evtlog adapter: reads system events from journald.
It **streams** through a journald cursor (one entry at a time, newest first), so it holds
the memory-bounded discipline exactly - ``get_win_event(...).take(10)`` reads only the ten
most-recent entries no matter how large the journal.

``systemd.journal`` (the ``systemd-python`` package) is Linux-only, so it is loaded lazily
through ``importlib`` and funnelled through an ``Any`` alias (the same seam the registry /
pwd-grp adapters use): importing this module is safe on Windows and macOS, and calling it
off Linux raises :class:`PlatformUnsupportedError`.

Contents:
    * :func:`to_event_level` - syslog PRIORITY (0-7) -> canonical :class:`EventLevel` (pure).
    * :func:`to_entry` - one journal-entry dict -> :class:`EventLogEntry` (pure).
    * :func:`iter_event_log` - stream the journal newest-first.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Any

from ...domain.enums import EventLevel
from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import EventLogEntry


def _load_journal() -> Any:
    """Import ``systemd.journal`` (the real one), working around lib_log_rich's stub.

    lib_log_rich registers an empty ``systemd`` / ``systemd.journal`` in ``sys.modules``
    (its optional-journald-backend handling) that has no ``Reader`` and shadows the real
    ``systemd-python`` package. If the cached module lacks ``Reader``, evict that stub and
    load the real package (lib_log_rich keeps its own reference, so it is unaffected).
    """
    cached = sys.modules.get("systemd.journal")
    if cached is not None and hasattr(cached, "Reader"):
        return cached
    sys.modules.pop("systemd.journal", None)
    sys.modules.pop("systemd", None)
    try:
        journal = importlib.import_module("systemd.journal")
    except ImportError as exc:  # pragma: no cover - only off Linux / without systemd-python
        raise PlatformUnsupportedError(
            "the event log needs Windows (win32evtlog), or Linux with the journald extra "
            "(pip install 'pwshpy[journald]')."
        ) from exc
    if not hasattr(journal, "Reader"):  # pragma: no cover - defensive
        raise PlatformUnsupportedError("systemd.journal has no Reader; reinstall the systemd-python package.")
    return journal


# syslog PRIORITY levels (RFC 5424): 0 emerg .. 7 debug.
_CRIT_MAX = 2  # emerg / alert / crit
_ERR = 3
_WARNING = 4
_INFO_MAX = 6  # notice / info (5 and 6)
_DEFAULT_PRIORITY = 6  # info, when an entry carries no PRIORITY field


def to_event_level(priority: int) -> EventLevel:
    """Map a syslog ``PRIORITY`` (0-7) to the canonical :class:`EventLevel`.

    Example:
        >>> to_event_level(3).value, to_event_level(4).value, to_event_level(6).value
        ('Error', 'Warning', 'Information')
    """
    if priority <= _CRIT_MAX:
        return EventLevel.CRITICAL
    if priority == _ERR:
        return EventLevel.ERROR
    if priority == _WARNING:
        return EventLevel.WARNING
    if priority <= _INFO_MAX:
        return EventLevel.INFORMATION
    return EventLevel.VERBOSE  # 7 debug


def to_entry(log_name: str, entry: Mapping[str, Any]) -> EventLogEntry:
    """Marshal one journal-entry mapping into an :class:`EventLogEntry`.

    journald exposes no stable numeric record/event id (its identity is the string cursor),
    so ``record_id`` and ``event_id`` are 0; the useful fields - time, level, provider, host,
    uid, message - all map. The timestamp is normalized to aware UTC.

    Example:
        >>> from datetime import datetime, timezone
        >>> e = to_entry("System", {"PRIORITY": 3, "MESSAGE": "boom", "SYSLOG_IDENTIFIER": "sshd",
        ...     "_HOSTNAME": "box", "_UID": 0,
        ...     "__REALTIME_TIMESTAMP": datetime(2026, 1, 1, tzinfo=timezone.utc)})
        >>> e.level.value, e.provider_name, e.user_id, e.message
        ('Error', 'sshd', '0', 'boom')
    """
    timestamp = entry.get("__REALTIME_TIMESTAMP")
    time_created = timestamp.astimezone(timezone.utc) if isinstance(timestamp, datetime) else None
    priority = entry.get("PRIORITY", _DEFAULT_PRIORITY)
    uid = entry.get("_UID")
    return EventLogEntry(
        log_name=log_name,
        record_id=0,
        event_id=0,
        level=to_event_level(int(priority) if priority is not None else _DEFAULT_PRIORITY),
        provider_name=str(entry.get("SYSLOG_IDENTIFIER") or entry.get("_SYSTEMD_UNIT") or entry.get("_COMM") or ""),
        time_created=time_created,
        machine_name=str(entry.get("_HOSTNAME") or ""),
        user_id=None if uid is None else str(uid),
        message=entry.get("MESSAGE"),
    )


def iter_event_log(log_name: str) -> Iterator[EventLogEntry]:
    """Stream the systemd journal newest-first as :class:`EventLogEntry` records (like Get-WinEvent).

    ``log_name`` is a systemd unit filter (``"sshd"`` / ``"nginx.service"``); ``"System"`` or
    an empty string reads the whole journal. Reading the system journal needs the
    ``systemd-journal`` group or root.

    Example:
        >>> import sys
        >>> callable(iter_event_log)
        True
    """
    journal = _load_journal()
    try:
        reader = journal.Reader()
    except OSError as exc:
        raise NativeCallError(f"cannot open the systemd journal: {exc}") from exc
    try:
        if log_name and log_name not in ("System", "*"):
            unit = log_name if "." in log_name else f"{log_name}.service"
            reader.add_match(_SYSTEMD_UNIT=unit)
        reader.seek_tail()
        while True:
            entry = reader.get_previous()
            if not entry:
                break
            yield to_entry(log_name, entry)
    except OSError as exc:
        raise NativeCallError(f"error reading the systemd journal: {exc}") from exc
    finally:
        reader.close()


__all__ = ["iter_event_log", "to_entry", "to_event_level"]
