"""``pwshpy`` mutating commands - the CLI surface for the mutating native verbs.

Top-level verb-noun commands mirroring PowerShell (``start-service``,
``set-registry-value``, ``new-local-user``, ``add-acl-ace``,
``register-scheduled-task`` ...).  Each is a thin front end over the ``ps`` facade
and echoes a one-line status.  **These change host state** - see CLAUDE.md
"Development Safety".

Most are portable and dispatch by OS: the service verbs, the scheduled-task verbs, the
local-account verbs and the ACL verbs all run on win32 on Windows and on the honest Linux
backend (systemd, systemd timers, shadow-utils, POSIX ACL xattr + chown) on Linux. Only the
registry verbs and ``clear_event_log`` are Windows-only and raise
:class:`~pwshpy.domain.errors.PlatformUnsupportedError` off Windows.

Contents:
    * the ``cli_*`` mutating commands, grouped by subsystem below.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import rich_click as click
from pydantic import ValidationError

from ....domain.enums import (
    AceType,
    RegistryValueType,
    ServiceStartType,
    TaskLogonType,
    TaskMultipleInstances,
    TaskRunLevel,
    WellKnownSid,
)
from ....domain.scheduling import (
    BootTrigger,
    DailyTrigger,
    LogonTrigger,
    OnceTrigger,
    TaskPrincipal,
    TaskSettings,
    TaskTrigger,
)
from ..context import get_cli_context
from ..typed_click import argument, option
from ._common import CaseInsensitiveChoice, enum_by_value

if TYPE_CHECKING:
    from pwshpy.composition import Ps


def _ps(ctx: click.Context) -> Ps:
    return get_cli_context(ctx).services.ps


# --- services ----------------------------------------------------------------


@click.command("start_service")
@argument("name")
@click.pass_context
def cli_start_service(ctx: click.Context, name: str) -> None:
    """Start a service - win32service on Windows, systemd on Linux (mutating).

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_start_service, ["--help"]).exit_code
        0
    """
    result = _ps(ctx).start_service(name)
    click.echo(f"{name}: {result.status.value}")


@click.command("stop_service")
@argument("name")
@option(
    "--wait/--no-wait",
    default=True,
    help="Wait until the service is Stopped (default; exit 110 on timeout), or issue the stop and return.",
)
@click.pass_context
def cli_stop_service(ctx: click.Context, name: str, wait: bool) -> None:
    """Stop a service - win32service on Windows, systemd on Linux (mutating).

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> "--no-wait" in CliRunner().invoke(cli_stop_service, ["--help"]).output
        True
    """
    result = _ps(ctx).stop_service(name, wait=wait)
    click.echo(f"{name}: {result.status.value}")


@click.command("restart_service")
@argument("name")
@click.pass_context
def cli_restart_service(ctx: click.Context, name: str) -> None:
    """Restart a service - win32service on Windows, systemd on Linux (mutating)."""
    result = _ps(ctx).restart_service(name)
    click.echo(f"{name}: {result.status.value}")


@click.command("set_service")
@argument("name")
@argument("startup_type", type=click.Choice([e.value for e in ServiceStartType]))
@click.pass_context
def cli_set_service_startup(ctx: click.Context, name: str, startup_type: str) -> None:
    """Set a service's startup type (Automatic/Manual/Disabled/...) (mutating)."""
    result = _ps(ctx).set_service(name, ServiceStartType(startup_type))
    start = result.start_type.value if result.start_type else "?"
    click.echo(f"{name}: startup={start}")


# --- registry ----------------------------------------------------------------


def _coerce_registry_data(data: str, value_type: RegistryValueType) -> str | int | list[str]:
    """Coerce a CLI string into the Python type for ``value_type``."""
    if value_type in (RegistryValueType.REG_DWORD, RegistryValueType.REG_QWORD):
        return int(data)
    if value_type is RegistryValueType.REG_MULTI_SZ:
        return data.split(";")
    return data


@click.command("set_item_property")
@argument("key")
@argument("name")
@argument("data")
@option(
    "--type",
    "value_type",
    type=click.Choice([e.value for e in RegistryValueType]),
    default=RegistryValueType.REG_SZ.value,
    help=f"Registry value type (default {RegistryValueType.REG_SZ.value}).",
)
@click.pass_context
def cli_set_registry_value(ctx: click.Context, key: str, name: str, data: str, value_type: str) -> None:
    """Create or overwrite a registry value (mutating)."""
    vtype = RegistryValueType(value_type)
    _ps(ctx).set_item_property(key, name, _coerce_registry_data(data, vtype), vtype)
    click.echo(f"set {key}\\{name} ({vtype.value})")


