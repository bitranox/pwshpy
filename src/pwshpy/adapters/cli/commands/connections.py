"""``pwshpy connections`` — list open network connections through the pipeline.

A thin front end over the ``ps`` facade (native / psutil), mirroring
``processes``: it parses options, picks an output format, and streams
:class:`~pwshpy.domain.records.NetConnection` records.  ``--jsonl`` stays lazy
end to end.

Contents:
    * :func:`cli_connections` — the ``connections`` subcommand.
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


@click.command("get_net_tcp_connection", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@option("--limit", "-n", "limit", type=int, default=None, help="Emit at most N connections.")
@click.pass_context
def cli_connections(ctx: click.Context, as_json: bool, as_jsonl: bool, limit: int | None) -> None:
    """List open network connections as typed records (native, native).

    The ``ps`` facade is injected via the CLI context (wired at the composition
    root), so this adapter never imports the composition layer.

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_connections, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-connections", extra={"command": "connections"}):
        logger.info("Listing network connections")
        pipeline = ps.get_net_tcp_connection()
        if limit is not None:
            pipeline = pipeline.take(limit)
        emit(pipeline, fmt)


__all__ = ["cli_connections"]
