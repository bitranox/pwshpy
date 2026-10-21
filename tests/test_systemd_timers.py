"""Native scheduled tasks via systemd timers: pure mapping (os_agnostic) + a live read (Linux).

The ActiveState/UnitFileState mappings and the unit-name/unit-text helpers are pure, so they
run on every OS. The live D-Bus read needs Linux + jeepney; it self-skips otherwise.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from pwshpy.adapters.native import systemd_dbus, systemd_timers
from pwshpy.adapters.native.systemd_timers import (
    SystemdScheduledTaskController,
    base_name,
    iter_timers,
    service_last_result,
    service_task_state,
    service_unit,
    timer_enabled,
    timer_to_task_state,
    usec_to_datetime,
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


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("usec", "expected"),
    [
        (0, None),  # never triggered / no realtime elapse
        (2**64 - 1, None),  # UINT64_MAX: systemd "infinity"
        (1_700_000_000_000_000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)),
        (1_700_000_000_000_001, datetime(2023, 11, 14, 22, 13, 20, 1, tzinfo=timezone.utc)),  # exact to the microsecond
    ],
)
def test_usec_to_datetime(usec: int, expected: datetime | None) -> None:
    """A systemd USec timestamp maps to aware UTC exactly; 0 and infinity map to None."""
    assert usec_to_datetime(usec) == expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("exec_code", "exec_status", "started_usec", "expected"),
    [
        (0, 0, 0, None),  # never started: no result, not "0 = success"
        (0, 0, 1_700_000_000_000_000, None),  # started, still running: no exit recorded yet
        (1, 0, 1_700_000_000_000_000, 0),  # CLD_EXITED, status 0
        (1, 3, 1_700_000_000_000_000, 3),  # CLD_EXITED, status 3
        (2, 9, 1_700_000_000_000_000, 137),  # CLD_KILLED by SIGKILL -> 128 + signal (shell convention)
    ],
)
def test_service_last_result(exec_code: int, exec_status: int, started_usec: int, expected: int | None) -> None:
    """The service's last exit status, None until it has run once."""
    assert service_last_result(exec_code=exec_code, exec_status=exec_status, started_usec=started_usec) == expected


# --- Fake system bus: pins which D-Bus interface/property each run-info field reads -------------

_TIMER = systemd_dbus.TIMER_IFACE
_SERVICE = systemd_dbus.SERVICE_IFACE
_T0 = 1_700_000_000_000_000  # 2023-11-14 22:13:20 UTC in microseconds
_T1 = _T0 + 3_600_000_000  # one hour later


class _Address(NamedTuple):
    path: str


class _Message(NamedTuple):
    path: str
    method: str
    body: tuple[Any, ...]


class _Reply:
    def __init__(self, body: tuple[Any, ...], *, error: bool = False) -> None:
        self.body = body
        self.header = type("H", (), {"message_type": type("T", (), {"name": "error" if error else "method_return"})})


class _FakeCore:
    """The two jeepney entry points the adapter uses: DBusAddress and new_method_call."""

    @staticmethod
    def DBusAddress(path: str, *, bus_name: str, interface: str) -> _Address:  # noqa: N802 - jeepney API name
        return _Address(path)

    @staticmethod
    def new_method_call(address: _Address, method: str, signature: str = "", body: tuple[Any, ...] = ()) -> _Message:
        return _Message(address.path, method, body)


class _FakeConn:
    """A system bus holding loaded units: (unit, interface, property) -> value; unknown -> D-Bus error.

    ``unit_files`` are the units with a unit file (GetUnitFileState answers for them only); the
    Manager verbs a controller sends (Reload, StopUnit, ...) are recorded and succeed.
    """

    _VERBS = frozenset({"Reload", "StartUnit", "StopUnit", "EnableUnitFiles", "DisableUnitFiles"})

    def __init__(
        self,
        units: list[tuple[str, str, str]],
        props: dict[tuple[str, str, str], Any],
        unit_files: frozenset[str] = frozenset(),
    ) -> None:
        self._units = units  # (name, description, active_state)
        self._props = props
        self._unit_files = unit_files
        self.verbs: list[str] = []
        self.closed = False

    def send_and_get_reply(self, msg: _Message) -> _Reply:
        loaded = {unit for unit, _iface, _prop in self._props}
        if msg.method == "ListUnits":
            return _Reply(([(n, d, "loaded", a, "", "", f"/u/{n}", 0, "", "/") for n, d, a in self._units],))
        if msg.method == "GetUnit":
            (name,) = msg.body
            return _Reply((f"/u/{name}",)) if name in loaded else _Reply(("not loaded",), error=True)
        if msg.method == "GetUnitFileState":
            (name,) = msg.body
            return _Reply(("enabled",)) if name in self._unit_files else _Reply(("no unit file",), error=True)
        if msg.method in self._VERBS:
            self.verbs.append(msg.method)
            return _Reply(())
        if msg.method == "Get":
            key = (msg.path[len("/u/") :], *msg.body)
            return _Reply((("t", self._props[key]),)) if key in self._props else _Reply(("no prop",), error=True)
        return _Reply(("unexpected",), error=True)

    def close(self) -> None:
        self.closed = True


