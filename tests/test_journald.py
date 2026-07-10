"""Native event log via journald: pure mapping (os_agnostic) + a live read (Linux, root).

The PRIORITY->level and entry->record marshaling are pure functions with no systemd
dependency, so they run on every OS (including the Windows dev box). The live journal
read needs the systemd-journal group or root, so it is local_only + Linux-only.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

from pwshpy.adapters.native.journald import iter_event_log, to_entry, to_event_level
from pwshpy.domain.enums import EventLevel
from pwshpy.domain.errors import NativeCallError, PlatformUnsupportedError
from pwshpy.domain.records import EventLogEntry


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("priority", "level"),
    [
        (0, EventLevel.CRITICAL),
        (2, EventLevel.CRITICAL),
        (3, EventLevel.ERROR),
        (4, EventLevel.WARNING),
        (5, EventLevel.INFORMATION),
        (6, EventLevel.INFORMATION),
        (7, EventLevel.VERBOSE),
    ],
)
def test_priority_maps_to_level(priority: int, level: EventLevel) -> None:
    """Syslog PRIORITY 0-7 maps to the canonical EventLevel."""
    assert to_event_level(priority) is level


@pytest.mark.os_agnostic
def test_marshal_entry() -> None:
    """A journal-entry dict marshals into an EventLogEntry with the right fields."""
    entry = {
        "PRIORITY": 3,
        "MESSAGE": "disk full",
        "SYSLOG_IDENTIFIER": "kernel",
        "_HOSTNAME": "box",
        "_UID": 0,
        "__REALTIME_TIMESTAMP": datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
    }
    record = to_entry("System", entry)
    assert isinstance(record, EventLogEntry)
    assert record.level is EventLevel.ERROR
    assert record.provider_name == "kernel"
    assert record.machine_name == "box"
    assert record.user_id == "0"
    assert record.message == "disk full"
    assert record.time_created == datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    assert record.record_id == 0
    assert record.event_id == 0


@pytest.mark.os_agnostic
def test_marshal_entry_missing_fields_are_safe() -> None:
    """A sparse entry defaults cleanly (priority 6 -> Information, no crash on missing keys)."""
    record = to_entry("System", {})
    assert record.level is EventLevel.INFORMATION
    assert record.provider_name == ""
    assert record.user_id is None
    assert record.message is None
    assert record.time_created is None


@pytest.mark.os_linux
@pytest.mark.local_only
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="journald is Linux-only")
def test_live_read_streams_entries() -> None:
    """Reading the real journal yields EventLogEntry records (needs root / systemd-journal group)."""
    try:
        first = next(iter(iter_event_log("System")))
    except (PlatformUnsupportedError, NativeCallError) as exc:
        # a local resource this box may lack: the [journald] extra (systemd-python) is not
        # installed, or the journal is not readable by this user (root / systemd-journal group).
        pytest.skip(f"live journald read unavailable here ({exc})")
    assert isinstance(first, EventLogEntry)
    assert first.time_created is not None