@click.command("remove_item_property")
@argument("key")
@argument("name")
@click.pass_context
def cli_remove_registry_value(ctx: click.Context, key: str, name: str) -> None:
    """Delete a registry value (mutating)."""
    _ps(ctx).remove_item_property(key, name)
    click.echo(f"removed {key}\\{name}")


@click.command("new_registry_key")
@argument("key")
@click.pass_context
def cli_new_registry_key(ctx: click.Context, key: str) -> None:
    """Create a registry key (mutating)."""
    _ps(ctx).new_registry_key(key)
    click.echo(f"created {key}")


@click.command("remove_registry_key")
@argument("key")
@option("--recursive", "-r", is_flag=True, default=False, help="Also remove subkeys.")
@click.pass_context
def cli_remove_registry_key(ctx: click.Context, key: str, recursive: bool) -> None:
    """Delete a registry key (mutating)."""
    _ps(ctx).remove_registry_key(key, recursive=recursive)
    click.echo(f"removed {key}")


# --- event log ---------------------------------------------------------------


@click.command("clear_event_log")
@argument("log_name")
@option("--backup", "backup_path", default=None, help="Export the log to this path before clearing.")
@click.pass_context
def cli_clear_event_log(ctx: click.Context, log_name: str, backup_path: str | None) -> None:
    """Clear a Windows event log (mutating)."""
    _ps(ctx).clear_event_log(log_name, backup_path=backup_path)
    click.echo(f"cleared event log: {log_name}")


# --- local accounts ----------------------------------------------------------


@click.command("new_local_user")
@argument("name")
@option("--password", default="", help="Initial password.")
@option("--full-name", default="", help="Full name.")
@option("--description", default="", help="Description.")
@option("--disabled", is_flag=True, default=False, help="Create the account disabled.")
@click.pass_context
def cli_new_local_user(
    ctx: click.Context, name: str, password: str, full_name: str, description: str, disabled: bool
) -> None:
    """Create a local user (mutating)."""
    result = _ps(ctx).new_local_user(
        name, password=password, full_name=full_name, description=description, disabled=disabled
    )
    click.echo(f"created user {name} ({result.sid})")


@click.command("remove_local_user")
@argument("name")
@click.pass_context
def cli_remove_local_user(ctx: click.Context, name: str) -> None:
    """Delete a local user (mutating)."""
    _ps(ctx).remove_local_user(name)
    click.echo(f"removed user {name}")


@click.command("enable_local_user")
@argument("name")
@click.pass_context
def cli_enable_local_user(ctx: click.Context, name: str) -> None:
    """Enable a local user (mutating)."""
    _ps(ctx).enable_local_user(name)
    click.echo(f"enabled user {name}")


@click.command("disable_local_user")
@argument("name")
@click.pass_context
def cli_disable_local_user(ctx: click.Context, name: str) -> None:
    """Disable a local user (mutating)."""
    _ps(ctx).disable_local_user(name)
    click.echo(f"disabled user {name}")


@click.command("new_local_group")
@argument("name")
@option("--description", default="", help="Description.")
@click.pass_context
def cli_new_local_group(ctx: click.Context, name: str, description: str) -> None:
    """Create a local group (mutating)."""
    result = _ps(ctx).new_local_group(name, description=description)
    click.echo(f"created group {name} ({result.sid})")


@click.command("remove_local_group")
@argument("name")
@click.pass_context
def cli_remove_local_group(ctx: click.Context, name: str) -> None:
    """Delete a local group (mutating)."""
    _ps(ctx).remove_local_group(name)
    click.echo(f"removed group {name}")


@click.command("add_local_group_member")
@argument("group")
@argument("member")
@click.pass_context
def cli_add_local_group_member(ctx: click.Context, group: str, member: str) -> None:
    """Add a member to a local group (mutating)."""
    _ps(ctx).add_local_group_member(group, member)
    click.echo(f"added {member} to {group}")


@click.command("remove_local_group_member")
@argument("group")
@argument("member")
@click.pass_context
def cli_remove_local_group_member(ctx: click.Context, group: str, member: str) -> None:
    """Remove a member from a local group (mutating)."""
    _ps(ctx).remove_local_group_member(group, member)
    click.echo(f"removed {member} from {group}")


