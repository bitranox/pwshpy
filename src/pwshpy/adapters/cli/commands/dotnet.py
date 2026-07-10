"""``pwshpy`` .NET commands - run PowerShell in-process and emit JSON.

``run`` executes an arbitrary script; ``cmdlet`` runs one cmdlet with safely-bound
``-p KEY=VALUE`` parameters; ``get_command`` introspects a cmdlet's parameters.  On top sit the
module wrappers - ``get_ad_user`` / ``get_ad_group`` / ``get_ad_computer`` / ``get_mailbox`` /
``get_az_vm`` / ``get_az_resource_group`` - each a fixed-cmdlet ``cmdlet`` for the AD / Exchange /
Azure long tail.  All need the ``[full]`` extra (else they exit with a clear
``FeatureUnavailableError``); the module wrappers also need their PowerShell module present.  They
turn the ``pwshpy`` CLI into a JSON-emitting PowerShell runner for shell one-liners.

Contents:
    * the ``cli_*`` .NET commands, collected in ``DOTNET_COMMANDS``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import rich_click as click

from ....domain.records import PSInvocationResult
from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit, emit_json
from ..typed_click import argument, option
from ._common import parse_pairs, resolve_format


@click.command("run", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("script")
@option("--timeout", type=float, default=None, help="Seconds before the script is abandoned.")
@click.pass_context
def cli_run(ctx: click.Context, script: str, timeout: float | None) -> None:
    """Run a PowerShell script in-process and print the marshaled output as JSON (.NET).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_run, ["--help"]).exit_code
        0
    """
    emit_json(get_cli_context(ctx).services.ps.run(script, timeout=timeout))


@click.command("cmdlet", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("name")
@option("--param", "-p", "params", multiple=True, help="A cmdlet parameter as KEY=VALUE (repeatable).")
@option("--timeout", type=float, default=None, help="Seconds before the cmdlet is abandoned.")
@option("--streams", is_flag=True, default=False, help="Emit all six streams, not just the output.")
@click.pass_context
def cli_cmdlet(ctx: click.Context, name: str, params: tuple[str, ...], timeout: float | None, streams: bool) -> None:
    """Run one cmdlet with safely-bound -p KEY=VALUE params; print JSON (.NET).

    Values are bound as strings (PowerShell coerces most); for typed/switch parameters use ``run``.
    """
    result = get_cli_context(ctx).services.ps.cmdlet(name, timeout=timeout, **parse_pairs(params, "=", "KEY=VALUE"))
    emit_json(result.to_dict() if streams else result.output)


@click.command("get_command", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("name")
@option("--json", "as_json", is_flag=True, default=False, help="Emit the record as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Emit the record as one JSON line.")
@click.pass_context
def cli_get_command(ctx: click.Context, name: str, as_json: bool, as_jsonl: bool) -> None:
    """Introspect a cmdlet's parameter metadata as a typed record (.NET; like Get-Command)."""
    ps = get_cli_context(ctx).services.ps
    emit([ps.get_command(name)], resolve_format(as_json, as_jsonl))


def _cmdlet_wrapper_options(command: Callable[..., None]) -> Callable[..., Any]:
    """Attach the shared ``-p`` / ``--timeout`` / ``--streams`` options to a module-wrapper command."""
    command = option("--param", "-p", "params", multiple=True, help="A cmdlet parameter as KEY=VALUE (repeatable).")(
        command
    )
    command = option("--timeout", type=float, default=None, help="Seconds before the cmdlet is abandoned.")(command)
    command = option("--streams", is_flag=True, default=False, help="Emit all six streams, not just the output.")(
        command
    )
    return command


def _emit_cmdlet(result: PSInvocationResult, *, streams: bool) -> None:
    """Emit a cmdlet result as JSON: all six streams with ``--streams``, else just the output."""
    emit_json(result.to_dict() if streams else result.output)


