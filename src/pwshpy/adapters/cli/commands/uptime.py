"""``pwshpy uptime`` — show system boot time and elapsed uptime.

A thin front end over the ``ps`` facade (native / psutil).  Emits a single
:class:`~pwshpy.domain.records.SystemUptime` record through the shared output
path (``--json`` / ``--jsonl`` / human table).

Contents:
    * :func:`cli_uptime` — the ``uptime`` subcommand.
"""

from __future__ import annotations

import logging

import lib_log_rich.runtime
import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit
from ..typed_click import option
from ._common import resolve_format

logger = logging.getLogger(__name__)


@click.command("get_uptime", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit the record as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Emit the record as one JSON line.")
@click.pass_context
def cli_uptime(ctx: click.Context, as_json: bool, as_jsonl: bool) -> None:
    """Show system boot time and elapsed uptime as a typed record (native, native).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_uptime, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-uptime", extra={"command": "uptime"}):
        logger.info("Reading system uptime")
        emit([ps.get_uptime()], fmt)


__all__ = ["cli_uptime"]