# --- ACLs --------------------------------------------------------------------


@click.command("add_acl_ace")
@argument("path")
@argument("trustee")
@argument("rights", type=int)
@option("--deny", is_flag=True, default=False, help="Add a deny ACE instead of allow.")
@click.pass_context
def cli_add_acl_ace(ctx: click.Context, path: str, trustee: str, rights: int, deny: bool) -> None:
    """Add an ACE (trustee = SID or name; rights = access mask) (mutating)."""
    access = AceType.DENY if deny else AceType.ALLOW
    _ps(ctx).add_acl_ace(path, trustee, rights, access_type=access)
    click.echo(f"added {access.value} ACE for {trustee} on {path}")


@click.command("remove_acl_ace")
@argument("path")
@argument("trustee")
@click.pass_context
def cli_remove_acl_ace(ctx: click.Context, path: str, trustee: str) -> None:
    """Remove a trustee's ACEs from a path (mutating)."""
    _ps(ctx).remove_acl_ace(path, trustee)
    click.echo(f"removed ACEs for {trustee} on {path}")


@click.command("set_owner")
@argument("path")
@argument("owner")
@click.pass_context
def cli_set_owner(ctx: click.Context, path: str, owner: str) -> None:
    """Set the owner of a filesystem path (owner = SID or name) (mutating)."""
    _ps(ctx).set_owner(path, owner)
    click.echo(f"owner of {path} -> {owner}")


# --- scheduled tasks ---------------------------------------------------------


def _parse_when(value: str) -> datetime:
    """Parse an ISO 8601 time that carries a UTC offset.

    A trailing ``Z`` is rewritten to ``+00:00`` because ``datetime.fromisoformat`` only accepts
    it from Python 3.11 on.  A time WITHOUT an offset is refused rather than read as local time
    or UTC: the same text would fire at a different instant on a machine in another zone.

    Raises:
        ValueError: ``value`` is not ISO 8601, or has no UTC offset.

    Example:
        >>> _parse_when("2026-10-21T03:00:00Z").isoformat()
        '2026-10-21T03:00:00+00:00'
    """
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} has no UTC offset; add one, e.g. {value}+02:00 or {value}Z")
    return parsed


def _run_as(value: str | None) -> str | None:
    """Map a WellKnownSid member name (any case) to its SID; pass a SID or uid through unchanged.

    Example:
        >>> _run_as("local_system"), _run_as("1000"), _run_as(None)
        ('S-1-5-18', '1000', None)
    """
    if value is None:
        return None
    member = WellKnownSid.__members__.get(value.upper())
    return member.value if member is not None else value


def _render(exc: ValueError) -> str:
    """One ``<field>: <reason>`` line per validation error, or the plain ValueError text.

    ``str(ValidationError)`` would hand the caller Pydantic's internals (model names, input
    echoes, documentation URLs), so only each error's location and message are kept.

    Example:
        >>> _render(ValueError("bad time"))
        'bad time'
    """
    if not isinstance(exc, ValidationError):
        return str(exc)
    lines = [f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()]
    return "\n".join(line.removeprefix(": ") for line in lines)


def _principal(run_as: str | None, run_level: TaskRunLevel, logon_type: TaskLogonType | None) -> TaskPrincipal | None:
    """Build the principal, or None when no principal option moved off its default.

    None keeps the adapter's own default: the interactive caller on Windows, root on Linux.

    Example:
        >>> _principal(None, TaskRunLevel.LIMITED, None) is None
        True
        >>> _principal(None, TaskRunLevel.HIGHEST, None).run_level is TaskRunLevel.HIGHEST
        True
    """
    if run_as is None and logon_type is None and run_level is TaskRunLevel.LIMITED:
        return None
    return TaskPrincipal(user_id=_run_as(run_as), run_level=run_level, logon_type=logon_type)


def _principal_label(user_id: str) -> str:
    """How the register confirmation names the principal the task read back as.

    A bare ``0`` would not read as root, so a decimal uid is labelled; an empty ``user_id``
    means the backend could not report one, which is not the same as "the caller" (a Linux
    task with no principal runs as root).

    Example:
        >>> _principal_label("0"), _principal_label("S-1-5-18"), _principal_label("")
        ('uid 0', 'S-1-5-18', 'an unknown principal')
    """
    if not user_id:
        return "an unknown principal"
    if user_id.isascii() and user_id.isdigit():
        return f"uid {user_id}"
    return user_id


