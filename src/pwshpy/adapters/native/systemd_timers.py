"""Native scheduled tasks on Linux via systemd timers over D-Bus (jeepney).

The portable counterpart to the Task Scheduler adapter. On systemd a scheduled task is a
``.timer`` unit (the schedule) that triggers a ``.service`` unit (the work), so:

* **read** (:func:`iter_timers`) lists the ``.timer`` units - the box's real scheduled tasks,
  loaded or not (a disabled timer is unloaded) - and marshals each into the same
  :class:`ScheduledTaskInfo`. Units are read through ``LoadUnit``, which answers for an
  unloaded unit too; ``GetUnit`` answers only for loaded ones.
* **register** writes a ``.service`` (``Type=oneshot``, the exec action; ``User=`` for a uid
  principal) and, when triggers are given, a ``.timer`` that starts it (``OnCalendar=`` in UTC
  for once/daily, ``OnBootSec=`` for boot), enabled over D-Bus. A calendar timer is started at
  once; a boot timer is only enabled, because systemd fires an already-elapsed ``OnBootSec=``
  the moment the timer starts, while a boot trigger must wait for the next boot. Like the
  Windows ``TASK_CREATE_OR_UPDATE`` registration, registering over an existing task replaces
  it, retiring a ``.timer`` the new definition no longer has. With no triggers the task runs
  only on demand: ``run`` starts it. ``enable``/``disable`` arm/disarm a task's ``.timer`` (via
  ``EnableUnitFiles``/``DisableUnitFiles``); an on-demand task has no schedule to toggle, so
  they raise a clear error pointing at ``run``/``unregister`` instead. (systemd cannot "disable"
  a static oneshot: a mask symlink cannot shadow the unit file we wrote under ``/etc``.) A static
  timer (no ``[Install]`` section) is refused the same way: systemd accepts both calls and changes
  nothing, so the task would still read enabled.
* **principals** are a decimal uid (``User=``) or SYSTEM / none (root: a system unit's
  default); a Windows SID, a logon type or HIGHEST for a non-root uid has no Linux meaning and
  is refused. Read back, ``user_id`` is the triggered service's ``User=`` as a decimal uid.

Shared D-Bus plumbing lives in :mod:`.systemd_dbus`. Mutation writes unit files under
``/etc/systemd/system`` (needs root, like the Windows verbs need admin) and never touches a
vendor unit under ``/lib``.

Contents:
    * :func:`timer_to_task_state` / :func:`timer_enabled` - pure systemd -> domain mappers.
    * :func:`usec_to_datetime` / :func:`service_last_result` / :func:`latest_usec` - pure run-info
      mappers.
    * :func:`service_user_id` - a service's ``User=`` as a decimal uid.
    * :func:`service_unit` / :func:`timer_unit` / :func:`linux_user` / :func:`arms_on_register` -
      pure unit-file rendering and register policy.
    * :func:`iter_timers` - stream the ``.timer`` units as records.
    * :class:`SystemdScheduledTaskController` - register/unregister, run/stop, enable/disable.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple, cast

from pydantic import ValidationError

from ...domain.enums import TaskRunLevel, TaskState, WellKnownSid
from ...domain.errors import ConfigurationError, NativeCallError, PlatformUnsupportedError
from ...domain.records import ScheduledTaskInfo
from ...domain.scheduling import BootTrigger, DailyTrigger, OnceTrigger, TaskPrincipal, TaskTrigger
from . import systemd_dbus as _dbus

logger = logging.getLogger(__name__)

_ROOT = "/"  # systemd timers are a flat namespace; task_path is this neutral root (no folder tree)
_UNIT_DIR = Path("/etc/systemd/system")  # where pwshpy writes its own units (system scope, needs root)

# Run-info sentinels, read on a container host with busctl; re-checked on a root Linux VM in Task 6.
_USEC_INFINITY = 2**64 - 1  # systemd's USEC_INFINITY: "never elapses"
_CLD_EXITED = 1  # ExecMainCode: the main process exited (status = exit code); 2/3 = killed/dumped (status = signal)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

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
    """Whether a timer's ``UnitFileState`` counts as enabled (armed, it runs on schedule).

    ``static`` counts as enabled: a static timer has no ``[Install]`` section, so it cannot be
    disabled, and another unit pulls it in, so it is armed and fires (``systemd-tmpfiles-clean``).
    ``disabled`` and ``masked`` are not enabled.

    Example:
        >>> timer_enabled("enabled"), timer_enabled("static"), timer_enabled("disabled")
        (True, True, False)
    """
    return unit_file_state in ("enabled", "enabled-runtime", "static")


def timer_task_state(active_state: str, *, enabled: bool) -> TaskState:
    """A timer task's state: an enabled but inactive timer is READY, else :func:`timer_to_task_state`.

    An enabled timer that is not running is armed for the next boot (timers.target starts it),
    which is what a freshly registered boot task looks like - waiting to fire, not disabled.

    Example:
        >>> timer_task_state("inactive", enabled=True).value, timer_task_state("inactive", enabled=False).value
        ('Ready', 'Disabled')
    """
    if active_state == "inactive" and enabled:
        return TaskState.READY
    return timer_to_task_state(active_state)


#: Monotonic timer bases (Timer.TimersMonotonic names) that systemd fires at once when the timer
#: is started after they have elapsed - OnBootSec= / OnStartupSec= (systemd.timer(5)).
_BOOT_RELATIVE = frozenset({"OnBootUSec", "OnStartupUSec"})


def starts_when_armed(monotonic_bases: Iterable[str]) -> bool:
    """Whether arming a timer may also start it now: False when any monotonic timer is boot-relative.

    The one decision behind both ``register`` (from the triggers) and ``enable`` (from the
    timer's live ``TimersMonotonic``): a started boot-relative timer whose boot offset is already
    past runs its job immediately, so such a timer is only enabled and waits for the next boot.

    Example:
        >>> starts_when_armed(["OnUnitActiveUSec"]), starts_when_armed(["OnBootUSec"])
        (True, False)
    """
    return _BOOT_RELATIVE.isdisjoint(monotonic_bases)


def service_task_state(active_state: str) -> TaskState:
    """State for an on-demand ``.service`` task: running -> RUNNING, else READY (it can always be run)."""
    if active_state in ("active", "activating"):
        return TaskState.RUNNING
    return TaskState.READY


def usec_to_datetime(value: int) -> datetime | None:
    """Map a systemd USec timestamp to aware UTC; 0 (never/none) and infinity -> None.

    Example:
        >>> usec_to_datetime(0) is None
        True
        >>> usec_to_datetime(1_000_000)
        datetime.datetime(1970, 1, 1, 0, 0, 1, tzinfo=datetime.timezone.utc)
    """
    if value in (0, _USEC_INFINITY):
        return None
    return _EPOCH + timedelta(microseconds=value)  # exact: a float division drops the last microsecond


def service_last_result(*, exec_code: int, exec_status: int, started_usec: int) -> int | None:
    """The service's last result: its exit status, 128 + signal if killed, None if it never started.

    Args:
        exec_code: ``ExecMainCode`` - the CLD_* code (0 none yet, 1 exited, 2 killed, 3 dumped).
        exec_status: ``ExecMainStatus`` - the exit status, or the signal number when killed.
        started_usec: ``ExecMainStartTimestamp`` - 0 when the service never started.

    Returns:
        The exit status, ``128 + signal`` for a killed or dumped process (the shell's
        convention, so a kill never reads like a clean exit with the same number), or None.

    Example:
        >>> service_last_result(exec_code=1, exec_status=0, started_usec=1), service_last_result(
        ...     exec_code=0, exec_status=0, started_usec=0)
        (0, None)
    """
    if started_usec == 0 or exec_code == 0:  # never started, or running now with no exit recorded yet
        return None
    return exec_status if exec_code == _CLD_EXITED else 128 + exec_status


def _canonical_uid(text: str) -> str | None:
    """``text`` when it is a canonical decimal uid (the domain's own predicate), else None."""
    try:
        principal = TaskPrincipal(user_id=text)
    except ValidationError:  # an account name, a padded or out-of-range number
        return None
    return text if principal.is_uid else None


def _passwd_uid(name: str) -> int | None:
    """The uid of account ``name`` from the account database, or None when it does not resolve."""
    try:
        pwd = importlib.import_module("pwd")  # POSIX-only stdlib: imported lazily so Windows can import us
        return int(pwd.getpwnam(name).pw_uid)
    except (ImportError, KeyError, OSError):
        return None


def service_user_id(configured: str | None, *, resolve: Callable[[str], int | None] = _passwd_uid) -> str:
    """A service's ``User=`` setting as the decimal uid :class:`ScheduledTaskInfo` carries.

    Fail-soft like the rest of the read-back: a value that is neither a uid nor a resolvable
    account yields ``""`` and the task is still listed.

    Args:
        configured: The ``Service.User`` D-Bus property (the configured text), or None when it
            could not be read (the service is not loaded).
        resolve: Account-name lookup returning a uid or None; the account database by default.

    Returns:
        The decimal uid; ``"0"`` for an empty ``User=`` (a system service runs as root); ``""``
        when unknown.

    Example:
        >>> service_user_id(""), service_user_id("1000"), service_user_id(None)
        ('0', '1000', '')
    """
    if configured is None:
        return ""
    if configured == "":
        # A DynamicUser=yes service also reads "0" here although it runs as a uid allocated at
        # start; its User= is empty and the dynamic uid exists only while it runs.
        return "0"
    # systemd itself reads a numeric User= as a uid before trying it as a name.
    uid = _canonical_uid(configured)
    if uid is not None:
        return uid
    resolved = resolve(configured)
    return "" if resolved is None else str(resolved)


def _service_user(bus: _dbus.Bus, service_path: str | None) -> str:
    """The decimal uid a loaded service runs as, read from its already-resolved object path."""
    # User lives on the Service interface (ExecContext), not on Unit; None marks "not readable".
    configured = _dbus.path_property(bus, service_path, "User", None, interface=_dbus.SERVICE_IFACE)
    return service_user_id(None if configured is None else str(configured))


def _timer_property(bus: _dbus.Bus, timer_path: str | None, prop: str) -> int:
    """One ``Timer`` USec property as an int (0 - "none" - when the timer is not loaded)."""
    return int(_dbus.path_property(bus, timer_path, prop, 0, interface=_dbus.TIMER_IFACE))


def _service_run(bus: _dbus.Bus, service_path: str | None) -> tuple[int | None, int]:
    """A service's last result (None if it never ran) and its last ``ExecMainStartTimestamp`` (0: none)."""

    def read(prop: str) -> int:
        return int(_dbus.path_property(bus, service_path, prop, 0, interface=_dbus.SERVICE_IFACE))

    started = read("ExecMainStartTimestamp")
    result = service_last_result(
        exec_code=read("ExecMainCode"), exec_status=read("ExecMainStatus"), started_usec=started
    )
    return result, started


def latest_usec(*values: int) -> int:
    """The latest of several systemd USec timestamps, ignoring 0 (none) and infinity; 0 when none is set.

    Example:
        >>> latest_usec(0, 5, 3), latest_usec(0, 2**64 - 1)
        (5, 0)
    """
    return max((v for v in values if v not in (0, _USEC_INFINITY)), default=0)


class _RunInfo(NamedTuple):
    """The fields a timer task reads from its timer and its triggered service."""

    last_run_time: datetime | None
    last_task_result: int | None
    next_run_time: datetime | None
    user_id: str


def _timer_run_info(bus: _dbus.Bus, timer_path: str | None, base: str) -> _RunInfo:
    """A timer task's last run, next realtime elapse, and its triggered service's result and user.

    The service is the one the timer's ``Unit`` property names (``<base>.service`` unless the
    timer sets ``Unit=``), so listing and controller report the same result for the same timer.
    Properties are read from the already-resolved ``timer_path``; only the service costs a
    lookup - ``LoadUnit``, because a stopped service is usually unloaded and ``GetUnit`` would
    read every field as a default - and its result and ``User=`` share that one resolved path.

    ``last_run_time`` is the later of the timer's ``LastTriggerUSec`` and the service's
    ``ExecMainStartTimestamp``: an on-demand start (``run``) never moves the trigger time but is
    a run, as Windows' LastRunTime counts Start-ScheduledTask; the trigger stays in the max so a
    ``Persistent=`` trigger that survived a reboot (which clears the service's start) still shows.
    """
    service = str(_dbus.path_property(bus, timer_path, "Unit", f"{base}.service", interface=_dbus.TIMER_IFACE))
    service_path = _dbus.load_unit_path(bus, service)
    result, started = _service_run(bus, service_path)
    return _RunInfo(
        last_run_time=usec_to_datetime(latest_usec(_timer_property(bus, timer_path, "LastTriggerUSec"), started)),
        last_task_result=result,
        next_run_time=usec_to_datetime(_timer_property(bus, timer_path, "NextElapseUSecRealtime")),
        user_id=_service_user(bus, service_path),
    )


class _Listed(NamedTuple):
    """What ``ListUnits`` already reports for a loaded unit, so no property read repeats it."""

    active_state: str
    description: str


def _timer_task(
    bus: _dbus.Bus, name: str, path: str | None, *, file_state: str, listed: _Listed | None = None
) -> ScheduledTaskInfo:
    """The record for timer ``name`` at its resolved object ``path`` (None: unreadable, fields default).

    ``listed`` carries ActiveState and Description from ``ListUnits``; without it (a unit read
    through ``LoadUnit``) both are read from ``path``.
    """
    if listed is None:
        listed = _Listed(
            active_state=str(_dbus.path_property(bus, path, "ActiveState", "inactive")),
            description=str(_dbus.path_property(bus, path, "Description", "")),
        )
    base = name[: -len(".timer")]
    run = _timer_run_info(bus, path, base)
    enabled = timer_enabled(file_state)
    return ScheduledTaskInfo.model_construct(
        task_name=base,
        task_path=_ROOT,
        state=timer_task_state(listed.active_state, enabled=enabled),
        enabled=enabled,
        author="",
        description=listed.description,
        user_id=run.user_id,
        run_level=None,  # systemd has no split token, so no run level to report
        last_run_time=run.last_run_time,
        last_task_result=run.last_task_result,
        next_run_time=run.next_run_time,
    )


def _timer_files(bus: _dbus.Bus) -> list[tuple[str, str]]:
    """Every ``.timer`` unit file as (unit name, UnitFileState); [] when systemd lists none.

    Fail-soft: when no unit-file listing answers, the listing keeps the loaded timers rather than
    failing.
    """
    try:
        return _dbus.unit_files(bus, ".timer")
    except NativeCallError as exc:
        logger.debug("cannot list the timer unit files, listing loaded timers only: %s", exc)
        return []


def iter_timers(folder_path: str = _ROOT) -> Iterator[ScheduledTaskInfo]:
    """Yield a :class:`ScheduledTaskInfo` per systemd ``.timer`` unit (like Get-ScheduledTask, Linux).

    ``folder_path`` is accepted for signature parity with the Windows source and ignored (systemd
    timers are flat). The loaded timers come first, from ``ListUnits``: the timer's object path
    comes from the listing itself, so a loaded timer costs no lookup. Then the timer unit FILES
    not among them - a disabled or not-yet-started timer is unloaded and ``ListUnits`` omits it,
    while Get-ScheduledTask lists a disabled task - each read through ``LoadUnit``. Skipped as no
    task: a stopped ListUnits entry whose LoadState is not-found or masked
    (:func:`~.systemd_dbus.is_unlisted_unit`; a running one is listed), and a masked file, an alias,
    a template or a file ``LoadUnit`` reads as not-found. A skipped entry does not hide its file:
    a name systemd cached as not-found before its file was written is listed from the file.
    Lazy: the unit-file listing,
    ``GetUnitFileState`` and the run-info reads run only for consumed timers. Reading an unloaded
    timer loads it transiently; systemd unloads it again when nothing references it.

    Example:
        >>> callable(iter_timers)
        True
    """
    bus = _dbus.open_bus()
    try:
        (units,) = _dbus.call(bus, "ListUnits")
        seen: set[str] = set()
        for unit in units:
            name = str(unit[0])
            if not name.endswith(".timer"):
                continue
            # ListUnits fields: 1 Description, 2 LoadState, 3 ActiveState, 6 the unit object path.
            if _dbus.is_unlisted_unit(str(unit[2]), str(unit[3])):
                # Not marked seen: a cached not-found name whose file appeared since (no
                # daemon-reload) is still listed by the unit-file pass, which LoadUnit reloads.
                continue
            seen.add(name)
            state = _dbus.unit_file_state(bus, name) or ""
            listed = _Listed(active_state=str(unit[3]), description=str(unit[1]))
            yield _timer_task(bus, name, str(unit[6]), file_state=state, listed=listed)
        for name, state in _timer_files(bus):
            if name in seen or state in _dbus.UNLISTED_FILE_STATES:
                continue
            seen.add(name)
            path = _dbus.load_unit_path(bus, name)
            if path is None:  # a stale or broken file: systemd has no unit behind the name
                continue
            yield _timer_task(bus, name, path, file_state=state)
    finally:
        bus.conn.close()


def base_name(task_path: str) -> str:
    """Reduce a task identifier to its systemd unit base name (drop any folder and .timer/.service)."""
    name = task_path.strip().replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".timer", ".service"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _single_line(text: str, field: str) -> str:
    """``text`` unchanged, or ConfigurationError when it holds a line break or NUL.

    A unit file is line-oriented, so a newline in a value starts a new directive (``User=0``,
    ``ExecStartPre=``); refusing is the only safe answer - there is no escape for it. The value
    itself is not echoed: arguments may carry a secret.
    """
    if "\n" in text or "\r" in text or "\0" in text:
        raise ConfigurationError(f"{field} must not contain a line break or NUL: it would inject unit-file directives.")
    return text


def _unit_value(text: str, field: str) -> str:
    """A single-line ``text`` with ``%`` doubled, so systemd reads it literally, not as a specifier."""
    return _single_line(text, field).replace("%", "%%")


def service_unit(program: str, arguments: str, description: str, *, user: str | None = None) -> str:
    """The unit-file text for a oneshot ``.service`` (the exec action), run as ``user`` if given.

    Raises:
        ConfigurationError: ``program``, ``arguments`` or ``description`` holds a line break.

    Example:
        >>> "User=1000" in service_unit("/bin/true", "", "d", user="1000")
        True
        >>> "ExecStart=/usr/bin/date +%%Y" in service_unit("/usr/bin/date", "+%Y", "")
        True
    """
    program = _unit_value(program, "program")
    arguments = _unit_value(arguments, "arguments")
    exec_line = program if not arguments else f"{program} {arguments}"
    user_line = f"User={user}\n" if user is not None else ""
    return (
        f"[Unit]\nDescription={_unit_value(description, 'description') or 'pwshpy task'}\n\n"
        f"[Service]\nType=oneshot\n{user_line}ExecStart={exec_line}\n"
    )


def _utc(at: datetime) -> datetime:
    return at.astimezone(timezone.utc)


def _timer_line(trigger: TaskTrigger) -> str:
    """One [Timer] line; UTC with an explicit suffix so the host's zone cannot shift it."""
    if isinstance(trigger, OnceTrigger):
        return f"OnCalendar={_utc(trigger.at):%Y-%m-%d %H:%M:%S} UTC"
    if isinstance(trigger, DailyTrigger):
        if trigger.every_days != 1:
            raise PlatformUnsupportedError(
                "systemd has no every-N-days calendar; only every_days=1 is supported on Linux."
            )
        # A calendar spec has no start date, so this runs daily from now on even when ``at`` lies ahead.
        return f"OnCalendar=*-*-* {_utc(trigger.at):%H:%M:%S} UTC"
    if isinstance(trigger, BootTrigger):
        return f"OnBootSec={int(trigger.delay.total_seconds())}s"
    raise PlatformUnsupportedError(
        "a logon trigger has no systemd analog on Linux: a user unit fires when the user's service "
        "manager starts (the user's first session), not at each logon."
    )


def timer_unit(base: str, description: str, triggers: Sequence[TaskTrigger]) -> str:
    """The ``.timer`` text that starts ``<base>.service`` on each trigger.

    ``AccuracySec=1s`` because systemd's default one-minute accuracy window lets a calendar
    trigger fire up to a minute late; Task Scheduler starts on the second.

    Raises:
        PlatformUnsupportedError: a logon trigger, or a daily trigger with ``every_days`` > 1.
        ConfigurationError: ``base`` or ``description`` holds a line break.

    Example:
        >>> "OnBootSec=90s" in timer_unit("job", "", [BootTrigger(delay=timedelta(seconds=90))])
        True
    """
    lines = "".join(f"{_timer_line(t)}\n" for t in triggers)
    unit = _single_line(base, "task_path")
    return (
        f"[Unit]\nDescription={_unit_value(description, 'description') or 'pwshpy task'}\n\n"
        f"[Timer]\n{lines}AccuracySec=1s\nUnit={unit}.service\n\n[Install]\nWantedBy=timers.target\n"
    )


def arms_on_register(triggers: Sequence[TaskTrigger]) -> bool:
    """Whether register starts the ``.timer`` now (calendar) or leaves it for the next boot (boot).

    systemd fires an ``OnBootSec=`` that is already in the past the moment the timer starts,
    so a boot timer is only enabled: timers.target starts it early at the next boot. The
    decision itself is :func:`starts_when_armed`, shared with ``enable``.

    Raises:
        PlatformUnsupportedError: boot and calendar triggers are mixed - starting the timer
            would fire the boot trigger now, not starting it would leave the calendar idle.

    Example:
        >>> arms_on_register([BootTrigger()])
        False
    """
    boot = [isinstance(t, BootTrigger) for t in triggers]
    if any(boot) and not all(boot):
        raise PlatformUnsupportedError(
            "on Linux a boot trigger cannot share a task with calendar triggers: one systemd timer "
            "cannot both wait for the next boot and run on schedule from now. Register them separately."
        )
    # A boot trigger renders as OnBootSec=, which systemd reports as the OnBootUSec monotonic base.
    return starts_when_armed("OnBootUSec" for t in triggers if isinstance(t, BootTrigger))


def linux_user(principal: TaskPrincipal) -> str | None:
    """The ``User=`` value for a principal, or None for root (a system unit's default).

    Raises:
        PlatformUnsupportedError: a logon type, a Windows SID other than SYSTEM, or HIGHEST for
            a non-root uid - none has a Linux meaning.

    Example:
        >>> linux_user(TaskPrincipal(user_id="1000")), linux_user(TaskPrincipal.system())
        ('1000', None)
    """
    if principal.logon_type is not None:
        raise PlatformUnsupportedError("logon_type is Windows-only; systemd has no logon types.")
    if principal.user_id is None or principal.user_id == WellKnownSid.LOCAL_SYSTEM.value:
        return None
    if not principal.is_uid:
        raise PlatformUnsupportedError("on Linux a principal is a decimal uid (or SYSTEM for root), not a Windows SID.")
    if principal.run_level is TaskRunLevel.HIGHEST and principal.user_id != "0":
        raise PlatformUnsupportedError("run level HIGHEST needs root on Linux; there is no split token to raise.")
    return principal.user_id


def _monotonic_bases(bus: _dbus.Bus, timer: str) -> list[str] | None:
    """The base names of a timer's monotonic timers (``Timer.TimersMonotonic``), None if unreadable.

    ``LoadUnit`` rather than ``GetUnit``: a disabled, stopped timer is usually unloaded, and this
    reads ANY timer the same way - pwshpy's own and one it did not write - from what systemd parsed.
    """
    raw = _dbus.path_property(
        bus, _dbus.load_unit_path(bus, timer), "TimersMonotonic", None, interface=_dbus.TIMER_IFACE
    )
    if not isinstance(raw, list):
        return None
    entries = cast("list[tuple[Any, ...]]", raw)  # a(stt): (base, offset usec, next elapse usec)
    return [str(entry[0]) for entry in entries]


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

    def register(  # noqa: PLR0913 - mirrors Register-ScheduledTask's independent, all-optional parameters
        self,
        task_path: str,
        *,
        program: str,
        arguments: str = "",
        description: str = "",
        principal: TaskPrincipal | None = None,
        triggers: Sequence[TaskTrigger] = (),
    ) -> ScheduledTaskInfo:
        """Create a oneshot ``.service`` and, with triggers, an enabled ``.timer``; return the task.

        Registering over an existing task replaces it (its old ``.timer`` is stopped first and
        removed when ``triggers`` is empty), like Task Scheduler's create-or-update.

        Args:
            task_path: The task identifier; reduced to the unit base name (:func:`base_name`).
            program: Executable the service's ``ExecStart=`` runs.
            arguments: Command-line arguments for ``program``.
            description: The units' ``Description=``.
            principal: Who the task runs as; None or SYSTEM is root, a decimal uid is ``User=``.
            triggers: When the task fires; empty means on demand only.

        Returns:
            The registered task, read back.

        Raises:
            PlatformUnsupportedError: the principal or a trigger has no systemd analog
                (:func:`linux_user`, :func:`timer_unit`, :func:`arms_on_register`).
            ConfigurationError: ``task_path``, ``program``, ``arguments`` or ``description``
                holds a line break.
            NativeCallError: a unit file could not be written or a D-Bus call failed.

        Example:
            >>> SystemdScheduledTaskController().register(
            ...     "job", program="/bin/true", principal=TaskPrincipal(user_id="S-1-5-21-1-2-3-1001")
            ... )
            Traceback (most recent call last):
            ...
            pwshpy.domain.errors.PlatformUnsupportedError: on Linux a principal is a decimal uid ...
        """
        base = _single_line(base_name(task_path), "task_path")
        # Validate and render everything first: a refusal must not leave a half-written task or cost a bus.
        service_text = service_unit(program, arguments, description, user=linux_user(principal or TaskPrincipal()))
        timer_text = timer_unit(base, description, triggers) if triggers else None
        arm_now = arms_on_register(triggers)
        timer = f"{base}.timer"
        timer_file = _UNIT_DIR / timer
        bus = _dbus.open_bus()
        try:
            if timer_file.exists():  # a previous registration: stop its schedule before replacing it
                self._retire_timer(bus, timer, remove=timer_text is None)
            self._write(_UNIT_DIR / f"{base}.service", service_text)
            if timer_text is not None:
                self._write(timer_file, timer_text)
            _dbus.call(bus, "Reload")  # load the new unit files before enabling them
            if timer_text is not None:
                _dbus.call(bus, "EnableUnitFiles", "asbb", ([timer], False, False))
                # Reload again, as systemctl enable does: else the new timers.target Wants= link is
                # not picked up and every new unit reads NeedDaemonReload ("changed on disk").
                _dbus.call(bus, "Reload")
                if arm_now:
                    _dbus.call(bus, "StartUnit", "ss", (timer, "replace"))
            return self._info(bus, base)
        finally:
            bus.conn.close()

    @staticmethod
    def _write(path: Path, text: str) -> None:
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise NativeCallError(f"cannot write unit {path}: {exc}") from exc

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise NativeCallError(f"cannot delete unit {path}: {exc}") from exc

    @classmethod
    def _retire_timer(cls, bus: _dbus.Bus, timer: str, *, remove: bool) -> None:
        """Stop a pwshpy ``.timer``; with ``remove`` also disable it and delete its unit file."""
        with contextlib.suppress(NativeCallError):  # not loaded -> nothing is scheduled to stop
            _dbus.call(bus, "StopUnit", "ss", (timer, "replace"))
        path = _UNIT_DIR / timer
        if not remove or not path.exists():  # no file under /etc: not ours (a vendor timer), never disable it
            return
        # Disable while the unit file still exists, so its timers.target.wants link goes with it.
        # Not suppressed: disabling a never-enabled unit succeeds, and "no such unit" cannot occur
        # with the file present, so any error is real and must stop us before the file is deleted.
        _dbus.call(bus, "DisableUnitFiles", "asb", ([timer], False))
        cls._remove(path)

    def unregister(self, task_path: str) -> None:
        """Stop and delete a task's units (only the files pwshpy could have written, under /etc)."""
        base = base_name(task_path)
        bus = _dbus.open_bus()
        try:
            self._retire_timer(bus, f"{base}.timer", remove=True)
            service = f"{base}.service"
            with contextlib.suppress(NativeCallError):  # not loaded / already gone -> nothing to stop
                _dbus.call(bus, "StopUnit", "ss", (service, "replace"))
            self._remove(_UNIT_DIR / service)
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
        """Arm a task's ``.timer`` so it runs on schedule (like Enable-ScheduledTask).

        A timer with a boot-relative part (``OnBootSec=`` / ``OnStartupSec=``) is enabled but not
        started (:func:`starts_when_armed`): started now, its elapsed boot offset would run the
        job at once. It arms at the next boot. A timer whose monotonic timers cannot be read is
        treated the same way - never running the job unexpectedly is the safe side.

        Raises:
            NativeCallError: the task is on-demand (no ``.timer``) or its timer is static (no
                ``[Install]`` section), or a D-Bus call failed.
        """
        base = base_name(task_path)
        timer = f"{base}.timer"
        bus = _dbus.open_bus()
        try:
            self._require_timer(bus, base, "enable")
            bases = _monotonic_bases(bus, timer)
            _dbus.call(bus, "EnableUnitFiles", "asbb", ([timer], False, False))
            _dbus.call(bus, "Reload")  # as register does: the new timers.target link is loaded before the start
            if bases is not None and starts_when_armed(bases):
                _dbus.call(bus, "StartUnit", "ss", (timer, "replace"))
            return self._info(bus, base)
        finally:
            bus.conn.close()

    def disable(self, task_path: str) -> ScheduledTaskInfo:
        """Disarm a task's ``.timer`` so it no longer runs on schedule (like Disable-ScheduledTask).

        Raises:
            NativeCallError: the task is on-demand (no ``.timer``) or its timer is static (no
                ``[Install]`` section) - refused before anything is stopped - or a D-Bus call failed.
        """
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
        """Refuse enable/disable for a task whose schedule systemd cannot toggle.

        A task with no ``.timer`` is on-demand: there is nothing to toggle. A static timer has no
        ``[Install]`` section, so ``EnableUnitFiles``/``DisableUnitFiles`` succeed and change nothing:
        the task would read back enabled as if the call had worked (and disable would have stopped
        the timer on the way, until whatever pulls it in starts it again).
        """
        if not _has_timer(bus, base):
            raise NativeCallError(
                f"task {base!r} is on-demand (no .timer); nothing to {verb} - run it directly, or unregister it."
            )
        if _dbus.unit_file_state(bus, f"{base}.timer") == "static":
            raise NativeCallError(
                f"task {base!r} is a static timer (no [Install] section; another unit pulls it in), so systemd "
                f"has nothing to {verb} - change the unit that wants it, or mask the timer."
            )

    def _start_stop(self, base: str, method: str) -> None:
        bus = _dbus.open_bus()
        try:
            _dbus.call(bus, method, "ss", (f"{base}.service", "replace"))
        finally:
            bus.conn.close()

    def _info(self, bus: _dbus.Bus, base: str) -> ScheduledTaskInfo:
        """Snapshot a task's state - timer-based if it has a ``.timer``, else the on-demand service.

        Units are resolved with ``LoadUnit``, never ``GetUnit``: a just-registered boot timer
        (enabled, not started), a disabled timer and a finished oneshot are all unloaded, and
        ``GetUnit`` would read each of their fields as a default (description and user ``""``).
        Each unit is resolved once for every property read from it.
        """
        if _has_timer(bus, base):
            unit = f"{base}.timer"
            state = _dbus.unit_file_state(bus, unit) or ""
            return _timer_task(bus, unit, _dbus.load_unit_path(bus, unit), file_state=state)
        path = _dbus.load_unit_path(bus, f"{base}.service")
        result, started = _service_run(bus, path)
        return ScheduledTaskInfo.model_construct(
            task_name=base,
            task_path=_ROOT,
            state=service_task_state(str(_dbus.path_property(bus, path, "ActiveState", "inactive"))),
            enabled=True,  # an on-demand oneshot has no schedule to disable; it can always be run
            author="",
            description=str(_dbus.path_property(bus, path, "Description", "")),
            user_id=_service_user(bus, path),
            run_level=None,  # systemd has no split token, so no run level to report
            last_run_time=usec_to_datetime(started),  # no timer: the last run is the service's own last start
            last_task_result=result,
            next_run_time=None,  # on demand: nothing is scheduled
        )


__all__ = [
    "SystemdScheduledTaskController",
    "arms_on_register",
    "base_name",
    "iter_timers",
    "latest_usec",
    "linux_user",
    "service_last_result",
    "service_task_state",
    "service_unit",
    "service_user_id",
    "starts_when_armed",
    "timer_enabled",
    "timer_task_state",
    "timer_to_task_state",
    "timer_unit",
    "usec_to_datetime",
]
