"""Locale invariance: pwshpy emits canonical ENGLISH enum values on any locale.

A regression guard for ``docs/locale-and-identity.md``.  This runs on the real
(German) dev box and on the English CI lane alike (``os_windows`` + skipif); if
any adapter ever marshaled a localized OS string (``Ausgeführt`` / ``Informationen``)
instead of the win32 numeric constant, it would emit German here and this test
would fail.
"""

from __future__ import annotations

import sys

import pytest

from pwshpy import ps
from pwshpy.domain.enums import AceType, EventLevel, ServiceStartType, ServiceState, TaskState

pytestmark = [pytest.mark.os_windows, pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")]

_ENGLISH_STATES = {state.value for state in ServiceState}
_ENGLISH_START_TYPES = {start.value for start in ServiceStartType}
_ENGLISH_LEVELS = {level.value for level in EventLevel}
_ENGLISH_TASK_STATES = {state.value for state in TaskState}
_ENGLISH_ACE_TYPES = {ace.value for ace in AceType}


def test_service_enums_are_english_regardless_of_os_locale() -> None:
    """Service status / start type serialize as English enum names, not localized OS strings."""
    services = ps.get_service().to_list()
    assert services
    assert {s.status.value for s in services} <= _ENGLISH_STATES
    assert {s.start_type.value for s in services if s.start_type is not None} <= _ENGLISH_START_TYPES
    # A running service reports the English "Running", never a translated string.
    assert any(s.status is ServiceState.RUNNING for s in services)


def test_event_levels_are_english_regardless_of_os_locale() -> None:
    """Event levels serialize as English enum names, not the localized LevelDisplayName."""
    levels = {entry.level.value for entry in ps.get_win_event("System").take(50)}
    assert levels
    assert levels <= _ENGLISH_LEVELS


def test_task_states_are_english_regardless_of_os_locale() -> None:
    """Scheduled-task states serialize as English enum names on any locale."""
    states = {task.state.value for task in ps.get_scheduled_task()}
    assert states
    assert states <= _ENGLISH_TASK_STATES


def test_ace_types_are_english_regardless_of_os_locale() -> None:
    """ACL access types serialize as English enum names (Allow/Deny), not localized strings."""
    types = {entry.access_type.value for entry in ps.get_acl(r"C:\Windows")}
    assert types
    assert types <= _ENGLISH_ACE_TYPES