def _check_modifiers(
    *, daily: bool, every_days: int, at_boot: bool, boot_delay: int, at_logon: bool, logon_user: str | None
) -> None:
    """Refuse a trigger modifier given without its trigger instead of silently dropping it.

    Example:
        >>> _check_modifiers(daily=False, every_days=1, at_boot=False, boot_delay=0, at_logon=False, logon_user=None)
    """
    orphans = [
        ("--every-days", "--daily", every_days != 1 and not daily),
        ("--boot-delay", "--at-boot", boot_delay != 0 and not at_boot),
        ("--logon-user", "--at-logon", logon_user is not None and not at_logon),
    ]
    for modifier, trigger, orphaned in orphans:
        if orphaned:
            raise click.UsageError(f"{modifier} needs {trigger}")


def _triggers(
    *,
    at_times: Sequence[str],
    daily_times: Sequence[str],
    every_days: int,
    at_boot: bool,
    boot_delay: int,
    at_logon: bool,
    logon_user: str | None,
) -> list[TaskTrigger]:
    """Build the triggers in a fixed order: once, daily, boot, logon.

    Example:
        >>> [t.kind for t in _triggers(at_times=(), daily_times=(), every_days=1, at_boot=True,
        ...                             boot_delay=0, at_logon=False, logon_user=None)]
        ['boot']
    """
    triggers: list[TaskTrigger] = [OnceTrigger(at=_parse_when(value)) for value in at_times]
    triggers += [DailyTrigger(at=_parse_when(value), every_days=every_days) for value in daily_times]
    if at_boot:
        triggers.append(BootTrigger(delay=timedelta(seconds=boot_delay)))
    if at_logon:
        triggers.append(LogonTrigger(user_id=logon_user))
    return triggers


# fullmatch + [0-9]: '$' would accept a trailing newline and '\d' (or int()) non-ASCII digits. The digit
# counts cover the 2147483647 s cap (10 digits, 596523 hours) and keep int() off a long input; the cap
# itself is TaskSettings' rule, so a value past it is refused there with the same message as the library.
_LIMIT_SECONDS = re.compile(r"[0-9]{1,10}")
_LIMIT_CLOCK = re.compile(r"([0-9]{1,6}):([0-5][0-9]):([0-5][0-9])")


