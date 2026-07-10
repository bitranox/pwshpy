"""``pwshpy`` networking commands - adapters, IP addresses, UDP endpoints (native, portable).

Thin front ends over the ``ps`` facade (psutil), mirroring the read-only Get-NetAdapter /
Get-NetIPAddress / Get-NetUDPEndpoint cmdlets.  Each streams typed records through the
shared output path (``--json`` / ``--jsonl`` / human table).

Contents:
    * the ``cli_*`` networking commands, collected in ``NETWORKING_COMMANDS``.
"""

from __future__ import annotations

import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit
from ..typed_click import option
from ._common import resolve_format


@click.command("get_net_adapter", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_get_net_adapter(ctx: click.Context, as_json: bool, as_jsonl: bool) -> None:
    """List network interfaces as typed records (native, portable; like Get-NetAdapter).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_get_net_adapter, ["--help"]).exit_code
        0
    """
    ps = get_cli_context(ctx).services.ps
    emit(ps.get_net_adapter(), resolve_format(as_json, as_jsonl))


@click.command("get_net_ip_address", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_get_net_ip_address(ctx: click.Context, as_json: bool, as_jsonl: bool) -> None:
    """List bound IP addresses as typed records (native, portable; like Get-NetIPAddress)."""
    ps = get_cli_context(ctx).services.ps
    emit(ps.get_net_ip_address(), resolve_format(as_json, as_jsonl))


@click.command("get_net_udp_endpoint", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_get_net_udp_endpoint(ctx: click.Context, as_json: bool, as_jsonl: bool) -> None:
    """List UDP sockets as typed records (native, portable; like Get-NetUDPEndpoint)."""
    ps = get_cli_context(ctx).services.ps
    emit(ps.get_net_udp_endpoint(), resolve_format(as_json, as_jsonl))


NETWORKING_COMMANDS = (
    cli_get_net_adapter,
    cli_get_net_ip_address,
    cli_get_net_udp_endpoint,
)

__all__ = ["NETWORKING_COMMANDS"]