@click.command("get_ad_user", context_settings=CLICK_CONTEXT_SETTINGS)
@_cmdlet_wrapper_options
@click.pass_context
def cli_get_ad_user(ctx: click.Context, params: tuple[str, ...], timeout: float | None, streams: bool) -> None:
    """Query Active Directory users (.NET; Get-ADUser, needs [full] + the ActiveDirectory module)."""
    result = get_cli_context(ctx).services.ps.get_ad_user(timeout=timeout, **parse_pairs(params, "=", "KEY=VALUE"))
    _emit_cmdlet(result, streams=streams)


@click.command("get_ad_group", context_settings=CLICK_CONTEXT_SETTINGS)
@_cmdlet_wrapper_options
@click.pass_context
def cli_get_ad_group(ctx: click.Context, params: tuple[str, ...], timeout: float | None, streams: bool) -> None:
    """Query Active Directory groups (.NET; Get-ADGroup, needs [full] + the ActiveDirectory module)."""
    result = get_cli_context(ctx).services.ps.get_ad_group(timeout=timeout, **parse_pairs(params, "=", "KEY=VALUE"))
    _emit_cmdlet(result, streams=streams)


@click.command("get_ad_computer", context_settings=CLICK_CONTEXT_SETTINGS)
@_cmdlet_wrapper_options
@click.pass_context
def cli_get_ad_computer(ctx: click.Context, params: tuple[str, ...], timeout: float | None, streams: bool) -> None:
    """Query Active Directory computers (.NET; Get-ADComputer, needs [full] + the ActiveDirectory module)."""
    result = get_cli_context(ctx).services.ps.get_ad_computer(timeout=timeout, **parse_pairs(params, "=", "KEY=VALUE"))
    _emit_cmdlet(result, streams=streams)


@click.command("get_mailbox", context_settings=CLICK_CONTEXT_SETTINGS)
@_cmdlet_wrapper_options
@click.pass_context
def cli_get_mailbox(ctx: click.Context, params: tuple[str, ...], timeout: float | None, streams: bool) -> None:
    """Query Exchange mailboxes (.NET; Get-Mailbox, needs [full] + the Exchange module)."""
    result = get_cli_context(ctx).services.ps.get_mailbox(timeout=timeout, **parse_pairs(params, "=", "KEY=VALUE"))
    _emit_cmdlet(result, streams=streams)


@click.command("get_az_vm", context_settings=CLICK_CONTEXT_SETTINGS)
@_cmdlet_wrapper_options
@click.pass_context
def cli_get_az_vm(ctx: click.Context, params: tuple[str, ...], timeout: float | None, streams: bool) -> None:
    """Query Azure virtual machines (.NET; Get-AzVM, needs [full] + the Az.Compute module)."""
    result = get_cli_context(ctx).services.ps.get_az_vm(timeout=timeout, **parse_pairs(params, "=", "KEY=VALUE"))
    _emit_cmdlet(result, streams=streams)


@click.command("get_az_resource_group", context_settings=CLICK_CONTEXT_SETTINGS)
@_cmdlet_wrapper_options
@click.pass_context
def cli_get_az_resource_group(
    ctx: click.Context, params: tuple[str, ...], timeout: float | None, streams: bool
) -> None:
    """Query Azure resource groups (.NET; Get-AzResourceGroup, needs [full] + the Az.Resources module)."""
    result = get_cli_context(ctx).services.ps.get_az_resource_group(
        timeout=timeout, **parse_pairs(params, "=", "KEY=VALUE")
    )
    _emit_cmdlet(result, streams=streams)


DOTNET_COMMANDS = (
    cli_run,
    cli_cmdlet,
    cli_get_command,
    cli_get_ad_user,
    cli_get_ad_group,
    cli_get_ad_computer,
    cli_get_mailbox,
    cli_get_az_vm,
    cli_get_az_resource_group,
)

__all__ = ["DOTNET_COMMANDS"]
