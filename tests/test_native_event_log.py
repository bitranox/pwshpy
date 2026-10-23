"""Event-log native adapter over win32evtlog.

A fake ``win32evtlog`` (passed in through ``evtlog_api``) exercises the XML
marshaling AND the streaming/laziness contract deterministically on every OS; a
real structural test reads the System log on Windows.  Exact live behaviour is
pinned against Get-WinEvent in ``test_event_log_pwsh_oracle``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

import pytest

from pwshpy.adapters.native.event_log import iter_event_log
from pwshpy.domain.enums import EventLevel
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import EventLogEntry

_NS = "xmlns='http://schemas.microsoft.com/win/2004/08/events/event'"
_XML_1 = (
    f"<Event {_NS}><System><Provider Name='Test-Provider'/><EventID>7001</EventID><Level>2</Level>"
    "<TimeCreated SystemTime='2026-07-07T15:03:14.2206901Z'/><EventRecordID>42</EventRecordID>"
    "<Channel>System</Channel><Computer>host</Computer><Security UserID='S-1-5-18'/></System><EventData/></Event>"
)
_XML_2 = (
    f"<Event {_NS}><System><Provider Name='Other'/><EventID>10</EventID><Level>4</Level>"
    "<TimeCreated SystemTime='2026-07-07T15:00:00.0000000Z'/><EventRecordID>43</EventRecordID>"
    "<Channel>System</Channel><Computer>host</Computer></System><EventData/></Event>"
)


def _event_xml(record_id: int, provider: str) -> str:
    return (
        f"<Event {_NS}><System><Provider Name='{provider}'/><EventID>1</EventID><Level>4</Level>"
        f"<EventRecordID>{record_id}</EventRecordID><Channel>Setup</Channel></System><EventData/></Event>"
    )


class _NoMoreItemsError(Exception):
    """Stands in for the win32 ERROR_NO_MORE_ITEMS that ends an EvtNext stream."""

    winerror = 259


class _Win32Error(Exception):
    """Stands in for a ``pywintypes.error`` carrying a Win32 code."""

    def __init__(self, winerror: int) -> None:
        super().__init__(winerror)
        self.winerror = winerror


class _FakeHandle:
    """An EVT_HANDLE stand-in that records whether it was closed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    def Close(self) -> None:  # noqa: N802 - pywin32 handle API name
        self.closed = True


# What real pywin32 EvtFormatMessage(None, ...) returned on a German Windows for
# Setup and System events (measured on a Windows 11 test VM, 2026-10-21): a Win32 error text
# posing as the message.  The fake reproduces it so a renderer that skips the
# publisher metadata yields this string instead of the provider's text.
_NO_METADATA_GARBAGE = "Unzulaessige Funktion. "
_ERROR_EVT_MESSAGE_NOT_FOUND = 15027
_ERROR_FILE_NOT_FOUND = 2


