"""Event-log native adapter over win32evtlog.

A fake ``win32evtlog`` (injected at the adapter's load seam) exercises the XML
marshaling AND the streaming/laziness contract deterministically on every OS; a
real structural test reads the System log on Windows.  Exact live behaviour is
pinned against Get-WinEvent in ``test_event_log_pwsh_oracle``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

import pytest

from pwshpy.adapters.native import event_log as event_log_mod
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


class _NoMoreItemsError(Exception):
    """Stands in for the win32 ERROR_NO_MORE_ITEMS that ends an EvtNext stream."""

    winerror = 259


class _FakeWin32Evtlog:
    """A minimal streaming stand-in for the ``win32evtlog`` module."""

    EvtQueryChannelPath = 1
    EvtQueryReverseDirection = 512
    EvtRenderEventXml = 1
    EvtFormatMessageEvent = 1

    def __init__(self) -> None:
        self._served = False
        self.render_count = 0

    def EvtQuery(self, path: str, flags: int) -> str:  # noqa: N802 - win32 API name
        return f"Q-{path}"

    def EvtNext(self, query: Any, count: int) -> list[str]:  # noqa: N802 - win32 API name
        if self._served:
            raise _NoMoreItemsError
        self._served = True
        return ["H1", "H2"]

    def EvtRender(self, handle: str, flag: int) -> str:  # noqa: N802 - win32 API name
        self.render_count += 1
        return _XML_1 if handle == "H1" else _XML_2

    def EvtFormatMessage(self, meta: Any, handle: str, flag: int) -> str:  # noqa: N802 - win32 API name
        return "the message" if handle == "H1" else ""


@pytest.mark.os_agnostic
def test_iter_event_log_streams_and_marshals(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter parses the event XML into EventLogEntry records and ends cleanly."""
    monkeypatch.setattr(event_log_mod, "_load_win32evtlog", _FakeWin32Evtlog)
    entries = list(iter_event_log("System"))
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
def test_iter_event_log_is_lazy_and_memory_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Consuming one entry renders exactly one event - the stream never slurps the log."""
    fake = _FakeWin32Evtlog()
    monkeypatch.setattr(event_log_mod, "_load_win32evtlog", lambda: fake)
    first = next(iter_event_log("System"))
    assert first.record_id == 42
    assert fake.render_count == 1  # the second event was NOT rendered until requested


class _ReadError(Exception):
    """A non-ERROR_NO_MORE_ITEMS EvtNext failure (e.g. ACCESS_DENIED mid-stream)."""

    winerror = 5


@pytest.mark.os_agnostic
def test_iter_event_log_wraps_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed EvtQuery (missing channel / access denied) surfaces as NativeCallError."""

    class _BadQuery(_FakeWin32Evtlog):
        def EvtQuery(self, path: str, flags: int) -> str:  # noqa: N802 - win32 API name
            raise RuntimeError("no such channel")

    monkeypatch.setattr(event_log_mod, "_load_win32evtlog", _BadQuery)
    with pytest.raises(NativeCallError):
        list(iter_event_log("System"))


@pytest.mark.os_agnostic
def test_iter_event_log_wraps_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EvtNext failure other than ERROR_NO_MORE_ITEMS surfaces as NativeCallError."""

    class _BadNext(_FakeWin32Evtlog):
        def EvtNext(self, query: Any, count: int) -> list[str]:  # noqa: N802 - win32 API name
            raise _ReadError

    monkeypatch.setattr(event_log_mod, "_load_win32evtlog", _BadNext)
    with pytest.raises(NativeCallError):
        list(iter_event_log("System"))


@pytest.mark.os_agnostic
def test_iter_event_log_ends_on_empty_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty EvtNext batch ends the stream cleanly (distinct from the no-more-items exception)."""

    class _EmptyNext(_FakeWin32Evtlog):
        def EvtNext(self, query: Any, count: int) -> list[str]:  # noqa: N802 - win32 API name
            return []

    monkeypatch.setattr(event_log_mod, "_load_win32evtlog", _EmptyNext)
    assert list(iter_event_log("System")) == []


@pytest.mark.os_agnostic
def test_iter_event_log_wraps_render_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable event record (EvtRender raises) surfaces as NativeCallError."""

    class _BadRender(_FakeWin32Evtlog):
        def EvtRender(self, handle: str, flag: int) -> str:  # noqa: N802 - win32 API name
            raise RuntimeError("corrupt record")

    monkeypatch.setattr(event_log_mod, "_load_win32evtlog", _BadRender)
    with pytest.raises(NativeCallError):
        list(iter_event_log("System"))


@pytest.mark.os_agnostic
def test_message_render_degrades_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider with no resolvable message table yields message=None, not an error."""

    class _BadMessage(_FakeWin32Evtlog):
        def EvtFormatMessage(self, meta: Any, handle: str, flag: int) -> str:  # noqa: N802 - win32 API name
            raise RuntimeError("no message table")

    monkeypatch.setattr(event_log_mod, "_load_win32evtlog", _BadMessage)
    entries = list(iter_event_log("System"))
    assert entries
    assert all(entry.message is None for entry in entries)


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="win32evtlog is Windows-only")
def test_iter_event_log_reads_real_system_log() -> None:
    """Against the real System channel, the adapter streams typed records lazily."""
    entries = [entry for _, entry in zip(range(5), iter_event_log("System"), strict=False)]
    assert entries
    assert all(isinstance(e, EventLogEntry) for e in entries)
    assert all(e.log_name == "System" for e in entries)