def _use_bus(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> None:
    # open_bus is the adapter's connect seam onto the system D-Bus (the external edge).
    bus = systemd_dbus.Bus(conn=conn, core=_FakeCore(), mgr=_Address("/org/freedesktop/systemd1"))
    monkeypatch.setattr(systemd_dbus, "open_bus", lambda: bus)


def _service_props(unit: str, *, code: int, status: int, started: int) -> dict[tuple[str, str, str], Any]:
    return {
        (unit, _SERVICE, "ExecMainCode"): code,
        (unit, _SERVICE, "ExecMainStatus"): status,
        (unit, _SERVICE, "ExecMainStartTimestamp"): started,
    }


@pytest.mark.os_agnostic
def test_iter_timers_reads_run_info_from_the_timer_and_its_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Listing: last/next run come from the Timer interface, the result from the service its Unit= names."""
    props: dict[tuple[str, str, str], Any] = {
        ("backup.timer", _TIMER, "LastTriggerUSec"): _T0,
        ("backup.timer", _TIMER, "NextElapseUSecRealtime"): _T1,
        ("backup.timer", _TIMER, "Unit"): "backup-job.service",  # Unit= differs from the timer's base name
        **_service_props("backup-job.service", code=1, status=3, started=_T0),
        ("never.timer", _TIMER, "LastTriggerUSec"): 0,
        ("never.timer", _TIMER, "NextElapseUSecRealtime"): 2**64 - 1,
        ("never.timer", _TIMER, "Unit"): "never.service",
    }
    units = [("backup.timer", "Backup", "active"), ("x.service", "", "active"), ("never.timer", "", "active")]
    conn = _FakeConn(units, props, frozenset({"backup.timer", "never.timer"}))
    _use_bus(monkeypatch, conn)
    backup, never = list(iter_timers())
    assert backup.task_name == "backup"
    assert backup.last_run_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert backup.next_run_time == datetime(2023, 11, 14, 23, 13, 20, tzinfo=timezone.utc)
    assert backup.last_task_result == 3
    assert (never.last_run_time, never.last_task_result, never.next_run_time) == (None, None, None)
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_controller_info_timer_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Controller snapshot of a timer task carries the same run info as the listing."""
    props: dict[tuple[str, str, str], Any] = {
        ("job.timer", systemd_dbus.UNIT_IFACE, "ActiveState"): "active",
        ("job.timer", _TIMER, "LastTriggerUSec"): _T0,
        ("job.timer", _TIMER, "NextElapseUSecRealtime"): _T1,
        ("job.timer", _TIMER, "Unit"): "job.service",
        **_service_props("job.service", code=2, status=9, started=_T0),
    }
    conn = _FakeConn([], props, frozenset({"job.timer"}))
    _use_bus(monkeypatch, conn)
    info = SystemdScheduledTaskController().disable("job")
    assert conn.verbs == ["StopUnit", "DisableUnitFiles", "Reload"]
    assert info.state is TaskState.READY
    assert info.last_run_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert info.next_run_time == datetime(2023, 11, 14, 23, 13, 20, tzinfo=timezone.utc)
    assert info.last_task_result == 137  # killed by SIGKILL


@pytest.mark.os_agnostic
def test_controller_info_on_demand_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An on-demand service: last run is its own last start, the result its exit, nothing scheduled."""
    props = _service_props("adhoc.service", code=1, status=0, started=_T0)
    _use_bus(monkeypatch, _FakeConn([], props))
    monkeypatch.setattr(systemd_timers, "_UNIT_DIR", tmp_path)  # filesystem edge: never write /etc here
    info = SystemdScheduledTaskController().register("adhoc", program="/bin/true")
    assert (tmp_path / "adhoc.service").is_file()
    assert info.last_run_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert info.last_task_result == 0  # ran and succeeded, never folded into None
    assert info.next_run_time is None


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux") or not _HAS_JEEPNEY, reason="needs Linux + jeepney")
def test_live_read_yields_timers() -> None:
    """A live D-Bus read yields typed timer records (ListUnits is unprivileged; a box always has timers)."""
    tasks = list(iter_timers())
    assert tasks  # every systemd box ships timers (e.g. systemd-tmpfiles-clean, logrotate, apt-daily)
    assert all(isinstance(t, ScheduledTaskInfo) for t in tasks)
    assert all(not t.task_name.endswith(".timer") for t in tasks)  # base name, suffix stripped
    assert all(t.task_path == "/" for t in tasks)


@pytest.mark.local_only
@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux") or not _HAS_JEEPNEY, reason="needs Linux + jeepney")
def test_live_read_reports_run_info() -> None:
    """Liveness for the run-info property names: a real box has a calendar timer with a next elapse.

    A misspelt property or interface reads the "none" default and would leave every timer at None,
    so requiring at least one real next_run_time is what proves the names are right.
    """
    tasks = list(iter_timers())
    assert any(t.next_run_time is not None for t in tasks)
    assert all(t.next_run_time is None or t.next_run_time.tzinfo is timezone.utc for t in tasks)
    assert all(t.last_run_time is None or t.last_run_time.tzinfo is timezone.utc for t in tasks)
