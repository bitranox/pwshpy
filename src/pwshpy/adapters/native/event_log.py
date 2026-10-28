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
    * :class:`PublisherMessages` - render event messages through per-provider publisher metadata.
"""

from __future__ import annotations

import contextlib
import importlib
from collections.abc import Generator, Iterator
from typing import Any

from defusedxml.ElementTree import fromstring as _xml_fromstring

from ...domain.errors import PlatformUnsupportedError
from ...domain.records import EventLogEntry
from .marshal import native_error, parse_event_time, to_event_level

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


def _parse_system(xml_text: str) -> Any:
    """Return the ``<System>`` element of one rendered event XML (``None`` if absent)."""
    return _xml_fromstring(xml_text).find(_NS + "System")


def _provider_name(system: Any) -> str:
    provider: Any = None if system is None else system.find(_NS + "Provider")
    return str(provider.get("Name", "")) if provider is not None else ""


def _to_entry(system: Any, log_name: str, message: str | None) -> EventLogEntry:
    """Marshal one event's ``<System>`` element into an :class:`EventLogEntry`."""
    if system is None:  # pragma: no cover - EvtRender always emits a <System> block
        return EventLogEntry.model_construct(
            log_name=log_name, record_id=0, event_id=0, level=to_event_level(0), provider_name="", message=message
        )
    time_created: Any = system.find(_NS + "TimeCreated")
    security: Any = system.find(_NS + "Security")
    return EventLogEntry.model_construct(
        log_name=_child_text(system, "Channel") or log_name,
        record_id=int(_child_text(system, "EventRecordID") or "0"),
        event_id=int(_child_text(system, "EventID") or "0"),
        level=to_event_level(int(_child_text(system, "Level") or "0")),
        provider_name=_provider_name(system),
        time_created=parse_event_time(time_created.get("SystemTime") if time_created is not None else None),
        machine_name=_child_text(system, "Computer"),
        user_id=security.get("UserID") if security is not None else None,
        message=message,
    )


def _close_quietly(handle: Any) -> None:
    """Release an EVT_HANDLE during cleanup.

    Runs in a ``finally``: a failing close must neither mask the exception that ended
    the stream nor stop the remaining handles from being released.
    """
    with contextlib.suppress(Exception):
        handle.Close()


class PublisherMessages:
    """Render event messages through each provider's publisher metadata, cached for one stream.

    ``EvtFormatMessage`` needs the provider's metadata handle to find the message table;
    called with ``None`` it returns a Win32 error text ("The system cannot find the file
    specified.") posing as the message.  So the metadata is opened once per provider and
    reused for every later event of that provider - a log holds events of a few hundred
    providers at most, which bounds the cache.  A provider whose metadata cannot be opened
    (uninstalled, no manifest) is remembered as such and renders ``None``, as does an event
    whose message is missing or has no resource for the current locale: the same cases in
    which ``Get-WinEvent`` reports no ``Message``.  Call :meth:`close` to release the handles.

    Example:
        >>> class _NoMetadata:
        ...     def EvtOpenPublisherMetadata(self, provider):
        ...         raise OSError(2, "not found")
        >>> messages = PublisherMessages(_NoMetadata())
        >>> messages.render("event-handle", "Gone-Provider") is None
        True
        >>> messages.close()
    """

    def __init__(self, win32evtlog: Any) -> None:
        self._api = win32evtlog
        self._metadata: dict[str, Any] = {}

    def render(self, event_handle: Any, provider: str) -> str | None:
        """Return the formatted message of one event, or ``None`` when it has none.

        Args:
            event_handle: The EVT_HANDLE of the event (from ``EvtNext``).
            provider: The event's ``<Provider Name=...>``.

        Returns:
            The provider's message text, or ``None`` when the provider has no metadata or
            the message cannot be formatted.  Never a Win32 error text.
        """
        metadata = self._open(provider)
        if metadata is None:
            return None
        try:
            text = self._api.EvtFormatMessage(metadata, event_handle, self._api.EvtFormatMessageEvent)
        except Exception:
            # Fail-soft by design: a missing message id (15027) or locale resource (15033)
            # is common and per-event; one unformattable event must not end the stream.
            return None
        return str(text) if text else None

    def _open(self, provider: str) -> Any:
        if provider in self._metadata:
            return self._metadata[provider]
        metadata: Any = None
        if provider:
            try:
                metadata = self._api.EvtOpenPublisherMetadata(provider)
            except Exception:
                # Cached as None, so a provider without metadata is asked once per stream.
                metadata = None
        self._metadata[provider] = metadata
        return metadata

    def close(self) -> None:
        """Release every publisher-metadata handle this renderer opened."""
        for metadata in self._metadata.values():
            if metadata is not None:
                _close_quietly(metadata)
        self._metadata.clear()


def iter_event_log(log_name: str, *, evtlog_api: Any | None = None) -> Generator[EventLogEntry, None, None]:
    """Stream a Windows event log's entries newest-first (like ``Get-WinEvent -LogName``).

    Reads in batches and yields one record at a time, so the pipeline stays
    memory-bounded even on a log with millions of entries.  Each entry's ``message`` is
    rendered through its provider's publisher metadata (see :class:`PublisherMessages`);
    the metadata and query handles are released when the stream ends or is abandoned.

    Args:
        log_name: The channel to read (``System``, ``Setup``, ``Microsoft-Windows-.../Operational``).
        evtlog_api: The ``win32evtlog`` module to call; ``None`` imports the real one.  Tests pass
            a fake here so the streaming and message-rendering logic runs on every OS.

    Raises:
        PlatformUnsupportedError: pywin32 is not installed (a non-Windows host).
        NativeCallError: the channel cannot be queried, read, or an event record rendered.

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import EventLogEntry
        >>> sys.platform != "win32" or isinstance(next(iter_event_log("System")), EventLogEntry)
        True
    """
    win32evtlog: Any = _load_win32evtlog() if evtlog_api is None else evtlog_api
    flags = win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection
    try:
        query = win32evtlog.EvtQuery(log_name, flags)
    except Exception as exc:
        raise native_error(str(exc) or f"cannot query event log {log_name!r}", exc) from exc
    messages = PublisherMessages(win32evtlog)
    try:
        yield from _stream(win32evtlog, query, log_name, messages)
    finally:
        messages.close()
        _close_quietly(query)


def _stream(win32evtlog: Any, query: Any, log_name: str, messages: PublisherMessages) -> Iterator[EventLogEntry]:
    """Pull ``query`` in batches of ``_BATCH`` and yield one marshaled entry per event."""
    while True:
        try:
            handles = win32evtlog.EvtNext(query, _BATCH)
        except Exception as exc:
            if getattr(exc, "winerror", None) == _ERROR_NO_MORE_ITEMS:
                return
            raise native_error(str(exc) or "EvtNext failed", exc) from exc
        if not handles:
            return
        for handle in handles:
            try:
                xml_text = str(win32evtlog.EvtRender(handle, win32evtlog.EvtRenderEventXml))
            except Exception as exc:
                raise native_error(str(exc) or "failed to render an event record", exc) from exc
            system = _parse_system(xml_text)
            yield _to_entry(system, log_name, messages.render(handle, _provider_name(system)))


__all__ = ["PublisherMessages", "iter_event_log"]
