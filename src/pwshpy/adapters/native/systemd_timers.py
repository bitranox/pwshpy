"""Native scheduled tasks on Linux via systemd timers over D-Bus (jeepney).

The portable counterpart to the Task Scheduler adapter. On systemd a scheduled task is a
``.timer`` unit (the schedule) that triggers a ``.service`` unit (the work), so:

* **read** (:func:`iter_timers`) lists the ``.timer`` units - the box's real scheduled tasks -
  and marshals each into the same :class:`ScheduledTaskInfo`.
* **register** creates an on-demand ``.service`` (``Type=oneshot``), exactly matching the
  Windows ``Register-ScheduledTask`` shape pwshpy exposes (a run-on-demand exec action, no
  trigger). ``run`` starts it. ``enable``/``disable`` arm/disarm a task's ``.timer`` (via
  ``EnableUnitFiles``/``DisableUnitFiles``); an on-demand task has no schedule to toggle, so
  they raise a clear error pointing at ``run``/``unregister`` instead. (systemd cannot "disable"
  a static oneshot: a mask symlink cannot shadow the unit file we wrote under ``/etc``.)

Shared D-Bus plumbing lives in :mod:`.systemd_dbus`. Mutation writes unit files under
``/etc/systemd/system`` (needs root, like the Windows verbs need admin) and never touches a
vendor unit under ``/lib``.

Contents:
    * :func:`timer_to_task_state` / :func:`timer_enabled` - pure systemd -> domain mappers.
    * :func:`iter_timers` - stream the ``.timer`` units as records.
    * :class:`SystemdScheduledTaskController` - register/unregister, run/stop, enable/disable.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from ...domain.enums import TaskState
from ...domain.errors import NativeCallError
from ...domain.records import ScheduledTaskInfo
from . import systemd_dbus as _dbus

_ROOT = "/"  # systemd timers are a flat namespace; task_path is this neutral root (no folder tree)
_UNIT_DIR = Path("/etc/systemd/system")  # where pwshpy writes its own units (system scope, needs root)

#: systemd timer ActiveState -> TaskState. An armed timer is READY (waiting to fire); a stopped
#: or failed timer is DISABLED. RUNNING (the triggered job executing) is Windows-only granularity.
_TIMER_STATE = {
    "active": TaskState.READY,
    "activating": TaskState.QUEUED,
    "inactive": TaskState.DISABLED,
    "deactivating": TaskState.DISABLED,
    "failed": TaskState.DISABLED,
}


def timer_to_task_state(active_state: str) -> TaskState:
    """Map a systemd timer ``ActiveState`` to a :class:`TaskState`.

    Example:
        >>> timer_to_task_state("active").value, timer_to_task_state("inactive").value
        ('Ready', 'Disabled')
    """
    return _TIMER_STATE.get(active_state, TaskState.UNKNOWN)


def timer_enabled(unit_file_state: str) -> bool:
    """Whether a unit's ``UnitFileState`` counts as enabled (armed at boot).

    Example:
        >>> timer_enabled("enabled"), timer_enabled("disabled")
        (True, False)
    """
    return unit_file_state in ("enabled", "enabled-runtime")


def service_task_state(active_state: str) -> TaskState:
    """State for an on-demand ``.service`` task: running -> RUNNING, else READY (it can always be run)."""
    if active_state in ("active", "activating"):
        return TaskState.RUNNING
    return TaskState.READY


def iter_timers(folder_path: str = _ROOT) -> Iterator[ScheduledTaskInfo]:
    """Yield a :class:`ScheduledTaskInfo` per systemd ``.timer`` unit (like Get-ScheduledTask, Linux).

    ``folder_path`` is accepted for signature parity with the Windows source and ignored (systemd
    timers are flat). Lazy: ``GetUnitFileState`` runs only for consumed timers.

    Example:
        >>> callable(iter_timers)
        True
    """
    bus = _dbus.open_bus()
    try:
        (units,) = _dbus.call(bus, "ListUnits")
        for unit in units:
            name = str(unit[0])
            if not name.endswith(".timer"):
                continue
            state = _dbus.unit_file_state(bus, name) or ""
            yield ScheduledTaskInfo.model_construct(
                task_name=name[: -len(".timer")],
                task_path=_ROOT,
                state=timer_to_task_state(str(unit[3])),
                enabled=timer_enabled(state),
                author="",
                description=str(unit[1]),  # ListUnits field 1 is the unit Description
            )
    finally:
        bus.conn.close()


def base_name(task_path: str) -> str:
    """Reduce a task identifier to its systemd unit base name (drop any folder and .timer/.service)."""
    name = task_path.strip().replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".timer", ".service"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def service_unit(program: str, arguments: str, description: str) -> str:
    """The unit-file text for an on-demand oneshot ``.service`` (the run-on-demand exec action)."""
    exec_line = program if not arguments else f"{program} {arguments}"
    return f"[Unit]\nDescription={description or 'pwshpy task'}\n\n[Service]\nType=oneshot\nExecStart={exec_line}\n"


def _has_timer(bus: _dbus.Bus, base: str) -> bool:
    """Whether ``base`` names a scheduled (``.timer``) task rather than a plain on-demand service."""
    return _dbus.unit_file_state(bus, f"{base}.timer") is not None or (_UNIT_DIR / f"{base}.timer").exists()


class SystemdScheduledTaskController:
    """Register/unregister, run/stop and enable/disable systemd timer tasks (Linux, **mutating**).

    The counterpart to the Task Scheduler NativeScheduledTaskController; needs root (writes units
    under ``/etc/systemd/system``), like the Windows verbs need admin.

    Example:
        >>> callable(SystemdScheduledTaskController().register)
        True
    """

    def register(
        self, task_path: str, *, program: str, arguments: str = "", description: str = ""
    ) -> ScheduledTaskInfo:
        """Create a run-on-demand oneshot ``.service`` (``program`` + ``arguments``); return it."""
        base = base_name(task_path)
        path = _UNIT_DIR / f"{base}.service"
        try:
            path.write_text(service_unit(program, arguments, description), encoding="utf-8")
        except OSError as exc:
            raise NativeCallError(f"cannot write unit {path}: {exc}") from exc
        bus = _dbus.open_bus()
        try:
            _dbus.call(bus, "Reload")
            return self._info(bus, base)
        finally:
            bus.conn.close()

    def unregister(self, task_path: str) -> None:
        """Stop and delete a task's units (only the files pwshpy could have written, under /etc)."""
        base = base_name(task_path)
        bus = _dbus.open_bus()
        try:
            for unit in (f"{base}.timer", f"{base}.service"):
                with contextlib.suppress(NativeCallError):  # not loaded / already gone -> nothing to stop
                    _dbus.call(bus, "StopUnit", "ss", (unit, "replace"))
                (_UNIT_DIR / unit).unlink(missing_ok=True)
            _dbus.call(bus, "Reload")
        finally:
            bus.conn.close()

    def run(self, task_path: str) -> None:
        """Run the task now: start its ``.service`` (like Start-ScheduledTask, on demand)."""
        self._start_stop(base_name(task_path), "StartUnit")

    def stop(self, task_path: str) -> None:
        """Stop the task's running ``.service`` (like Stop-ScheduledTask)."""
        self._start_stop(base_name(task_path), "StopUnit")

    def enable(self, task_path: str) -> ScheduledTaskInfo:
        """Arm a task's ``.timer`` so it runs on schedule (like Enable-ScheduledTask)."""
        base = base_name(task_path)
        bus = _dbus.open_bus()
        try:
            self._require_timer(bus, base, "enable")
            _dbus.call(bus, "EnableUnitFiles", "asbb", ([f"{base}.timer"], False, False))
            _dbus.call(bus, "StartUnit", "ss", (f"{base}.timer", "replace"))
            _dbus.call(bus, "Reload")
            return self._info(bus, base)
        finally:
            bus.conn.close()

    def disable(self, task_path: str) -> ScheduledTaskInfo:
        """Disarm a task's ``.timer`` so it no longer runs on schedule (like Disable-ScheduledTask)."""
        base = base_name(task_path)
        bus = _dbus.open_bus()
        try:
            self._require_timer(bus, base, "disable")
            _dbus.call(bus, "StopUnit", "ss", (f"{base}.timer", "replace"))
            _dbus.call(bus, "DisableUnitFiles", "asb", ([f"{base}.timer"], False))
            _dbus.call(bus, "Reload")
            return self._info(bus, base)
        finally:
            bus.conn.close()

    @staticmethod
    def _require_timer(bus: _dbus.Bus, base: str, verb: str) -> None:
        """A task with no ``.timer`` is on-demand; enable/disable have nothing to toggle for it."""
        if not _has_timer(bus, base):
            raise NativeCallError(
                f"task {base!r} is on-demand (no .timer); nothing to {verb} - run it directly, or unregister it."
            )

    def _start_stop(self, base: str, method: str) -> None:
        bus = _dbus.open_bus()
        try:
            _dbus.call(bus, method, "ss", (f"{base}.service", "replace"))
        finally:
            bus.conn.close()

    def _info(self, bus: _dbus.Bus, base: str) -> ScheduledTaskInfo:
        """Snapshot a task's state - timer-based if it has a ``.timer``, else the on-demand service."""
        if _has_timer(bus, base):
            unit = f"{base}.timer"
            state = _dbus.unit_file_state(bus, unit) or ""
            return ScheduledTaskInfo.model_construct(
                task_name=base,
                task_path=_ROOT,
                state=timer_to_task_state(_dbus.active_state(bus, unit)),
                enabled=timer_enabled(state),
                author="",
                description=str(_dbus.unit_property(bus, unit, "Description", "")),
            )
        unit = f"{base}.service"
        return ScheduledTaskInfo.model_construct(
            task_name=base,
            task_path=_ROOT,
            state=service_task_state(_dbus.active_state(bus, unit)),
            enabled=True,  # an on-demand oneshot has no schedule to disable; it can always be run
            author="",
            description=str(_dbus.unit_property(bus, unit, "Description", "")),
        )


__all__ = [
    "SystemdScheduledTaskController",
    "base_name",
    "iter_timers",
    "service_task_state",
    "service_unit",
    "timer_enabled",
    "timer_to_task_state",
]