class _FakeWin32Evtlog:
    """A minimal streaming stand-in for the ``win32evtlog`` module.

    ``metadata`` maps a provider name to the message text per event handle; a provider
    missing from it has no publisher metadata (EvtOpenPublisherMetadata fails, as for an
    uninstalled provider), and a handle missing from its dict has no message in the table.
    """

    EvtQueryChannelPath = 1
    EvtQueryReverseDirection = 512
    EvtRenderEventXml = 1
    EvtFormatMessageEvent = 1

    def __init__(
        self,
        events: list[tuple[str, str]] | None = None,
        metadata: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._events = events if events is not None else [("H1", _XML_1), ("H2", _XML_2)]
        self._metadata = (
            metadata if metadata is not None else {"Test-Provider": {"H1": "the message"}, "Other": {"H2": ""}}
        )
        self._served = False
        self.render_count = 0
        self.metadata_attempts: list[str] = []
        self.opened: list[_FakeHandle] = []
        self.query_handle: _FakeHandle | None = None

    def EvtQuery(self, path: str, flags: int) -> _FakeHandle:  # noqa: N802 - win32 API name
        self.query_handle = _FakeHandle(f"Q-{path}")
        return self.query_handle

    def EvtNext(self, query: Any, count: int) -> list[str]:  # noqa: N802 - win32 API name
        if self._served:
            raise _NoMoreItemsError
        self._served = True
        return [handle for handle, _ in self._events]

    def EvtRender(self, handle: str, flag: int) -> str:  # noqa: N802 - win32 API name
        self.render_count += 1
        return dict(self._events)[handle]

    def EvtOpenPublisherMetadata(self, publisher: str) -> _FakeHandle:  # noqa: N802 - win32 API name
        self.metadata_attempts.append(publisher)
        if publisher not in self._metadata:
            raise _Win32Error(_ERROR_FILE_NOT_FOUND)
        handle = _FakeHandle(publisher)
        self.opened.append(handle)
        return handle

    def EvtFormatMessage(self, meta: _FakeHandle | None, handle: str, flag: int) -> str:  # noqa: N802 - win32 API name
        if meta is None:
            return _NO_METADATA_GARBAGE
        assert not meta.closed, "formatted through a publisher-metadata handle that was already closed"
        texts = self._metadata[meta.name]
        if handle not in texts:
            raise _Win32Error(_ERROR_EVT_MESSAGE_NOT_FOUND)
        return texts[handle]


@pytest.mark.os_agnostic
def test_iter_event_log_streams_and_marshals() -> None:
    """The adapter parses the event XML into EventLogEntry records and ends cleanly."""
    entries = list(iter_event_log("System", evtlog_api=_FakeWin32Evtlog()))
    assert len(entries) == 2

    first = entries[0]
    assert isinstance(first, EventLogEntry)
    assert first.record_id == 42
    assert first.event_id == 7001
    assert first.level is EventLevel.ERROR
    assert first.provider_name == "Test-Provider"
    assert first.log_name == "System"
    assert first.machine_name == "host"
    assert first.user_id == "S-1-5-18"
    assert first.message == "the message"
    assert first.time_created == datetime(2026, 7, 7, 15, 3, 14, 220690, tzinfo=timezone.utc)

    second = entries[1]  # no <Security>, empty formatted message -> None
    assert second.record_id == 43
    assert second.level is EventLevel.INFORMATION
    assert second.user_id is None
    assert second.message is None


@pytest.mark.os_agnostic
def test_iter_event_log_is_lazy_and_memory_bounded() -> None:
    """Consuming one entry renders exactly one event - the stream never slurps the log."""
    fake = _FakeWin32Evtlog()
    first = next(iter_event_log("System", evtlog_api=fake))
    assert first.record_id == 42
    assert fake.render_count == 1  # the second event was NOT rendered until requested


class _ReadError(Exception):
    """A non-ERROR_NO_MORE_ITEMS EvtNext failure (e.g. ACCESS_DENIED mid-stream)."""

    winerror = 5


@pytest.mark.os_agnostic
def test_iter_event_log_wraps_query_failure() -> None:
    """A failed EvtQuery (missing channel / access denied) surfaces as NativeCallError."""

    class _BadQuery(_FakeWin32Evtlog):
        def EvtQuery(self, path: str, flags: int) -> _FakeHandle:  # noqa: N802 - win32 API name
            raise RuntimeError("no such channel")

    api = _BadQuery()
    with pytest.raises(NativeCallError):
        list(iter_event_log("System", evtlog_api=api))


@pytest.mark.os_agnostic
def test_iter_event_log_wraps_read_failure() -> None:
    """An EvtNext failure other than ERROR_NO_MORE_ITEMS surfaces as NativeCallError."""

    class _BadNext(_FakeWin32Evtlog):
        def EvtNext(self, query: Any, count: int) -> list[str]:  # noqa: N802 - win32 API name
            raise _ReadError

    api = _BadNext()
    with pytest.raises(NativeCallError):
        list(iter_event_log("System", evtlog_api=api))


@pytest.mark.os_agnostic
def test_iter_event_log_ends_on_empty_batch() -> None:
    """An empty EvtNext batch ends the stream cleanly (distinct from the no-more-items exception)."""

    class _EmptyNext(_FakeWin32Evtlog):
        def EvtNext(self, query: Any, count: int) -> list[str]:  # noqa: N802 - win32 API name
            return []

    api = _EmptyNext()
    assert list(iter_event_log("System", evtlog_api=api)) == []


@pytest.mark.os_agnostic
def test_iter_event_log_wraps_render_failure() -> None:
    """An unreadable event record (EvtRender raises) surfaces as NativeCallError."""

    class _BadRender(_FakeWin32Evtlog):
        def EvtRender(self, handle: str, flag: int) -> str:  # noqa: N802 - win32 API name
            raise RuntimeError("corrupt record")

    api = _BadRender()
    with pytest.raises(NativeCallError):
        list(iter_event_log("System", evtlog_api=api))


@pytest.mark.os_agnostic
def test_message_render_degrades_on_exception() -> None:
    """A provider with no resolvable message table yields message=None, not an error."""

    class _BadMessage(_FakeWin32Evtlog):
        def EvtFormatMessage(self, meta: Any, handle: str, flag: int) -> str:  # noqa: N802 - win32 API name
            raise RuntimeError("no message table")

    api = _BadMessage()
    entries = list(iter_event_log("System", evtlog_api=api))
    assert entries
    assert all(entry.message is None for entry in entries)


@pytest.mark.os_agnostic
def test_message_is_rendered_through_the_provider_metadata() -> None:
    """The message is the provider's text, never what EvtFormatMessage gives without metadata."""
    api = _FakeWin32Evtlog(
        events=[("E1", _event_xml(1, "Microsoft-Windows-Servicing"))],
        metadata={
            "Microsoft-Windows-Servicing": {"E1": "Package KB1 was successfully changed to the Installed state."}
        },
    )
    (entry,) = iter_event_log("Setup", evtlog_api=api)
    assert entry.message == "Package KB1 was successfully changed to the Installed state."


@pytest.mark.os_agnostic
def test_publisher_metadata_is_opened_once_per_provider_per_stream() -> None:
    """Many events of one provider share one metadata handle; each provider opens its own."""
    api = _FakeWin32Evtlog(
        events=[
            ("E1", _event_xml(1, "A")),
            ("E2", _event_xml(2, "B")),
            ("E3", _event_xml(3, "A")),
            ("E4", _event_xml(4, "A")),
        ],
        metadata={"A": {"E1": "a1", "E3": "a3", "E4": "a4"}, "B": {"E2": "b2"}},
    )
    messages = [entry.message for entry in iter_event_log("Setup", evtlog_api=api)]
    assert messages == ["a1", "b2", "a3", "a4"]
    assert api.metadata_attempts == ["A", "B"]


@pytest.mark.os_agnostic
def test_provider_without_metadata_yields_none_and_is_not_reopened() -> None:
    """No publisher metadata -> message None (never the no-metadata error text), tried once."""
    api = _FakeWin32Evtlog(
        events=[("E1", _event_xml(1, "Gone")), ("E2", _event_xml(2, "Gone"))],
        metadata={},
    )
    messages = [entry.message for entry in iter_event_log("Setup", evtlog_api=api)]
    assert messages == [None, None]
    assert api.metadata_attempts == ["Gone"]


@pytest.mark.os_agnostic
def test_message_missing_from_the_provider_table_yields_none() -> None:
    """A provider WITH metadata but no message for this event id renders None."""
    api = _FakeWin32Evtlog(events=[("E1", _event_xml(1, "A"))], metadata={"A": {}})
    (entry,) = iter_event_log("Setup", evtlog_api=api)
    assert entry.message is None


@pytest.mark.os_agnostic
def test_metadata_and_query_handles_are_closed_when_the_stream_ends() -> None:
    """Every opened publisher-metadata handle and the query handle are released at the end."""
    api = _FakeWin32Evtlog(
        events=[("E1", _event_xml(1, "A")), ("E2", _event_xml(2, "B"))],
        metadata={"A": {"E1": "a"}, "B": {"E2": "b"}},
    )
    assert len(list(iter_event_log("Setup", evtlog_api=api))) == 2
    assert [handle.closed for handle in api.opened] == [True, True]
    assert api.query_handle is not None and api.query_handle.closed


@pytest.mark.os_agnostic
def test_metadata_handles_are_closed_when_the_stream_is_abandoned() -> None:
    """A consumer that stops early (take(N)) still releases the handles when the generator closes."""
    api = _FakeWin32Evtlog(
        events=[("E1", _event_xml(1, "A")), ("E2", _event_xml(2, "A"))],
        metadata={"A": {"E1": "a1", "E2": "a2"}},
    )
    stream = iter_event_log("Setup", evtlog_api=api)
    assert next(stream).message == "a1"
    stream.close()
    assert [handle.closed for handle in api.opened] == [True]
    assert api.query_handle is not None and api.query_handle.closed


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="win32evtlog is Windows-only")
def test_iter_event_log_reads_real_system_log() -> None:
    """Against the real System channel, the adapter streams typed records lazily."""
    entries = [entry for _, entry in zip(range(5), iter_event_log("System"), strict=False)]
    assert entries
    assert all(isinstance(e, EventLogEntry) for e in entries)
    assert all(e.log_name == "System" for e in entries)
