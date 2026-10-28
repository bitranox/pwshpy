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
* **settings** (:class:`TaskSettings`) become keys of the ``.service``: ``ConditionACPower=true``
  (no start on battery), ``RefuseManualStart=yes`` (no start on demand; the timer still starts
  it), ``TimeoutStartSec=`` (the time limit; a oneshot's start job lasts the whole run) and
  ``SendSIGKILL=no`` (no hard terminate). A value systemd has no key for is refused
  (:func:`check_settings`); a None field writes nothing. Read back, each record carries the
  effective values from the triggered service, each field unknown (None) on its own when its
  property cannot be read.

Shared D-Bus plumbing lives in :mod:`.systemd_dbus`. Mutation writes unit files under
``/etc/systemd/system`` (needs root, like the Windows verbs need admin) and never touches a
vendor unit under ``/lib``.

Contents:
    * :func:`timer_to_task_state` / :func:`timer_enabled` - pure systemd -> domain mappers.
    * :class:`TimerBase` / :func:`starts_when_armed` - a monotonic timer's base, and whether arming
      a timer with those bases may start it now.
    * :func:`usec_to_datetime` / :func:`service_last_result` / :func:`latest_usec` - pure run-info
      mappers.
    * :func:`service_user_id` - a service's ``User=`` as a decimal uid.
    * :func:`battery_start_allowed` / :func:`start_time_limit` - pure settings read-back mappers.
    * :func:`check_settings` - refuse the :class:`TaskSettings` values systemd has no key for.
    * :func:`service_unit` / :func:`timer_unit` / :func:`linux_user` / :func:`arms_on_register` -
      pure unit-file rendering and register policy.
    * :func:`iter_timers` - stream the ``.timer`` units as records.
    * :class:`SystemdScheduledTaskController` - register/unregister, run/stop, enable/disable.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple, cast

from pydantic import ValidationError

from ... import __init__conf__
from ...domain.enums import TaskMultipleInstances, TaskRunLevel, TaskState, WellKnownSid
from ...domain.errors import ConfigurationError, NativeCallError, PlatformUnsupportedError
from ...domain.records import ScheduledTaskInfo
from ...domain.scheduling import BootTrigger, DailyTrigger, OnceTrigger, TaskPrincipal, TaskSettings, TaskTrigger
from . import systemd_dbus as _dbus
from .marshal import native_error, skip_or_raise
from .systemd_dbus import ActiveState, LoadState, UnitFileState

logger = logging.getLogger(__name__)

_ROOT = "/"  # systemd timers are a flat namespace; task_path is this neutral root (no folder tree)
_UNIT_DIR = Path("/etc/systemd/system")  # where pwshpy writes its own units (system scope, needs root)
#: The [Unit] key that marks a unit file as one pwshpy wrote; its value is the pwshpy version that
#: wrote it. systemd ignores a key starting with "X-" without a warning (systemd.unit(5); measured with
#: systemd-analyze verify on 259, where an unknown key without the prefix warns).
OWNERSHIP_KEY = "X-PwshPy"

#: The characters systemd allows in a unit name prefix (systemd.unit(5), "Description"): ASCII
#: letters, digits, ":", "-", "_", "." and a backslash. The names pwshpy registers leave out the
#: backslash (it reaches a name only as an escape, see :data:`_UNIT_NAME_ESCAPE`) and "@" on purpose:
#: "@" makes a template or instance unit, which pwshpy does not register.
_UNIT_NAME_PREFIX = re.compile(r"[A-Za-z0-9:_.\-]+")

#: systemd's escape for a character a unit name cannot hold (systemd.unit(5), "String Escaping for
#: Inclusion in Unit Names"; ``systemd-escape``): a backslash, "x" and two hex digits, such as ``\x2d``
#: for "-" in ``zfs-scrub-monthly@my\x2dpool``.
_UNIT_NAME_ESCAPE = r"\\x[0-9A-Fa-f]{2}"

#: A folder separator in a task path that does not start with a backslash (a listed unit name, or a
#: "/" path): "/", or a backslash that does not start an escape.
_FOLDER_SEPARATOR = re.compile(r"/|\\(?!x[0-9A-Fa-f]{2})")

#: A folder separator in the Windows form ``\Folder\Name`` (it starts with a backslash), which a
#: portable script may pass on Linux: every backslash and "/", as in 1.5.2. No listed unit name starts
#: with a backslash, so ``\Maintenance\xfer_logs`` is the task ``xfer_logs``, never an escape.
_WINDOWS_FOLDER_SEPARATOR = re.compile(r"[\\/]")

#: A unit name prefix as systemd may list it: the prefix characters or escapes (a backslash only as
#: part of one, the only form that survives :func:`base_name`).
_LISTED_UNIT_NAME_PREFIX = re.compile(rf"(?:[A-Za-z0-9:_.\-]|{_UNIT_NAME_ESCAPE})+")

#: The characters systemd allows in an instance name (after the first "@"): the prefix characters,
#: escapes included, plus "@" itself (systemd's unit_instance_is_valid; ``systemd-escape`` turns "/"
#: into "-", so ``btrfs-scrub@-.timer`` is the instance for "/").
_UNIT_NAME_INSTANCE = re.compile(rf"(?:[A-Za-z0-9:_.\-@]|{_UNIT_NAME_ESCAPE})+")

#: The longest unit name systemd accepts, suffix included: UNIT_NAME_MAX is 256 with the NUL
#: (src/basic/unit-name.h; measured on systemd 259: 255 characters load, 256 are "not valid").
_UNIT_NAME_MAX = 255

# Run-info sentinels, read on a container host with busctl; re-checked on a root Linux VM in Task 6.
_USEC_INFINITY = 2**64 - 1  # systemd's USEC_INFINITY: "never elapses"
_CLD_EXITED = 1  # ExecMainCode: the main process exited (status = exit code); 2/3 = killed/dumped (status = signal)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: systemd timer ActiveState -> TaskState. An armed timer is READY (waiting to fire); a stopped
#: or failed timer is DISABLED. RUNNING (the triggered job executing) is Windows-only granularity.
_TIMER_STATE = {
    ActiveState.ACTIVE: TaskState.READY,
    ActiveState.ACTIVATING: TaskState.QUEUED,
    ActiveState.INACTIVE: TaskState.DISABLED,
    ActiveState.DEACTIVATING: TaskState.DISABLED,
    ActiveState.FAILED: TaskState.DISABLED,
}

#: UnitFileStates of an armed timer (:func:`timer_enabled`).
_ENABLED_FILE_STATES = frozenset({UnitFileState.ENABLED, UnitFileState.ENABLED_RUNTIME, UnitFileState.STATIC})

#: ActiveStates in which an on-demand service task is RUNNING (:func:`service_task_state`).
_SERVICE_RUNNING_STATES = frozenset({ActiveState.ACTIVE, ActiveState.ACTIVATING})


def timer_to_task_state(active_state: ActiveState) -> TaskState:
    """Map a systemd timer ``ActiveState`` to a :class:`TaskState` (an unmapped one is Unknown).

    Example:
        >>> timer_to_task_state(ActiveState.ACTIVE).value, timer_to_task_state(ActiveState.INACTIVE).value
        ('Ready', 'Disabled')
    """
    return _TIMER_STATE.get(active_state, TaskState.UNKNOWN)


def timer_enabled(unit_file_state: UnitFileState) -> bool:
    """Whether a timer's ``UnitFileState`` counts as enabled (armed, it runs on schedule).

    ``static`` counts as enabled: a static timer has no ``[Install]`` section, so it cannot be
    disabled, and another unit pulls it in, so it is armed and fires (``systemd-tmpfiles-clean``).
    ``disabled`` and ``masked`` are not enabled.

    Example:
        >>> timer_enabled(UnitFileState.STATIC), timer_enabled(UnitFileState.DISABLED)
        (True, False)
    """
    return unit_file_state in _ENABLED_FILE_STATES


def timer_task_state(active_state: ActiveState, *, enabled: bool) -> TaskState:
    """A timer task's state: an enabled but inactive timer is READY, else :func:`timer_to_task_state`.

    An enabled timer that is not running is armed for the next boot (timers.target starts it),
    which is what a freshly registered boot task looks like - waiting to fire, not disabled.

    Example:
        >>> timer_task_state(ActiveState.INACTIVE, enabled=True).value
        'Ready'
        >>> timer_task_state(ActiveState.INACTIVE, enabled=False).value
        'Disabled'
    """
    if active_state is ActiveState.INACTIVE and enabled:
        return TaskState.READY
    return timer_to_task_state(active_state)


class TimerBase(str, Enum):
    """A monotonic timer's base (the first field of a ``Timer.TimersMonotonic`` entry); else :attr:`UNKNOWN`.

    systemd names each base after its ``On*Sec=`` setting with a ``USec`` suffix (systemd.timer(5)).
    A base a later systemd adds reads as UNKNOWN, which is not boot-relative, as an unnamed text
    value was not before the bases were typed.

    Example:
        >>> TimerBase("OnBootUSec") is TimerBase.ON_BOOT, TimerBase("OnFutureUSec") is TimerBase.UNKNOWN
        (True, True)
    """

    ON_ACTIVE = "OnActiveUSec"
    ON_BOOT = "OnBootUSec"
    ON_STARTUP = "OnStartupUSec"
    ON_UNIT_ACTIVE = "OnUnitActiveUSec"
    ON_UNIT_INACTIVE = "OnUnitInactiveUSec"
    UNKNOWN = "unknown"  # not a systemd value: the fallback for one this list does not name

    @classmethod
    def _missing_(cls, value: object) -> TimerBase:
        return cls.UNKNOWN


#: Monotonic timer bases that systemd fires at once when the timer is started after they have
#: elapsed - OnBootSec= / OnStartupSec= (systemd.timer(5)).
_BOOT_RELATIVE = frozenset({TimerBase.ON_BOOT, TimerBase.ON_STARTUP})


def starts_when_armed(monotonic_bases: Iterable[TimerBase]) -> bool:
    """Whether arming a timer may also start it now: False when any monotonic timer is boot-relative.

    The one decision behind both ``register`` (from the triggers) and ``enable`` (from the
    timer's live ``TimersMonotonic``): a started boot-relative timer whose boot offset is already
    past runs its job immediately, so such a timer is only enabled and waits for the next boot.

    Example:
        >>> starts_when_armed([TimerBase.ON_UNIT_ACTIVE]), starts_when_armed([TimerBase.ON_BOOT])
        (True, False)
    """
    return _BOOT_RELATIVE.isdisjoint(monotonic_bases)


def service_task_state(active_state: ActiveState) -> TaskState:
    """State for an on-demand ``.service`` task: running -> RUNNING, else READY (it can always be run)."""
    if active_state in _SERVICE_RUNNING_STATES:
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


#: The spellings systemd's parse_boolean reads as true, case-insensitively (src/basic/parse-util.c), so
#: a hand-written ``ConditionACPower=yes`` reads like the ``true`` pwshpy writes.
_SYSTEMD_TRUE = frozenset({"1", "yes", "y", "true", "t", "on"})


def _is_struct(entry: object, kinds: tuple[type, ...]) -> bool:
    """Whether ``entry`` is a D-Bus struct (a tuple) whose fields have exactly the types ``kinds``."""
    if not isinstance(entry, tuple):
        return False
    fields = cast("tuple[object, ...]", entry)
    return len(fields) == len(kinds) and all(type(f) is k for f, k in zip(fields, kinds, strict=True))


def battery_start_allowed(conditions: object) -> bool | None:
    """``allow_start_if_on_batteries`` from a unit's ``Conditions`` property (D-Bus ``a(sbbsi)``).

    False when an entry is ``ConditionACPower=true`` as written - not a triggering (``|``) and not
    a negated (``!``) condition - because then a run starts only on AC power; True otherwise. Each
    entry is (type, trigger, negate, parameter, state); the state and the unit's
    ``ConditionResult`` are never read: both read "no" after a oneshot finishes whether it ran or
    was skipped (measured on systemd 259).

    Args:
        conditions: The property value, or None when it could not be read.

    Returns:
        The effective value, or None when ``conditions`` is unreadable or not an ``a(sbbsi)``.

    Example:
        >>> battery_start_allowed([("ConditionACPower", False, False, "true", 0)]), battery_start_allowed([])
        (False, True)
        >>> battery_start_allowed(None) is None
        True
    """
    if not isinstance(conditions, list):
        return None
    entries = cast("list[object]", conditions)
    if not all(_is_struct(entry, (str, bool, bool, str, int)) for entry in entries):
        return None
    for entry in cast("list[tuple[str, bool, bool, str, int]]", entries):
        kind, trigger, negate, parameter, _state = entry
        if kind == "ConditionACPower" and not trigger and not negate and _is_systemd_true(parameter):
            return False
    return True


def _is_systemd_true(text: str) -> bool:
    return text.lower() in _SYSTEMD_TRUE


def start_time_limit(service_type: object, timeout_start_usec: object) -> timedelta | None:
    """``execution_time_limit`` from a service's ``Type`` and ``TimeoutStartUSec`` (D-Bus ``s``, ``t``).

    Only a ``Type=oneshot`` service's start job lasts the whole run, so only there does the start
    timeout bound the run (measured on 259: ``TimeoutStartSec=2`` ends a 30 s oneshot with
    Result=timeout); for any other type it bounds only the start, and the run limit is unknown.
    ``UINT64_MAX`` - what ``infinity``, ``0`` and an unset oneshot all read - is ``timedelta(0)``,
    no limit. The microseconds are kept as systemd holds them, a fraction included.

    Args:
        service_type: The ``Type`` property, or None when it could not be read.
        timeout_start_usec: The ``TimeoutStartUSec`` property, or None when it could not be read.

    Returns:
        The limit, or None when either value is unreadable, has the wrong type, or the service is
        no oneshot.

    Example:
        >>> start_time_limit("oneshot", 2**64 - 1), start_time_limit("oneshot", 2_000_000)
        (datetime.timedelta(0), datetime.timedelta(seconds=2))
        >>> start_time_limit("simple", 90_000_000) is None
        True
    """
    if service_type != "oneshot" or type(timeout_start_usec) is not int:
        return None
    if timeout_start_usec == _USEC_INFINITY:
        return timedelta(0)
    return timedelta(microseconds=timeout_start_usec)


def _service_settings(bus: _dbus.Bus, service_path: str | None) -> TaskSettings:
    """The effective :class:`TaskSettings` of the service at ``service_path`` (None: nothing readable).

    Each field degrades to None on its own when its property cannot be read; the task is kept.
    ``stop_if_going_on_batteries`` is always False: systemd never stops a running unit when the
    power source changes. ``multiple_instances`` is None: a repeating timer's catch-up run after an
    overrun (measured on 259) is none of the four policies. ``TimeoutStartUSec`` is read only for a
    oneshot (:func:`start_time_limit`), saving the read where it would mean nothing.
    ``allow_demand_start`` is the inverse of ``RefuseManualStart``.
    """

    def read(prop: str, interface: str = _dbus.SERVICE_IFACE) -> object:
        return cast("object", _dbus.path_property(bus, service_path, prop, None, interface=interface))

    service_type = read("Type")
    send_sigkill = read("SendSIGKILL")
    refuse_manual_start = read("RefuseManualStart", _dbus.UNIT_IFACE)
    # model_construct: these are systemd's values, not a caller's; the input bounds describe what
    # register writes, not every value a hand-written unit can hold.
    return TaskSettings.model_construct(
        allow_start_if_on_batteries=battery_start_allowed(read("Conditions", _dbus.UNIT_IFACE)),
        stop_if_going_on_batteries=False,
        execution_time_limit=start_time_limit(
            service_type, read("TimeoutStartUSec") if service_type == "oneshot" else None
        ),
        multiple_instances=None,
        allow_hard_terminate=send_sigkill if isinstance(send_sigkill, bool) else None,
        allow_demand_start=not refuse_manual_start if isinstance(refuse_manual_start, bool) else None,
    )


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
    settings: TaskSettings


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
    The settings are the service's too: the timer only decides when it starts.
    """
    service = str(_dbus.path_property(bus, timer_path, "Unit", f"{base}.service", interface=_dbus.TIMER_IFACE))
    service_path = _dbus.load_unit_path(bus, service)
    result, started = _service_run(bus, service_path)
    return _RunInfo(
        last_run_time=usec_to_datetime(latest_usec(_timer_property(bus, timer_path, "LastTriggerUSec"), started)),
        last_task_result=result,
        next_run_time=usec_to_datetime(_timer_property(bus, timer_path, "NextElapseUSecRealtime")),
        user_id=_service_user(bus, service_path),
        settings=_service_settings(bus, service_path),
    )


def _timer_task(
    bus: _dbus.Bus,
    name: str,
    path: str | None,
    *,
    file_state: UnitFileState,
    listed: _dbus.ListedUnit | None = None,
) -> ScheduledTaskInfo:
    """The record for timer ``name`` at its resolved object ``path`` (None: unreadable, fields default).

    ``listed`` is the timer's ``ListUnits`` entry, whose ActiveState and Description no property
    read repeats; without it (a unit read through ``LoadUnit``) both are read from ``path``.
    """
    if listed is None:
        active_state = _dbus.path_active_state(bus, path)
        description = str(_dbus.path_property(bus, path, "Description", ""))
    else:
        active_state, description = listed.active_state, listed.description
    base = name[: -len(".timer")]
    run = _timer_run_info(bus, path, base)
    enabled = timer_enabled(file_state)
    return ScheduledTaskInfo.model_construct(
        task_name=base,
        task_path=_ROOT,
        state=timer_task_state(active_state, enabled=enabled),
        enabled=enabled,
        author="",
        description=description,
        user_id=run.user_id,
        run_level=None,  # systemd has no split token, so no run level to report
        last_run_time=run.last_run_time,
        last_task_result=run.last_task_result,
        next_run_time=run.next_run_time,
        settings=run.settings,
    )


def _service_task(bus: _dbus.Bus, base: str, path: str | None) -> ScheduledTaskInfo:
    """The record for the on-demand task ``base``: its ``.service`` at object ``path`` (None: fields default).

    A task with no ``.timer`` runs only when started, so it is always enabled, has nothing
    scheduled, and its last run is the service's own last start.
    """
    result, started = _service_run(bus, path)
    return ScheduledTaskInfo.model_construct(
        task_name=base,
        task_path=_ROOT,
        state=service_task_state(_dbus.path_active_state(bus, path)),
        enabled=True,  # an on-demand oneshot has no schedule to disable; it can always be run
        author="",
        description=str(_dbus.path_property(bus, path, "Description", "")),
        user_id=_service_user(bus, path),
        run_level=None,  # systemd has no split token, so no run level to report
        last_run_time=usec_to_datetime(started),  # no timer: the last run is the service's own last start
        last_task_result=result,
        next_run_time=None,  # on demand: nothing is scheduled
        settings=_service_settings(bus, path),
    )


def _folder_separator(path: str) -> re.Pattern[str]:
    """The folder separator for a stripped task path: the Windows form's when it starts with a backslash.

    The one choice :func:`base_name` splits by and :func:`_check_task_name` refuses by, so the guard
    can never accept a folder the splitter would then drop.

    Args:
        path: The task path or name, already stripped of surrounding whitespace.

    Returns:
        :data:`_WINDOWS_FOLDER_SEPARATOR` for the ``\\Folder\\Name`` form, else :data:`_FOLDER_SEPARATOR`.

    Example:
        >>> _folder_separator(r"\\Maintenance\\xfer_logs") is _WINDOWS_FOLDER_SEPARATOR
        True
        >>> _folder_separator(r"zfs-scrub-monthly@my\\x2dpool") is _FOLDER_SEPARATOR
        True
    """
    return _WINDOWS_FOLDER_SEPARATOR if path.startswith("\\") else _FOLDER_SEPARATOR


def _check_task_name(task_name: str) -> None:
    """Refuse a ``task_name`` that names something other than one task, as the Windows lookup does.

    An empty name names no task, and a folder part would be dropped silently by :func:`base_name`
    (``tasks/job`` would answer for ``job``). The folder separators are the ones :func:`base_name`
    splits on; a systemd ``\\xNN`` escape is part of a name, not a separator, because the listing
    reports instance timers such as ``zfs-scrub-monthly@my\\x2dpool`` and the lookup must reach them.
    So unlike the Windows rule (every backslash) a backslash that starts an escape is accepted here.

    Raises:
        ConfigurationError: ``task_name`` is empty or contains a folder separator.

    Example:
        >>> _check_task_name("tasks/job")
        Traceback (most recent call last):
        ...
        pwshpy.domain.errors.ConfigurationError: task_name 'tasks/job' contains a folder: ...
    """
    if task_name == "":
        raise ConfigurationError("task_name must not be empty: an empty name names no task")
    stripped = task_name.strip()
    if _folder_separator(stripped).search(stripped):
        raise ConfigurationError(
            f"task_name {task_name!r} contains a folder: systemd timers have no folders, "
            "so pass only the unit name (folder_path is ignored on Linux)"
        )


def _is_absent(bus: _dbus.Bus, loaded: _dbus.LoadedUnit, file_state: UnitFileState) -> bool:
    """Whether the ``loaded`` unit is no task, by the listing's rule: masked counts unless running.

    The listing lists a ``ListUnits`` entry unless :func:`~.systemd_dbus.is_unlisted_unit` says no, and
    an unloaded timer unless its file is masked. Here the unit was loaded just now, so the LoadState
    read with it answers for both - and a masked file is read as masked even when
    ``GetUnitFileState`` failed. A masked file state with a stale LoadState (masked after it was
    loaded) is masked too.
    """
    load_state = LoadState.MASKED if file_state in _dbus.UNLISTED_FILE_STATES else loaded.load_state
    return _dbus.is_unlisted_unit(load_state, _dbus.path_active_state(bus, loaded.path))


def _load_named_unit(bus: _dbus.Bus, unit: str) -> _dbus.LoadedUnit | None:
    """``unit`` loaded, or None when it has no unit file or is an alias of another unit.

    ``LoadUnit`` of an alias returns the object of the unit it names, whose ``Id`` is that unit's
    name, so an ``Id`` other than ``unit`` is an alias even when ``GetUnitFileState`` could not say
    "alias": the listing reports the real unit under its own name, never under the alias.

    Raises:
        NativeCallError: The ``LoadUnit`` call, its ``LoadState`` read or the ``Id`` read fails.
    """
    loaded = _dbus.load_unit_state(bus, unit)
    if loaded is None:
        return None
    unit_id = _dbus.path_property(bus, loaded.path, "Id", None)
    if unit_id is None:
        raise NativeCallError(f"cannot read the Id of '{unit}'")
    return loaded if str(unit_id) == unit else None


def _owned_service(bus: _dbus.Bus, unit: str, path: str, *, strict: bool) -> bool:
    """Whether the on-demand ``unit`` at ``path`` is a task: its unit file carries :data:`OWNERSHIP_KEY`.

    A ``.service`` alone is a task only when pwshpy registered it (USER decision for [50]): every
    system daemon is a ``.service`` without a ``.timer``. The file is the one systemd loaded the unit
    from (``FragmentPath``); a unit with none (``""``: transient or generated) is not pwshpy's. A
    ``FragmentPath`` systemd does not answer, or a file that cannot be read, goes through the
    skip-or-raise rule: ownership that cannot be read is no answer, unlike a service that is not ours,
    which is no task and is left out silently.
    """
    fragment = _dbus.path_property(bus, path, "FragmentPath", None)
    if fragment is None:
        skip_or_raise(
            f"cannot read the unit file path of '{unit}'",
            NativeCallError("systemd did not answer the FragmentPath read"),
            strict=strict,
            log=logger,
        )
        return False
    if str(fragment) == "":
        return False
    try:
        text = Path(str(fragment)).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        skip_or_raise(f"cannot read the unit file of '{unit}' ({fragment})", exc, strict=strict, log=logger)
        return False
    return is_pwshpy_unit(text)


def _lookup_unit(bus: _dbus.Bus, base: str, *, strict: bool) -> Iterator[ScheduledTaskInfo]:
    """The timer task ``base``, else the on-demand service pwshpy registered as ``base``, else nothing.

    The same answer the controller reads back after register: a ``.timer`` makes it a scheduled
    task, a ``.service`` alone an on-demand one - but only a ``.service`` whose unit file pwshpy
    wrote (:func:`_owned_service`), or every system daemon would answer as a task. An alias counts
    as absent (:func:`_load_named_unit`), and a masked unit unless it is running, as in the listing
    (:func:`_is_absent`). A unit systemd does not answer for (a refused ``LoadUnit``, ``LoadState``
    or ``Id`` read) ends the lookup through the listing's skip-or-raise rule: the service must not
    stand in for a timer that may exist.
    """
    for unit in (f"{base}.timer", f"{base}.service"):
        state = _dbus.unit_file_state(bus, unit) or UnitFileState.EMPTY
        if state is UnitFileState.ALIAS:  # the name of another unit: the listing reports that unit, never the alias
            continue
        try:
            loaded = _load_named_unit(bus, unit)
        except NativeCallError as exc:
            skip_or_raise(f"cannot load unit '{unit}'", exc, strict=strict, log=logger)
            return
        if loaded is None or _is_absent(bus, loaded, state):
            continue
        if unit.endswith(".timer"):
            yield _timer_task(bus, unit, loaded.path, file_state=state)
        elif _owned_service(bus, unit, loaded.path, strict=strict):
            yield _service_task(bus, base, loaded.path)
        return


def _timer_files(bus: _dbus.Bus, *, strict: bool) -> list[_dbus.UnitFile]:
    """Every ``.timer`` unit file as a :class:`~.systemd_dbus.UnitFile`; [] when no listing answers.

    When no unit-file listing answers, the default keeps the loaded timers with a WARNING (every
    unloaded timer is then missing); ``strict`` raises instead.
    """
    try:
        return _dbus.unit_files(bus, ".timer")
    except NativeCallError as exc:
        skip_or_raise(
            "cannot list the timer unit files, so no unloaded timer is listed", exc, strict=strict, log=logger
        )
        return []


def _timer_file_path(bus: _dbus.Bus, name: str, *, strict: bool) -> str | None:
    """The loaded object path of timer file ``name``; None for a stale file or (by default) a failed load."""
    try:
        return _dbus.load_unit(bus, name)
    except NativeCallError as exc:
        skip_or_raise(f"cannot load timer unit '{name}'", exc, strict=strict, log=logger)
        return None


def iter_timers(
    folder_path: str = _ROOT, *, strict: bool = False, task_name: str | None = None
) -> Iterator[ScheduledTaskInfo]:
    """Yield a :class:`ScheduledTaskInfo` per systemd ``.timer`` unit (like Get-ScheduledTask, Linux).

    ``folder_path`` is accepted for signature parity with the Windows source and ignored (systemd
    timers are flat). Given ``task_name``, nothing is listed: the one task of that name is read
    directly - its ``.timer``, else its on-demand ``.service`` when pwshpy wrote it (the listing,
    timers only, leaves it out; a service whose unit file lacks :data:`OWNERSHIP_KEY`, such as a
    system daemon, is no task), else nothing - the record the controller reads back after register.

    Without ``task_name`` the loaded timers come first, from ``ListUnits``: the timer's object path
    comes from the listing itself, so a loaded timer costs no lookup. Then the timer unit FILES
    not among them - a disabled or not-yet-started timer is unloaded and ``ListUnits`` omits it,
    while Get-ScheduledTask lists a disabled task - each read through ``LoadUnit``. Skipped as no
    task: a stopped ListUnits entry whose LoadState is not-found or masked
    (:func:`~.systemd_dbus.is_unlisted_unit`; a running one is listed), and a masked file, an alias,
    a template or a file ``LoadUnit`` reads as not-found. A skipped entry does not hide its file:
    a name systemd cached as not-found before its file was written is listed from the file
    (systemd >= 246; an older one needs a daemon-reload first). Lazy: the unit-file listing,
    ``GetUnitFileState`` and the run-info reads run only for consumed timers. Reading an unloaded
    timer loads it transiently; systemd unloads it again when nothing references it.

    Args:
        folder_path: Ignored (systemd timers are flat); kept for parity with the Windows source.
        strict: What happens when systemd does not answer a read the listing needs - the
            unit-file listing (without it no unloaded timer can be listed), or the ``LoadUnit`` of
            one timer file or its ``LoadState`` read. False (default) skips what it could not read
            and logs a WARNING naming it; True raises :class:`NativeCallError` naming it, when
            iteration reaches it. The deliberate filtering above (not-found, masked, alias,
            template, stale) is systemd's answer, not a failure, and is the same either way; a
            single property that cannot be read still falls back to its default in both modes
            (the task is kept). With ``task_name`` it decides a unit whose ``LoadUnit`` or
            ``LoadState`` read fails, and an on-demand service whose unit file (``FragmentPath``)
            cannot be read; a task that does not exist, or a service pwshpy did not write, is an
            empty answer either way.
        task_name: Read this one task instead of listing: a unit name as the listing reports it
            (``.timer``/``.service`` suffix optional; an instance with ``\\xNN`` escapes accepted).
            An alias counts as absent, and a masked unit unless it is running, as in the listing.

    Yields:
        One :class:`ScheduledTaskInfo` per timer; with ``task_name``, at most one.

    Raises:
        ConfigurationError: ``task_name`` is empty, contains a folder (``/``, or a backslash that
            starts no ``\\xNN`` escape), or is no systemd unit name (:func:`existing_unit_base`);
            raised before the bus is opened.
        NativeCallError: The system bus or ``ListUnits`` cannot be reached, or (with ``strict``)
            a read named above fails.

    Example:
        >>> callable(iter_timers)
        True
    """
    if task_name is not None:
        _check_task_name(task_name)
        base = existing_unit_base(task_name)
        bus = _dbus.open_bus()
        try:
            yield from _lookup_unit(bus, base, strict=strict)
        finally:
            bus.conn.close()
        return
    bus = _dbus.open_bus()
    try:
        seen: set[str] = set()
        for unit in _dbus.list_units(bus):
            name = unit.name
            if not name.endswith(".timer"):
                continue
            if _dbus.is_unlisted_unit(unit.load_state, unit.active_state):
                # Not marked seen: a cached not-found name whose file appeared since (no
                # daemon-reload) is still listed by the unit-file pass, whose LoadUnit retries the
                # load from disk on systemd >= 246: manager_load_unit_prepare (src/core/manager.c)
                # re-loads a not-found unit when the unit-file cache is stale or was refreshed
                # after its failed load (commits d904afc7 and 7233e91a, both first in v246-rc1;
                # not in NEWS). An older systemd answers not-found until a daemon-reload, so
                # there the unit-file pass skips the name as well.
                continue
            seen.add(name)
            state = _dbus.unit_file_state(bus, name) or UnitFileState.EMPTY
            yield _timer_task(bus, name, unit.path, file_state=state, listed=unit)
        for name, state in _timer_files(bus, strict=strict):
            if name in seen or state in _dbus.UNLISTED_FILE_STATES:
                continue
            seen.add(name)
            path = _timer_file_path(bus, name, strict=strict)
            if path is None:  # a stale or broken file (no unit behind the name), or a refused load
                continue
            yield _timer_task(bus, name, path, file_state=state)
    finally:
        bus.conn.close()


def base_name(task_path: str) -> str:
    """Reduce a task identifier to its systemd unit base name (drop any folder and .timer/.service).

    A path starting with a backslash is the Windows form ``\\Folder\\Name``: every backslash and "/"
    ends a folder (:data:`_WINDOWS_FOLDER_SEPARATOR`). Otherwise a folder ends at "/" or at a backslash,
    except a backslash that starts systemd's ``\\xNN`` escape, which is part of the name
    (:data:`_FOLDER_SEPARATOR`).

    Example:
        >>> base_name(r"\\Maintenance\\xfer_logs"), base_name(r"/zfs-scrub-monthly@my\\x2dpool.timer")
        ('xfer_logs', 'zfs-scrub-monthly@my\\\\x2dpool')
    """
    path = task_path.strip()
    name = _folder_separator(path).split(path)[-1]
    for suffix in (".timer", ".service"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def unit_base(task_path: str) -> str:
    """The unit base name of ``task_path`` (:func:`base_name`), refused unless systemd would accept it.

    Checked before anything is written or sent: systemd refuses such a name only on the bus, after the
    file is on disk, and a name with a space or an empty one read back as a plausible task.

    Raises:
        ConfigurationError: The name is empty, holds a character outside :data:`_UNIT_NAME_PREFIX`
            (an "@" included), or is longer than systemd allows with its ``.service`` suffix.

    Example:
        >>> unit_base("/tasks/backup.timer")
        'backup'
        >>> unit_base("My Backup")
        Traceback (most recent call last):
        ...
        pwshpy.domain.errors.ConfigurationError: 'My Backup' is not a systemd unit name: ...
    """
    base = base_name(task_path)
    if not _UNIT_NAME_PREFIX.fullmatch(base):
        raise ConfigurationError(
            f"{base!r} is not a systemd unit name: use ASCII letters, digits, ':', '-', '_' and '.' only "
            f"('@' would make a template or instance unit).{_backslash_hint(task_path, base)}"
        )
    return _within_unit_name_max(base)


def _backslash_hint(task_path: str, base: str) -> str:
    """How :func:`base_name` read a backslash in ``task_path``, for a refusal naming ``base``; or ''."""
    if task_path.strip().startswith("\\"):
        return " A path starting with a backslash is read as \\Folder\\Name: every backslash ends a folder."
    if "\\" in base:
        return " Here a backslash starts a systemd \\xNN escape, which pwshpy does not register."
    return ""


def existing_unit_base(task_path: str) -> str:
    """The unit base name of a task to act on (run, stop, enable, disable), in systemd's full grammar.

    Unlike :func:`unit_base` (register and unregister, which write and delete files), an instance
    name and systemd's ``\\xNN`` escapes are accepted: ``get_scheduled_task`` lists instance timers
    such as ``zfs-scrub-monthly@rpool.timer`` or ``zfs-scrub-monthly@my\\x2dpool.timer``, and the
    verbs that write no unit file must reach them, as they did in 1.5.2 for the unescaped ones. A bare
    template (``name@``, an empty instance) is no unit systemd can start.

    Raises:
        ConfigurationError: The prefix (before the first "@") is empty or holds a character outside
            :data:`_LISTED_UNIT_NAME_PREFIX`, the instance is empty (a template) or holds a character
            outside :data:`_UNIT_NAME_INSTANCE`, or the name is longer than systemd allows.

    Example:
        >>> existing_unit_base("zfs-scrub-monthly@rpool.timer")
        'zfs-scrub-monthly@rpool'
        >>> existing_unit_base(r"zfs-scrub-monthly@my\\x2dpool.timer")
        'zfs-scrub-monthly@my\\\\x2dpool'
        >>> existing_unit_base("scrub@")
        Traceback (most recent call last):
        ...
        pwshpy.domain.errors.ConfigurationError: 'scrub@' is a template, not a unit: ...
    """
    base = base_name(task_path)
    prefix, at, instance = base.partition("@")
    if not _LISTED_UNIT_NAME_PREFIX.fullmatch(prefix):
        raise ConfigurationError(
            f"{base!r} is not a systemd unit name: the part before any '@' must be non-empty ASCII letters, "
            "digits, ':', '-', '_', '.' and \\xNN escapes."
        )
    if not at:
        return _within_unit_name_max(base)
    if not instance:
        raise ConfigurationError(
            f"{base!r} is a template, not a unit: name one of its instances ({prefix}@<instance>)."
        )
    if not _UNIT_NAME_INSTANCE.fullmatch(instance):
        raise ConfigurationError(
            f"{base!r} is not a systemd unit name: the instance after '@' may hold ASCII letters, digits, "
            "':', '-', '_', '.', '@' and \\xNN escapes only."
        )
    return _within_unit_name_max(base)


def _within_unit_name_max(base: str) -> str:
    """``base``, or ConfigurationError when ``base.service`` is longer than systemd allows."""
    if len(base) + len(".service") > _UNIT_NAME_MAX:
        raise ConfigurationError(
            f"{base[:40]!r}... is too long for a systemd unit name: at most "
            f"{_UNIT_NAME_MAX - len('.service')} characters (systemd allows {_UNIT_NAME_MAX} with the suffix)."
        )
    return base


#: An ASCII control character (C0 or DEL), which systemd does not allow in a command line.
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


def _single_line(text: str, field: str) -> str:
    """``text`` unchanged, or ConfigurationError when it holds a line break, NUL or other control character.

    A unit file is line-oriented, so a newline in a value starts a new directive (``User=0``,
    ``ExecStartPre=``); refusing is the only safe answer - there is no escape for it. The other control
    characters (a tab, an escape, DEL) systemd.service(5) does not allow in a command line, and the
    description and the arguments share that line format, so they are refused too. The value itself is
    not echoed: arguments may carry a secret.
    """
    if "\n" in text or "\r" in text or "\0" in text:
        raise ConfigurationError(f"{field} must not contain a line break or NUL: it would inject unit-file directives.")
    if _CONTROL_CHARACTER.search(text):
        raise ConfigurationError(f"{field} must not contain a control character: systemd does not allow one.")
    return text


def _unit_value(text: str, field: str) -> str:
    """A single-line ``text`` with ``%`` doubled, so systemd reads it literally, not as a specifier."""
    return _single_line(text, field).replace("%", "%%")


#: The characters systemd reads as a prefix of ExecStart='s executable (systemd.service(5), "Command
#: lines", Table 2 on 259): "@" (argv[0]), "-" (ignore the exit code), ":" (no variable expansion),
#: "+" (full privileges, ignoring User=), "!" / "!!" (no privilege drop) and "|" (spawn the command
#: through the user's shell). systemd strips them from the word after unquoting it, so quoting does
#: not keep one literal (``ExecStart="|a/bin/true"`` verifies clean on 259).
_EXEC_PREFIXES = frozenset("@-:+!|")


def _exec_program(program: str) -> str:
    """``program`` as ExecStart='s first word: refused with a leading prefix, else double-quoted.

    Unquoted, systemd splits the line on whitespace, so ``/opt/my app/run`` would run ``/opt/my``
    (measured with systemd-analyze verify on 259). Inside double quotes it reads C escapes, so a
    backslash and a double quote are escaped; ``%`` is doubled like every value (:func:`_unit_value`).

    Raises:
        ConfigurationError: ``program`` is empty, holds a line break or another control character,
            is a lone ``;``, or starts with a prefix.
    """
    # A line break or another control character is refused by _unit_value (systemd.service(5): "control
    # characters are not allowed" in a command line).
    program = _unit_value(program, "program")
    if not program:
        raise ConfigurationError("program must not be empty: it is the executable ExecStart= runs.")
    if program == ";":
        # A standalone ';' separates command lines in ExecStart=, so it never names an executable.
        raise ConfigurationError("program must not be ';': systemd reads it as a command-line separator.")
    if program[0] in _EXEC_PREFIXES:
        raise ConfigurationError(
            f"program must not start with {program[0]!r}: systemd reads it as an ExecStart= prefix "
            "('+' would run it with full privileges, ignoring User=, '|' through a shell); give the "
            "executable's path."
        )
    return '"' + program.replace("\\", "\\\\").replace('"', '\\"') + '"'


def check_settings(settings: TaskSettings | None, triggers: Sequence[TaskTrigger]) -> None:
    """Refuse a :class:`TaskSettings` value systemd has no key for, given the task's ``triggers``.

    Accepted, and written as nothing because systemd already behaves so: a start allowed on
    battery, no stop when going on battery, hard terminate allowed, a start on demand allowed, and
    IgnoreNew for a task without a daily trigger - a start requested while the oneshot runs joins
    the running start job, so one run happens (measured on 259 for StartUnit); a once or boot
    timer does not repeat, so no catch-up run can follow an overrun. ``allow_demand_start=False``
    has a key (``RefuseManualStart=yes``), so it is accepted too.

    Raises:
        PlatformUnsupportedError: ``stop_if_going_on_batteries`` is True, ``multiple_instances``
            is anything but IgnoreNew, or it is IgnoreNew and ``triggers`` hold a daily trigger.

    Example:
        >>> check_settings(TaskSettings(stop_if_going_on_batteries=True), ())
        Traceback (most recent call last):
        ...
        pwshpy.domain.errors.PlatformUnsupportedError: stop_if_going_on_batteries=True has no systemd analog ...
    """
    if settings is None:
        return
    if settings.stop_if_going_on_batteries:
        raise PlatformUnsupportedError(
            "stop_if_going_on_batteries=True has no systemd analog on Linux: ConditionACPower= is "
            "checked only when a run starts, and systemd never stops a running unit when the power "
            "source changes."
        )
    policy = settings.multiple_instances
    if policy is None:
        return
    if policy is not TaskMultipleInstances.IGNORE_NEW:
        raise PlatformUnsupportedError(
            f"multiple_instances={policy.value} has no systemd analog on Linux: a start requested "
            "while the task runs joins the running start job (one run, as IgnoreNew); systemd "
            "cannot run instances in parallel, queue them or stop the running one for a new start."
        )
    if any(isinstance(trigger, DailyTrigger) for trigger in triggers):
        raise PlatformUnsupportedError(
            "multiple_instances=IgnoreNew has no systemd analog for a daily trigger on Linux: when a "
            "run outlasts its day, the timer fires one catch-up run as soon as it ends instead of "
            "skipping the missed day. Leave multiple_instances unset for a daily task."
        )


def _settings_keys(settings: TaskSettings | None) -> tuple[str, str]:
    """The [Unit] and the [Service] lines ``settings`` add; empty for each field systemd already matches."""
    if settings is None:
        return "", ""
    unit = "ConditionACPower=true\n" if settings.allow_start_if_on_batteries is False else ""
    if settings.allow_demand_start is False:
        # StartUnit (the run verb) is then refused, "Operation refused, unit ... may be requested by
        # dependency only"; the timer still starts the service (both measured on 259). StopUnit
        # stays allowed: only RefuseManualStop= gates it.
        unit += "RefuseManualStart=yes\n"
    service = ""
    limit = settings.execution_time_limit
    if limit is not None:
        # Whole seconds (TaskSettings refuses a fraction); a oneshot's start job lasts the whole run,
        # so TimeoutStartSec bounds it. Never RuntimeMaxSec: no effect on a oneshot (measured, 259).
        seconds = int(limit.total_seconds())
        service += f"TimeoutStartSec={seconds if seconds else 'infinity'}\n"
    if settings.allow_hard_terminate is False:
        service += "SendSIGKILL=no\n"
    return unit, service


def service_unit(
    program: str,
    arguments: str,
    description: str,
    *,
    user: str | None = None,
    settings: TaskSettings | None = None,
) -> str:
    """The unit-file text for a oneshot ``.service`` (the exec action), run as ``user`` if given.

    ``program`` is one quoted word (:func:`_exec_program`); ``arguments`` follow it as written, so
    systemd splits and unquotes them with its own rules and expands ``$VAR``, as documented.
    ``settings`` add their keys after the existing lines of their section (:func:`check_settings`
    with no triggers refuses what has no key, so no value is dropped silently); a None field, or
    None for all, adds nothing.

    Raises:
        ConfigurationError: ``program``, ``arguments`` or ``description`` holds a line break or
            another control character, or ``program`` is empty or starts with an ExecStart= prefix.
        PlatformUnsupportedError: a setting has no systemd key (:func:`check_settings`).

    Example:
        >>> "User=1000" in service_unit("/bin/true", "", "d", user="1000")
        True
        >>> 'ExecStart="/usr/bin/date" +%%Y' in service_unit("/usr/bin/date", "+%Y", "")
        True
    """
    check_settings(settings, ())
    program = _exec_program(program)
    arguments = _unit_value(arguments, "arguments")
    exec_line = program if not arguments else f"{program} {arguments}"
    user_line = f"User={user}\n" if user is not None else ""
    unit_keys, service_keys = _settings_keys(settings)
    return (
        f"{_unit_head(description, unit_keys)}[Service]\nType=oneshot\n{user_line}ExecStart={exec_line}\n{service_keys}"
    )


def _unit_head(description: str, extra: str = "") -> str:
    """The [Unit] section every unit pwshpy writes: its description, the ownership key, then ``extra`` lines."""
    described = _unit_value(description, "description") or "pwshpy task"
    return f"[Unit]\nDescription={described}\n{OWNERSHIP_KEY}={__init__conf__.version}\n{extra}\n"


def is_pwshpy_unit(text: str) -> bool:
    """Whether unit-file ``text`` carries :data:`OWNERSHIP_KEY` in its [Unit] section.

    Example:
        >>> is_pwshpy_unit("[Unit]\\nX-PwshPy=1.6.0\\n\\n[Service]\\nExecStart=/bin/true\\n")
        True
        >>> is_pwshpy_unit("[Service]\\nX-PwshPy=1.6.0\\n")
        False
    """
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "Unit" and line.split("=", 1)[0].strip() == OWNERSHIP_KEY and "=" in line:
            return True
    return False


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
        ConfigurationError: ``base`` or ``description`` holds a line break or another control character.

    Example:
        >>> "OnBootSec=90s" in timer_unit("job", "", [BootTrigger(delay=timedelta(seconds=90))])
        True
    """
    lines = "".join(f"{_timer_line(t)}\n" for t in triggers)
    unit = _single_line(base, "task_path")
    return (
        f"{_unit_head(description)}"
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
    return starts_when_armed(TimerBase.ON_BOOT for t in triggers if isinstance(t, BootTrigger))


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


def _monotonic_bases(bus: _dbus.Bus, timer: str) -> list[TimerBase] | None:
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
    return [TimerBase(str(entry[0])) for entry in entries]


def _owned_unit_file(unit: str) -> bool:
    """Whether ``/etc/systemd/system/<unit>`` is a file pwshpy wrote; False when there is none.

    Raises:
        NativeCallError: A file (or link) is there that pwshpy did not write (no :data:`OWNERSHIP_KEY`
            in its [Unit] section), or it cannot be read: ownership that cannot be read is no
            ownership, so the caller leaves it alone.
    """
    path = _UNIT_DIR / unit
    if not path.exists() and not path.is_symlink():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise native_error(f"cannot read {path} to tell whether pwshpy wrote it: {exc}", exc) from exc
    if not is_pwshpy_unit(text):
        raise NativeCallError(
            f"{path} was not written by pwshpy (no {OWNERSHIP_KEY}= in its [Unit] section); it is left as it is."
        )
    return True


def _require_free_or_owned(bus: _dbus.Bus, unit: str) -> None:
    """Refuse to write ``unit`` unless pwshpy wrote the file already or systemd knows no such unit.

    A unit without a file under /etc (a vendor unit under /lib, a runtime or masked one) would be
    shadowed by the file register writes, and a vendor ``.timer`` of the same name would trigger the
    new service. :func:`systemd_dbus.load_unit` raises when systemd gives no answer: refused then too.
    """
    if _owned_unit_file(unit):
        return
    if _dbus.load_unit(bus, unit) is not None:
        raise NativeCallError(f"systemd already has a unit {unit!r}, not written by pwshpy; it is left as it is.")


def _has_timer(bus: _dbus.Bus, base: str) -> bool:
    """Whether ``base`` names a scheduled (``.timer``) task rather than a plain on-demand service."""
    return _dbus.unit_file_state(bus, f"{base}.timer") is not None or (_UNIT_DIR / f"{base}.timer").exists()


class _ScheduleVerb(str, Enum):
    """The verb that toggles a task's schedule; its value is the word refusals name."""

    ENABLE = "enable"
    DISABLE = "disable"


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
        settings: TaskSettings | None = None,
    ) -> ScheduledTaskInfo:
        """Create a oneshot ``.service`` and, with triggers, an enabled ``.timer``; return the task.

        Registering over an existing task replaces it (its old ``.timer`` is stopped first and
        removed when ``triggers`` is empty), like Task Scheduler's create-or-update, but only a task
        pwshpy registered: each unit file it writes carries :data:`OWNERSHIP_KEY`, and a unit of the
        same name that pwshpy did not write (a file in ``/etc/systemd/system`` without the key, or one
        systemd has from anywhere else) is refused before anything is written.

        Args:
            task_path: The task identifier; reduced to the unit base name (:func:`base_name`).
            program: Executable the service's ``ExecStart=`` runs.
            arguments: Command-line arguments for ``program``.
            description: The units' ``Description=``.
            principal: Who the task runs as; None or SYSTEM is root, a decimal uid is ``User=``.
            triggers: When the task fires; empty means on demand only.
            settings: Battery, time-limit, multiple-instances, hard-terminate and demand-start
                switches, written as keys of the ``.service``; only a set field that differs from
                systemd's own behaviour adds a key, so None (or ``TaskSettings()``) writes the unit
                as before.

        Returns:
            The registered task, read back, its effective settings included.

        Raises:
            PlatformUnsupportedError: the principal, a trigger or a setting has no systemd analog
                (:func:`linux_user`, :func:`timer_unit`, :func:`arms_on_register`,
                :func:`check_settings`).
            ConfigurationError: ``task_path`` is no valid systemd unit name (:func:`unit_base`), or
                ``program``, ``arguments`` or ``description`` holds a line break.
            NativeCallError: a unit of the same name exists that pwshpy did not write, a unit file
                could not be read or written, or a D-Bus call failed.

        Example:
            >>> SystemdScheduledTaskController().register(
            ...     "job", program="/bin/true", principal=TaskPrincipal(user_id="S-1-5-21-1-2-3-1001")
            ... )
            Traceback (most recent call last):
            ...
            pwshpy.domain.errors.PlatformUnsupportedError: on Linux a principal is a decimal uid ...
        """
        base = unit_base(task_path)
        # Validate and render everything first: a refusal must not leave a half-written task or cost a bus.
        check_settings(settings, triggers)  # the daily-trigger rule needs the triggers service_unit lacks
        service_text = service_unit(
            program, arguments, description, user=linux_user(principal or TaskPrincipal()), settings=settings
        )
        timer_text = timer_unit(base, description, triggers) if triggers else None
        arm_now = arms_on_register(triggers)
        timer = f"{base}.timer"
        timer_file = _UNIT_DIR / timer
        bus = _dbus.open_bus()
        try:
            for unit in (f"{base}.service", timer):  # before anything is stopped or written
                _require_free_or_owned(bus, unit)
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
            raise native_error(f"cannot write unit {path}: {exc}", exc) from exc

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise native_error(f"cannot delete unit {path}: {exc}", exc) from exc

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
        """Stop and delete a task's units, only those pwshpy wrote (under /etc, with :data:`OWNERSHIP_KEY`).

        Nothing is stopped, disabled or deleted unless a unit file pwshpy wrote is there: a vendor
        unit of the same name stays running and enabled.

        Raises:
            ConfigurationError: ``task_path`` is no valid systemd unit name (:func:`unit_base`).
            NativeCallError: no such task (pwshpy wrote neither unit), a unit file of that name that
                pwshpy did not write or cannot read is there, or a D-Bus call failed.
        """
        base = unit_base(task_path)
        service, timer = f"{base}.service", f"{base}.timer"
        owned = {unit: _owned_unit_file(unit) for unit in (timer, service)}  # refuses a foreign file first
        if not any(owned.values()):
            raise NativeCallError(f"no such task {base!r}: pwshpy wrote no {service} or {timer} under {_UNIT_DIR}.")
        bus = _dbus.open_bus()
        try:
            if owned[timer]:
                self._retire_timer(bus, timer, remove=True)
            if owned[service]:
                with contextlib.suppress(NativeCallError):  # not loaded / already gone -> nothing to stop
                    _dbus.call(bus, "StopUnit", "ss", (service, "replace"))
                self._remove(_UNIT_DIR / service)
            _dbus.call(bus, "Reload")
        finally:
            bus.conn.close()

    def run(self, task_path: str) -> None:
        """Run the task now: start its ``.service`` (like Start-ScheduledTask, on demand).

        Raises:
            ConfigurationError: ``task_path`` is no systemd unit name, or a bare template (:func:`existing_unit_base`).
            NativeCallError: the D-Bus call failed, systemd's reason in the message - for a task
                registered with ``allow_demand_start=False`` it is "Operation refused, unit ...
                may be requested by dependency only".
        """
        self._start_stop(existing_unit_base(task_path), "StartUnit")

    def stop(self, task_path: str) -> None:
        """Stop the task's running ``.service`` (like Stop-ScheduledTask).

        Raises:
            ConfigurationError: ``task_path`` is no systemd unit name, or a bare template (:func:`existing_unit_base`).
            NativeCallError: the D-Bus call failed.
        """
        self._start_stop(existing_unit_base(task_path), "StopUnit")

    def enable(self, task_path: str) -> ScheduledTaskInfo:
        """Arm a task's ``.timer`` so it runs on schedule (like Enable-ScheduledTask).

        A timer with a boot-relative part (``OnBootSec=`` / ``OnStartupSec=``) is enabled but not
        started (:func:`starts_when_armed`): started now, its elapsed boot offset would run the
        job at once. It arms at the next boot. A timer whose monotonic timers cannot be read is
        treated the same way - never running the job unexpectedly is the safe side.

        Raises:
            ConfigurationError: ``task_path`` is no systemd unit name, or a bare template (:func:`existing_unit_base`).
            NativeCallError: the task is on-demand (no ``.timer``) or its timer is static (no
                ``[Install]`` section), or a D-Bus call failed.
        """
        base = existing_unit_base(task_path)
        timer = f"{base}.timer"
        bus = _dbus.open_bus()
        try:
            self._require_timer(bus, base, _ScheduleVerb.ENABLE)
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
            ConfigurationError: ``task_path`` is no systemd unit name, or a bare template (:func:`existing_unit_base`).
            NativeCallError: the task is on-demand (no ``.timer``) or its timer is static (no
                ``[Install]`` section) - refused before anything is stopped - or a D-Bus call failed.
        """
        base = existing_unit_base(task_path)
        bus = _dbus.open_bus()
        try:
            self._require_timer(bus, base, _ScheduleVerb.DISABLE)
            _dbus.call(bus, "StopUnit", "ss", (f"{base}.timer", "replace"))
            _dbus.call(bus, "DisableUnitFiles", "asb", ([f"{base}.timer"], False))
            _dbus.call(bus, "Reload")
            return self._info(bus, base)
        finally:
            bus.conn.close()

    @staticmethod
    def _require_timer(bus: _dbus.Bus, base: str, verb: _ScheduleVerb) -> None:
        """Refuse enable/disable for a task whose schedule systemd cannot toggle.

        A task with no ``.timer`` is on-demand: there is nothing to toggle. A static timer has no
        ``[Install]`` section, so ``EnableUnitFiles``/``DisableUnitFiles`` succeed and change nothing:
        the task would read back enabled as if the call had worked (and disable would have stopped
        the timer on the way, until whatever pulls it in starts it again).
        """
        if not _has_timer(bus, base):
            raise NativeCallError(
                f"task {base!r} is on-demand (no .timer); nothing to {verb.value} - run it directly, or unregister it."
            )
        if _dbus.unit_file_state(bus, f"{base}.timer") is UnitFileState.STATIC:
            # What does work differs by verb: a static timer runs whenever the unit that wants it starts,
            # so only that unit (or an [Install] section) arms it, and only a mask keeps it from running.
            remedy = (
                "it runs whenever the unit that wants it starts; give it an [Install] section to enable it on its own"
                if verb is _ScheduleVerb.ENABLE
                else "change the unit that wants it, or mask the timer to keep it from running"
            )
            raise NativeCallError(
                f"task {base!r} is a static timer (no [Install] section; another unit pulls it in), so systemd "
                f"has nothing to {verb.value} - {remedy}."
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
            state = _dbus.unit_file_state(bus, unit) or UnitFileState.EMPTY
            return _timer_task(bus, unit, _dbus.load_unit_path(bus, unit), file_state=state)
        return _service_task(bus, base, _dbus.load_unit_path(bus, f"{base}.service"))


__all__ = [
    "SystemdScheduledTaskController",
    "arms_on_register",
    "OWNERSHIP_KEY",
    "base_name",
    "battery_start_allowed",
    "check_settings",
    "existing_unit_base",
    "is_pwshpy_unit",
    "iter_timers",
    "latest_usec",
    "linux_user",
    "service_last_result",
    "service_task_state",
    "service_unit",
    "service_user_id",
    "start_time_limit",
    "starts_when_armed",
    "TimerBase",
    "timer_enabled",
    "timer_task_state",
    "timer_to_task_state",
    "timer_unit",
    "unit_base",
    "usec_to_datetime",
]
