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

from typing import TYPE_CHECKING

import rich_click as click

from ....domain.enums import AceType, RegistryValueType, ServiceStartType
from ..context import get_cli_context
from ..typed_click import argument, option

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

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_start_service, ["--help"]).exit_code
        0
    """
    result = _ps(ctx).start_service(name)
    click.echo(f"{name}: {result.status.value}")


@click.command("stop_service")
@argument("name")
@click.pass_context
def cli_stop_service(ctx: click.Context, name: str) -> None:
    """Stop a service - win32service on Windows, systemd on Linux (mutating)."""
    result = _ps(ctx).stop_service(name)
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
    default="REG_SZ",
    help="Registry value type (default REG_SZ).",
)
@click.pass_context
def cli_set_registry_value(ctx: click.Context, key: str, name: str, data: str, value_type: str) -> None:
    """Create or overwrite a registry value (mutating)."""
    vtype = RegistryValueType(value_type)
    _ps(ctx).set_item_property(key, name, _coerce_registry_data(data, vtype), vtype)
    click.echo(f"set {key}\\{name} ({value_type})")


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


@click.command("register_scheduled_task")
@argument("task_path")
@option("--program", required=True, help="Executable to run.")
@option("--arguments", default="", help="Program arguments.")
@option("--description", default="", help="Task description.")
@click.pass_context
def cli_register_scheduled_task(
    ctx: click.Context, task_path: str, program: str, arguments: str, description: str
) -> None:
    """Register a run-on-demand task (mutating)."""
    _ps(ctx).register_scheduled_task(task_path, program=program, arguments=arguments, description=description)
    click.echo(f"registered {task_path}")


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