def _parse_time_limit(value: str) -> timedelta:
    """Parse ``--execution-time-limit``: whole seconds (``259200``) or ``HH:MM:SS`` (``72:00:00``).

    Hours may exceed 23; ``0`` and ``00:00:00`` mean no limit.  Parsed here rather than by
    Pydantic, whose lax mode would also take a float, an ISO 8601 duration or a non-ASCII digit.

    Raises:
        ValueError: ``value`` is neither form.

    Example:
        >>> _parse_time_limit("72:00:00") == _parse_time_limit("259200") == timedelta(hours=72)
        True
    """
    if _LIMIT_SECONDS.fullmatch(value):
        return timedelta(seconds=int(value))
    clock = _LIMIT_CLOCK.fullmatch(value)
    if clock is None:
        raise ValueError(f"--execution-time-limit {value!r} is neither whole seconds (259200) nor HH:MM:SS (72:00:00)")
    hours, minutes, seconds = (int(part) for part in clock.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _settings(
    *,
    allow_start_if_on_batteries: bool | None,
    stop_if_going_on_batteries: bool | None,
    execution_time_limit: str | None,
    multiple_instances: str | None,
    allow_hard_terminate: bool | None,
    allow_demand_start: bool | None,
) -> TaskSettings | None:
    """Build the task settings, or None when no settings flag was given (every OS default kept).

    Raises:
        ValueError: The time limit is malformed, or a value breaks a :class:`TaskSettings` rule.

    Example:
        >>> unset = dict(allow_start_if_on_batteries=None, stop_if_going_on_batteries=None,
        ...              execution_time_limit=None, multiple_instances=None, allow_hard_terminate=None,
        ...              allow_demand_start=None)
        >>> _settings(**unset) is None
        True
        >>> _settings(**{**unset, "multiple_instances": "ignorenew"}).multiple_instances.value
        'IgnoreNew'
    """
    given = (
        allow_start_if_on_batteries,
        stop_if_going_on_batteries,
        execution_time_limit,
        multiple_instances,
        allow_hard_terminate,
        allow_demand_start,
    )
    if all(flag is None for flag in given):
        return None
    return TaskSettings(
        allow_start_if_on_batteries=allow_start_if_on_batteries,
        stop_if_going_on_batteries=stop_if_going_on_batteries,
        execution_time_limit=None if execution_time_limit is None else _parse_time_limit(execution_time_limit),
        multiple_instances=None
        if multiple_instances is None
        else enum_by_value(TaskMultipleInstances, multiple_instances),
        allow_hard_terminate=allow_hard_terminate,
        allow_demand_start=allow_demand_start,
    )


@click.command("register_scheduled_task")
@argument("task_path")
@option("--program", required=True, help="Executable to run.")
@option("--arguments", default="", help="Program arguments.")
@option("--description", default="", help="Task description.")
@option(
    "--run-as",
    default=None,
    help="Principal: a SID, a decimal uid (Linux), or a WellKnownSid name such as local_system. "
    "Default: the caller on Windows, root on Linux. Account names are refused.",
)
@option(
    "--run-level",
    type=CaseInsensitiveChoice([e.value for e in TaskRunLevel]),
    default=TaskRunLevel.LIMITED.value,
    show_default=True,
    help="Token the task runs with (Highest = elevated).",
)
@option(
    "--logon-type",
    type=CaseInsensitiveChoice([e.value for e in TaskLogonType]),
    default=None,
    help="Windows logon type; default follows --run-as (service account, S4U, or interactive).",
)
@option("--at", "at_times", multiple=True, help="Run once at this ISO 8601 time with a UTC offset (repeatable).")
@option(
    "--daily", "daily_times", multiple=True, help="Run daily from this ISO 8601 time with a UTC offset (repeatable)."
)
@option("--every-days", type=int, default=1, show_default=True, help="Day interval for every --daily trigger.")
@option("--at-boot", is_flag=True, help="Run at boot.")
@option("--boot-delay", type=int, default=0, show_default=True, help="Seconds after boot (with --at-boot).")
@option("--at-logon", is_flag=True, help="Run at logon (Windows only).")
@option("--logon-user", default=None, help="SID whose logon triggers the task (with --at-logon; default any user).")
@option(
    "--allow-start-if-on-batteries/--disallow-start-if-on-batteries",
    default=None,
    help="Whether a run may start on battery power. Default: the OS default.",
)
@option(
    "--stop-if-going-on-batteries/--dont-stop-if-going-on-batteries",
    default=None,
    help="Whether a running task is stopped when the machine switches to battery; Linux accepts only the "
    "--dont- form. Default: the OS default.",
)
@option(
    "--execution-time-limit",
    default=None,
    help="Longest a run may last: whole seconds (259200) or HH:MM:SS (72:00:00); 0 means no limit. "
    "Default: the OS default.",
)
@option(
    "--multiple-instances",
    type=CaseInsensitiveChoice([e.value for e in TaskMultipleInstances]),
    default=None,
    help="What a start does while a run is active; Linux supports only IgnoreNew, and refuses it together "
    "with --daily. Default: the OS default.",
)
@option(
    "--allow-hard-terminate/--disallow-hard-terminate",
    default=None,
    help="Whether a run that ignores a stop request may be killed. Default: the OS default.",
)
@option(
    "--allow-demand-start/--disallow-demand-start",
    default=None,
    help="Whether the task may be started on demand (start_scheduled_task) besides its triggers. Default: allowed.",
)
@click.pass_context
def cli_register_scheduled_task(
    ctx: click.Context,
    task_path: str,
    program: str,
    arguments: str,
    description: str,
    run_as: str | None,
    run_level: str,
    logon_type: str | None,
    at_times: tuple[str, ...],
    daily_times: tuple[str, ...],
    every_days: int,
    at_boot: bool,
    boot_delay: int,
    at_logon: bool,
    logon_user: str | None,
    allow_start_if_on_batteries: bool | None,
    stop_if_going_on_batteries: bool | None,
    execution_time_limit: str | None,
    multiple_instances: str | None,
    allow_hard_terminate: bool | None,
    allow_demand_start: bool | None,
) -> None:
    """Register a task with an optional principal, triggers and settings; no trigger means run on demand (mutating).

    Times are ISO 8601 with a UTC offset (2026-10-21T03:00:00+02:00, or Z for UTC); a time
    without one is refused. On Linux a task becomes a systemd .service plus, with triggers,
    a .timer; logon triggers and --every-days above 1 are Windows-only. A settings flag left
    out keeps the OS default; Linux refuses a setting systemd has no analog for.

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_register_scheduled_task, ["--help"]).exit_code
        0
    """
    _check_modifiers(
        daily=bool(daily_times),
        every_days=every_days,
        at_boot=at_boot,
        boot_delay=boot_delay,
        at_logon=at_logon,
        logon_user=logon_user,
    )
    try:
        level = enum_by_value(TaskRunLevel, run_level)
        logon = None if logon_type is None else enum_by_value(TaskLogonType, logon_type)
        principal = _principal(run_as, level, logon)
        triggers = _triggers(
            at_times=at_times,
            daily_times=daily_times,
            every_days=every_days,
            at_boot=at_boot,
            boot_delay=boot_delay,
            at_logon=at_logon,
            logon_user=logon_user,
        )
        settings = _settings(
            allow_start_if_on_batteries=allow_start_if_on_batteries,
            stop_if_going_on_batteries=stop_if_going_on_batteries,
            execution_time_limit=execution_time_limit,
            multiple_instances=multiple_instances,
            allow_hard_terminate=allow_hard_terminate,
            allow_demand_start=allow_demand_start,
        )
    except ValueError as exc:  # ValidationError is a ValueError; fromisoformat raises a plain one
        raise click.BadParameter(_render(exc)) from exc
    info = _ps(ctx).register_scheduled_task(
        task_path,
        program=program,
        arguments=arguments,
        description=description,
        principal=principal,
        triggers=triggers,
        settings=settings,
    )
    click.echo(f"registered {task_path} as {_principal_label(info.user_id)}")


@click.command("unregister_scheduled_task")
@argument("task_path")
@click.pass_context
def cli_unregister_scheduled_task(ctx: click.Context, task_path: str) -> None:
    """Delete a scheduled task (mutating)."""
    _ps(ctx).unregister_scheduled_task(task_path)
    click.echo(f"unregistered {task_path}")


@click.command("enable_scheduled_task")
@argument("task_path")
@click.pass_context
def cli_enable_scheduled_task(ctx: click.Context, task_path: str) -> None:
    """Enable a scheduled task (mutating)."""
    _ps(ctx).enable_scheduled_task(task_path)
    click.echo(f"enabled {task_path}")


@click.command("disable_scheduled_task")
@argument("task_path")
@click.pass_context
def cli_disable_scheduled_task(ctx: click.Context, task_path: str) -> None:
    """Disable a scheduled task (mutating)."""
    _ps(ctx).disable_scheduled_task(task_path)
    click.echo(f"disabled {task_path}")


@click.command("start_scheduled_task")
@argument("task_path")
@click.pass_context
def cli_run_scheduled_task(ctx: click.Context, task_path: str) -> None:
    """Start a scheduled task now (mutating)."""
    _ps(ctx).start_scheduled_task(task_path)
    click.echo(f"started {task_path}")


@click.command("stop_scheduled_task")
@argument("task_path")
@click.pass_context
def cli_stop_scheduled_task(ctx: click.Context, task_path: str) -> None:
    """Stop a running scheduled task (mutating)."""
    _ps(ctx).stop_scheduled_task(task_path)
    click.echo(f"stopped {task_path}")


MUTATING_COMMANDS = (
    cli_start_service,
    cli_stop_service,
    cli_restart_service,
    cli_set_service_startup,
    cli_set_registry_value,
    cli_remove_registry_value,
    cli_new_registry_key,
    cli_remove_registry_key,
    cli_clear_event_log,
    cli_new_local_user,
    cli_remove_local_user,
    cli_enable_local_user,
    cli_disable_local_user,
    cli_new_local_group,
    cli_remove_local_group,
    cli_add_local_group_member,
    cli_remove_local_group_member,
    cli_add_acl_ace,
    cli_remove_acl_ace,
    cli_set_owner,
    cli_register_scheduled_task,
    cli_unregister_scheduled_task,
    cli_enable_scheduled_task,
    cli_disable_scheduled_task,
    cli_run_scheduled_task,
    cli_stop_scheduled_task,
)

__all__ = ["MUTATING_COMMANDS"]
