"""``pwshpy get_uptime`` - show system boot time and elapsed uptime.

A thin front end over the ``ps`` facade (native / psutil).  Emits a single
:class:`~pwshpy.domain.records.SystemUptime` record through the shared output
path (``--json`` / ``--jsonl`` / human table).

Contents:
    * :func:`cli_uptime` - the ``get_uptime`` subcommand.
"""

from __future__ import annotations

import logging

import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit
from ..typed_click import option
from ._common import bind_command, resolve_format

logger = logging.getLogger(__name__)


@click.command("get_uptime", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit the record as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Emit the record as one JSON line.")
@click.pass_context
def cli_uptime(ctx: click.Context, as_json: bool, as_jsonl: bool) -> None:
    """Show system boot time and elapsed uptime as a typed record (native).

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_uptime, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with bind_command(ctx):
        logger.info("Reading system uptime")
        emit([ps.get_uptime()], fmt)


__all__ = ["cli_uptime"]
