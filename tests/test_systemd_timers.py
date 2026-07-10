"""Native scheduled tasks via systemd timers: pure mapping (os_agnostic) + a live read (Linux).

The ActiveState/UnitFileState mappings and the unit-name/unit-text helpers are pure, so they
run on every OS. The live D-Bus read needs Linux + jeepney; it self-skips otherwise.
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from pwshpy.adapters.native.systemd_timers import (
    base_name,
    iter_timers,
    service_task_state,
    service_unit,
    timer_enabled,
    timer_to_task_state,
)
from pwshpy.domain.enums import TaskState
from pwshpy.domain.records import ScheduledTaskInfo

_HAS_JEEPNEY = importlib.util.find_spec("jeepney") is not None


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("active_state", "state"),
    [
        ("active", TaskState.READY),
        ("activating", TaskState.QUEUED),
        ("inactive", TaskState.DISABLED),
        ("deactivating", TaskState.DISABLED),
        ("failed", TaskState.DISABLED),
        ("nonsense", TaskState.UNKNOWN),
    ],
)
def test_timer_state_maps(active_state: str, state: TaskState) -> None:
    """A systemd timer ActiveState maps to a TaskState (unknown -> Unknown, not a wrong guess)."""
    assert timer_to_task_state(active_state) is state


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("unit_file_state", "enabled"),
    [("enabled", True), ("enabled-runtime", True), ("disabled", False), ("static", False), ("masked", False)],
)
def test_timer_enabled_maps(unit_file_state: str, enabled: bool) -> None:
    """timer_enabled is True only for the enabled UnitFileStates."""
    assert timer_enabled(unit_file_state) is enabled


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("active_state", "state"),
    [
        ("inactive", TaskState.READY),
        ("failed", TaskState.READY),
        ("active", TaskState.RUNNING),
        ("activating", TaskState.RUNNING),
    ],
)
def test_service_task_state(active_state: str, state: TaskState) -> None:
    """An on-demand service task is RUNNING while executing, else READY (it can always be run)."""
    assert service_task_state(active_state) is state


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("task_path", "base"),
    [
        ("pwshpy-backup", "pwshpy-backup"),
        ("pwshpy-backup.timer", "pwshpy-backup"),
        ("pwshpy-backup.service", "pwshpy-backup"),
        ("\\Folder\\pwshpy-backup", "pwshpy-backup"),
        ("/etc/systemd/system/pwshpy-backup.timer", "pwshpy-backup"),
    ],
)
def test_base_name(task_path: str, base: str) -> None:
    """base_name reduces any task identifier to the systemd unit base name."""
    assert base_name(task_path) == base


@pytest.mark.os_agnostic
def test_service_unit_text() -> None:
    """service_unit renders a oneshot exec unit; arguments are appended to ExecStart."""
    text = service_unit("/usr/bin/backup", "--full /data", "nightly backup")
    assert "Type=oneshot" in text
    assert "ExecStart=/usr/bin/backup --full /data" in text
    assert "Description=nightly backup" in text


@pytest.mark.os_agnostic
def test_service_unit_no_arguments() -> None:
    """With no arguments, ExecStart is just the program (no trailing space)."""
    assert "ExecStart=/bin/true\n" in service_unit("/bin/true", "", "")


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux") or not _HAS_JEEPNEY, reason="needs Linux + jeepney")
def test_live_read_yields_timers() -> None:
    """A live D-Bus read yields typed timer records (ListUnits is unprivileged; a box always has timers)."""
    tasks = list(iter_timers())
    assert tasks  # every systemd box ships timers (e.g. systemd-tmpfiles-clean, logrotate, apt-daily)
    assert all(isinstance(t, ScheduledTaskInfo) for t in tasks)
    assert all(not t.task_name.endswith(".timer") for t in tasks)  # base name, suffix stripped
    assert all(t.task_path == "/" for t in tasks)
