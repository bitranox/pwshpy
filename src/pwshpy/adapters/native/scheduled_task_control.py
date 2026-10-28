"""native scheduled-task CONTROL over the Task Scheduler 2.0 COM API (Windows-only, **mutating**).

Register / Unregister a task, Enable / Disable it, and Run / Stop it - the mutating
counterpart of the read-only :mod:`~pwshpy.adapters.native.scheduled_tasks` source.
Tasks are addressed by their full path (``\\Folder\\Name``; ``\\Name`` at the root).
``register`` creates an EXEC task (a program + arguments) with an optional principal,
triggers and settings.  ``win32com`` is imported lazily; every COM call is wrapped in
:class:`~pwshpy.domain.errors.NativeCallError`.

**MUTATING** - see CLAUDE.md "Development Safety": real tests use a scratch task
they register and unregister on the disposable throwaway VM.

Contents:
    * :class:`NativeScheduledTaskController` - register/unregister/enable/disable/run/stop.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, NoReturn

from ...domain.enums import TaskLogonType, TaskRunLevel
from ...domain.errors import ConfigurationError, PlatformUnsupportedError
from ...domain.records import ScheduledTaskInfo
from ...domain.scheduling import (
    BootTrigger,
    DailyTrigger,
    LogonTrigger,
    OnceTrigger,
    TaskPrincipal,
    TaskSettings,
    TaskTrigger,
)
from .marshal import from_task_multiple_instances, native_error, to_task_state
from .task_readback import read_task_extras

_TASK_ACTION_EXEC = 0
_TASK_CREATE_OR_UPDATE = 6
_ROOT = "\\"
# TASK_LOGON_TYPE / TASK_RUNLEVEL_TYPE / TASK_TRIGGER_TYPE2 from taskschd.h.
_LOGON_TYPE = {
    TaskLogonType.S4U: 2,
    TaskLogonType.INTERACTIVE_TOKEN: 3,
    TaskLogonType.SERVICE_ACCOUNT: 5,
}
_RUN_LEVEL = {TaskRunLevel.LIMITED: 0, TaskRunLevel.HIGHEST: 1}
_TRIGGER_TIME, _TRIGGER_DAILY, _TRIGGER_BOOT, _TRIGGER_LOGON = 1, 2, 8, 9
# Well-known GROUP SIDs: Everyone, Authenticated Users and every BUILTIN alias (S-1-5-32-*).
# No TASK_LOGON_TYPE fits a group, so RegisterTaskDefinition fails on one with an opaque HRESULT.
_GROUP_SID = re.compile(r"S-1-1-0|S-1-5-11|S-1-5-32-[0-9]+")
_UID_ON_WINDOWS = "a numeric uid principal is POSIX-only; on Windows pass a SID."


def _connect_scheduler() -> Any:
    """Connect to the Task Scheduler service via COM (lazy pywin32 import)."""
    try:
        win32com_client: Any = importlib.import_module("win32com.client")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "Scheduled-task control requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc
    try:
        service = win32com_client.Dispatch("Schedule.Service")
        service.Connect()
    except Exception as exc:
        raise native_error(str(exc) or "cannot connect to the Task Scheduler service", exc) from exc
    return service


def _split_path(task_path: str) -> tuple[str, str]:
    """Split ``\\Folder\\Name`` into ``(folder_path, name)`` (root folder for a bare name)."""
    folder_path, _, name = task_path.rstrip("\\").rpartition("\\")
    return (folder_path or _ROOT), name


def _task_info(task: Any, folder_path: str) -> ScheduledTaskInfo:
    """Marshal an IRegisteredTask into a :class:`ScheduledTaskInfo` snapshot."""
    registration = task.Definition.RegistrationInfo
    extras = read_task_extras(task)  # fail-soft per field: never costs the task
    return ScheduledTaskInfo.model_construct(
        task_name=str(task.Name),
        task_path=folder_path if folder_path.endswith("\\") else folder_path + "\\",
        state=to_task_state(int(task.State)),
        enabled=bool(task.Enabled),
        author=str(registration.Author or ""),
        description=str(registration.Description or ""),
        user_id=extras.user_id,
        run_level=extras.run_level,
        last_run_time=extras.last_run_time,
        last_task_result=extras.last_task_result,
        next_run_time=extras.next_run_time,
        settings=extras.settings,
    )


def _iso_duration(delay: timedelta) -> str:
    """Render ``delay`` as the ISO 8601 duration Task Scheduler wants; it honours whole seconds only.

    Example:
        >>> _iso_duration(timedelta(minutes=1, seconds=30, microseconds=5))
        'PT90S'
    """
    return f"PT{int(delay.total_seconds())}S"


def _is_group_sid(user_id: str | None) -> bool:
    """True when ``user_id`` is a well-known group SID (Everyone, Authenticated Users, BUILTIN).

    A domain-relative group (``S-1-5-21-...-513``, Domain Users) has the same shape as a user
    SID; telling them apart needs an LSA lookup, so such a SID is passed through.

    Example:
        >>> _is_group_sid("S-1-5-32-544"), _is_group_sid("S-1-5-21-1-2-3-513"), _is_group_sid(None)
        (True, False, False)
    """
    return user_id is not None and _GROUP_SID.fullmatch(user_id) is not None


def _refuse_group(user_id: str | None, role: str) -> None:
    """Raise :class:`ConfigurationError` when ``user_id`` is a well-known group SID."""
    if _is_group_sid(user_id):
        raise ConfigurationError(
            f"{role} {user_id} is a group SID; pwshpy registers a single account - pass a user or service account SID."
        )


def _check_principal(principal: TaskPrincipal) -> None:
    """Refuse a principal Task Scheduler cannot register, before any COM call.

    The domain deliberately does not cross-check ``logon_type`` against ``user_id`` (the
    POSIX backend reads neither the same way), so the Windows rules live here: no logon
    type fits a well-known group SID, a service SID logs on only as a service account, and
    only a service SID can.  Refusing up front gives a clear message instead of an opaque
    HRESULT from ``RegisterTaskDefinition``.  A domain-relative group SID
    (``S-1-5-21-...-513``) cannot be told from a user without an LSA lookup and is passed on.

    Raises:
        PlatformUnsupportedError: ``user_id`` is a numeric POSIX uid.
        ConfigurationError: ``user_id`` is a well-known group SID, or the explicit
            ``logon_type`` cannot fit ``user_id``.

    Example:
        >>> _check_principal(TaskPrincipal.system())
        >>> _check_principal(TaskPrincipal(user_id="S-1-5-18", logon_type=TaskLogonType.S4U))
        Traceback (most recent call last):
        ...
        pwshpy.domain.errors.ConfigurationError: logon type S4U cannot run as S-1-5-18: ...
    """
    if principal.is_uid:
        raise PlatformUnsupportedError(_UID_ON_WINDOWS)
    _refuse_group(principal.user_id, "principal")
    logon = principal.effective_logon_type()
    is_service = principal.is_service_account
    if is_service and logon is not TaskLogonType.SERVICE_ACCOUNT:
        raise ConfigurationError(
            f"logon type {logon.value} cannot run as {principal.user_id}: a service account SID "
            f"logs on only as {TaskLogonType.SERVICE_ACCOUNT.value}."
        )
    if logon is TaskLogonType.SERVICE_ACCOUNT and not is_service:
        who = principal.user_id or "the interactive caller"
        raise ConfigurationError(
            f"logon type {logon.value} cannot run as {who}: it needs SYSTEM (S-1-5-18), "
            "LOCAL SERVICE (S-1-5-19) or NETWORK SERVICE (S-1-5-20)."
        )


def _check_triggers(triggers: Sequence[TaskTrigger]) -> None:
    """Refuse a logon trigger naming a POSIX uid or a well-known group SID, before any COM call.

    Raises:
        PlatformUnsupportedError: a :class:`LogonTrigger` names a numeric uid.
        ConfigurationError: a :class:`LogonTrigger` names a well-known group SID.

    Example:
        >>> _check_triggers([LogonTrigger(user_id="S-1-5-21-1-2-3-1001"), BootTrigger()])
    """
    for trigger in triggers:
        if not isinstance(trigger, LogonTrigger):
            continue
        if trigger.is_uid:
            raise PlatformUnsupportedError(_UID_ON_WINDOWS)
        _refuse_group(trigger.user_id, "logon trigger user")


def _unknown_trigger(trigger: NoReturn) -> NoReturn:
    """Fail loudly on a value outside the TaskTrigger union (pyright proves it unreachable)."""
    raise TypeError(f"not a TaskTrigger: {type(trigger).__name__}")


def _add_trigger(definition: Any, trigger: TaskTrigger) -> None:
    """Create one COM trigger on ``definition``.

    ``StartBoundary`` keeps the caller's UTC offset (``isoformat``), which Task Scheduler
    honours, so a trigger built in one zone fires at the same instant on a machine in another.

    Raises:
        TypeError: ``trigger`` is not one of the :data:`TaskTrigger` kinds.

    Example:
        >>> callable(_add_trigger)
        True
    """
    # match, not an isinstance chain: a last isinstance(LogonTrigger) would be "unnecessary"
    # under pyright strict, and a bare else would silently treat any stray value as a logon.
    match trigger:
        case OnceTrigger():
            com = definition.Triggers.Create(_TRIGGER_TIME)
            com.StartBoundary = trigger.at.isoformat(timespec="seconds")
        case DailyTrigger():
            com = definition.Triggers.Create(_TRIGGER_DAILY)
            com.StartBoundary = trigger.at.isoformat(timespec="seconds")
            com.DaysInterval = trigger.every_days
        case BootTrigger():
            com = definition.Triggers.Create(_TRIGGER_BOOT)
            # Whole seconds only: a sub-second delay would render "PT0S", so it keeps the default.
            if int(trigger.delay.total_seconds()):
                com.Delay = _iso_duration(trigger.delay)
        case LogonTrigger():
            com = definition.Triggers.Create(_TRIGGER_LOGON)
            if trigger.user_id is not None:  # None keeps the COM default: any user logging on
                com.UserId = trigger.user_id
        case _:
            _unknown_trigger(trigger)


def _apply_principal(definition: Any, principal: TaskPrincipal) -> int:
    """Set the definition's principal; return the logon type ``RegisterTaskDefinition`` needs.

    Example:
        >>> callable(_apply_principal)
        True
    """
    logon = _LOGON_TYPE[principal.effective_logon_type()]
    if principal.user_id is not None:
        definition.Principal.UserId = principal.user_id
    definition.Principal.LogonType = logon
    definition.Principal.RunLevel = _RUN_LEVEL[principal.run_level]
    return logon


def _apply_settings(definition: Any, settings: TaskSettings) -> None:
    """Write each field of ``settings`` that is set onto the definition's ITaskSettings.

    A None field writes nothing, so the OS default stays (``TaskSettings()`` changes nothing).
    Every value the domain accepts has a Task Scheduler analog, so nothing is refused here.
    ``allow_demand_start`` is not written here: ``register`` always writes AllowDemandStart
    itself (:func:`_allow_demand_start`).

    Example:
        >>> callable(_apply_settings)
        True
    """
    com = definition.Settings
    if settings.allow_start_if_on_batteries is not None:
        com.DisallowStartIfOnBatteries = not settings.allow_start_if_on_batteries
    if settings.stop_if_going_on_batteries is not None:
        com.StopIfGoingOnBatteries = settings.stop_if_going_on_batteries
    if settings.execution_time_limit is not None:
        # PT{n}S; Windows stores it normalised (measured): PT259200S reads back PT72H, never P3D,
        # and as that is the default the task XML holds no ExecutionTimeLimit; PT0S = no limit.
        com.ExecutionTimeLimit = _iso_duration(settings.execution_time_limit)
    if settings.multiple_instances is not None:
        com.MultipleInstances = from_task_multiple_instances(settings.multiple_instances)
    if settings.allow_hard_terminate is not None:
        com.AllowHardTerminate = settings.allow_hard_terminate


def _allow_demand_start(settings: TaskSettings | None) -> bool:
    """The AllowDemandStart ``register`` writes: True unless ``settings`` say False.

    ``register`` always writes this one property, so an unset field (or no settings) writes True,
    the Task Scheduler default, and a set field is that single write rather than a second one.

    Example:
        >>> _allow_demand_start(None), _allow_demand_start(TaskSettings(allow_demand_start=False))
        (True, False)
    """
    if settings is None or settings.allow_demand_start is None:
        return True
    return settings.allow_demand_start


class NativeScheduledTaskController:
    """Mutating scheduled-task control over the Task Scheduler COM API."""

    def _fetch(self, service: Any, task_path: str) -> tuple[str, Any]:
        folder_path, name = _split_path(task_path)
        folder = service.GetFolder(folder_path)
        return folder_path, folder.GetTask(name)

    def enable(self, task_path: str) -> ScheduledTaskInfo:
        """Enable a scheduled task; return its new state."""
        service = _connect_scheduler()
        try:
            folder_path, task = self._fetch(service, task_path)
            task.Enabled = True
            return _task_info(task, folder_path)
        except Exception as exc:
            raise native_error(str(exc) or f"cannot enable task {task_path!r}", exc) from exc

    def disable(self, task_path: str) -> ScheduledTaskInfo:
        """Disable a scheduled task; return its new state."""
        service = _connect_scheduler()
        try:
            folder_path, task = self._fetch(service, task_path)
            task.Enabled = False
            return _task_info(task, folder_path)
        except Exception as exc:
            raise native_error(str(exc) or f"cannot disable task {task_path!r}", exc) from exc

    def run(self, task_path: str) -> None:
        """Start a scheduled task now (on demand)."""
        service = _connect_scheduler()
        try:
            _folder_path, task = self._fetch(service, task_path)
            task.Run(None)
        except Exception as exc:
            raise native_error(str(exc) or f"cannot run task {task_path!r}", exc) from exc

    def stop(self, task_path: str) -> None:
        """Stop a running scheduled task."""
        service = _connect_scheduler()
        try:
            _folder_path, task = self._fetch(service, task_path)
            task.Stop(0)
        except Exception as exc:
            raise native_error(str(exc) or f"cannot stop task {task_path!r}", exc) from exc

    def unregister(self, task_path: str) -> None:
        """Delete (unregister) a scheduled task."""
        service = _connect_scheduler()
        try:
            folder_path, name = _split_path(task_path)
            service.GetFolder(folder_path).DeleteTask(name, 0)
        except Exception as exc:
            raise native_error(str(exc) or f"cannot unregister task {task_path!r}", exc) from exc

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
        """Register an EXEC task (``program`` + ``arguments``) with an optional principal, triggers and settings.

        With no ``principal`` the task runs as the interactive caller; with no ``triggers``
        it runs only on demand (``start_scheduled_task``).

        Args:
            task_path: Full task path (``\\Folder\\Name``; ``\\Name`` at the root).
            program: Executable the EXEC action starts.
            arguments: Command-line arguments for ``program``.
            description: Registration description.
            principal: Who the task runs as, and with which token and run level.
            triggers: When the task fires; empty means on demand only.
            settings: Battery, time-limit, multiple-instances, hard-terminate and demand-start
                switches; only the fields that are set are written, so None (or ``TaskSettings()``)
                keeps every Task Scheduler default. AllowDemandStart is written in any case, True
                unless ``allow_demand_start`` is False.

        Returns:
            The registered task, read back, its effective settings included.

        Raises:
            PlatformUnsupportedError: a principal or logon trigger names a POSIX uid, or
                pywin32 is absent.
            ConfigurationError: the principal or a logon trigger names a well-known group
                SID, or the principal's logon type cannot fit its user.
            NativeCallError: a Task Scheduler COM call failed.

        Example:
            >>> NativeScheduledTaskController().register(
            ...     "\\\\T", program="cmd.exe", principal=TaskPrincipal(user_id="1000")
            ... )
            Traceback (most recent call last):
            ...
            pwshpy.domain.errors.PlatformUnsupportedError: a numeric uid principal is POSIX-only; ...
        """
        principal = principal or TaskPrincipal()
        # Validate first: a refusal must not depend on (or cost) a Task Scheduler connection.
        _check_principal(principal)
        _check_triggers(triggers)
        service = _connect_scheduler()
        try:
            folder_path, name = _split_path(task_path)
            folder = service.GetFolder(folder_path)
            definition = service.NewTask(0)
            definition.RegistrationInfo.Description = description
            definition.Settings.Enabled = True
            definition.Settings.AllowDemandStart = _allow_demand_start(settings)
            action = definition.Actions.Create(_TASK_ACTION_EXEC)
            action.Path = program
            action.Arguments = arguments
            logon = _apply_principal(definition, principal)
            for trigger in triggers:
                _add_trigger(definition, trigger)
            if settings is not None:
                _apply_settings(definition, settings)
            task = folder.RegisterTaskDefinition(
                name, definition, _TASK_CREATE_OR_UPDATE, principal.user_id, None, logon
            )
            return _task_info(task, folder_path)
        except Exception as exc:
            raise native_error(str(exc) or f"cannot register task {task_path!r}", exc) from exc


__all__ = ["NativeScheduledTaskController"]
