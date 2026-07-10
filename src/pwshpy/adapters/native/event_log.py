"""native event-log source over ``win32evtlog`` (Windows-only), streaming and memory-bounded.

Uses the modern Windows Event Log API (``EvtQuery`` + batched ``EvtNext`` +
``EvtRender``), so it matches ``Get-WinEvent``'s channel coverage.  The adapter is
a GENERATOR that reads events in small batches and yields one
:class:`~pwshpy.domain.records.EventLogEntry` at a time, so the lazy Pipeline plus
``.take(N)`` never materialize a whole log - which can hold millions of entries.
``pywin32`` is imported lazily so a portable install stays clean and raises
:class:`~pwshpy.domain.errors.PlatformUnsupportedError`; the event XML is parsed
with ``defusedxml``.

Contents:
    * :func:`iter_event_log` - stream a named log's entries, newest first.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

from defusedxml.ElementTree import fromstring as _xml_fromstring

from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import EventLogEntry
from .marshal import parse_event_time, to_event_level

_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
_BATCH = 64
_ERROR_NO_MORE_ITEMS = 259


def _load_win32evtlog() -> Any:
    """Import ``win32evtlog`` lazily; absent means a portable (non-Windows) install."""
    try:
        return importlib.import_module("win32evtlog")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "The event-log subsystem requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc


def _child_text(node: Any, tag: str) -> str:
    child = node.find(_NS + tag)
    text = None if child is None else child.text
    return "" if text is None else str(text)


def _to_entry(xml_text: str, log_name: str, message: str | None) -> EventLogEntry:
    """Marshal one rendered event XML into an :class:`EventLogEntry`."""
    system: Any = _xml_fromstring(xml_text).find(_NS + "System")
    if system is None:  # pragma: no cover - EvtRender always emits a <System> block
        return EventLogEntry.model_construct(
            log_name=log_name, record_id=0, event_id=0, level=to_event_level(0), provider_name="", message=message
        )
    provider: Any = system.find(_NS + "Provider")
    time_created: Any = system.find(_NS + "TimeCreated")
    security: Any = system.find(_NS + "Security")
    return EventLogEntry.model_construct(
        log_name=_child_text(system, "Channel") or log_name,
        record_id=int(_child_text(system, "EventRecordID") or "0"),
        event_id=int(_child_text(system, "EventID") or "0"),
        level=to_event_level(int(_child_text(system, "Level") or "0")),
        provider_name=str(provider.get("Name", "")) if provider is not None else "",
        time_created=parse_event_time(time_created.get("SystemTime") if time_created is not None else None),
        machine_name=_child_text(system, "Computer"),
        user_id=security.get("UserID") if security is not None else None,
        message=message,
    )


def _render_message(win32evtlog: Any, handle: Any) -> str | None:
    """Render the provider-formatted event message, or ``None`` when unavailable."""
    try:
        text = win32evtlog.EvtFormatMessage(None, handle, win32evtlog.EvtFormatMessageEvent)
    except Exception:
        return None
    return str(text) if text else None


def iter_event_log(log_name: str) -> Iterator[EventLogEntry]:
    """Stream a Windows event log's entries newest-first (like ``Get-WinEvent -LogName``).

    Reads in batches and yields one record at a time, so the pipeline stays
    memory-bounded even on a log with millions of entries.

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import EventLogEntry
        >>> sys.platform != "win32" or isinstance(next(iter_event_log("System")), EventLogEntry)
        True
    """
    win32evtlog: Any = _load_win32evtlog()
    flags = win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection
    try:
        query = win32evtlog.EvtQuery(log_name, flags)
    except Exception as exc:
        raise NativeCallError(str(exc) or f"cannot query event log {log_name!r}") from exc
    while True:
        try:
            handles = win32evtlog.EvtNext(query, _BATCH)
        except Exception as exc:
            if getattr(exc, "winerror", None) == _ERROR_NO_MORE_ITEMS:
                return
            raise NativeCallError(str(exc) or "EvtNext failed") from exc
        if not handles:
            return
        for handle in handles:
            try:
                xml_text = str(win32evtlog.EvtRender(handle, win32evtlog.EvtRenderEventXml))
            except Exception as exc:
                raise NativeCallError(str(exc) or "failed to render an event record") from exc
            yield _to_entry(xml_text, log_name, _render_message(win32evtlog, handle))


__all__ = ["iter_event_log"]
