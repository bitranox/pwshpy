"""``pwshpy get_volume`` - list mounted filesystems through the pipeline.

A thin front end over the ``ps`` facade (native / psutil), mirroring ``get_process``
and ``get_net_tcp_connection``: parse options, pick a format, stream
:class:`~pwshpy.domain.records.DiskUsage` records.

Contents:
    * :func:`cli_disks` - the ``get_volume`` subcommand.
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


@click.command("get_volume", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@option("--limit", "-n", "limit", type=int, default=None, help="Emit at most N filesystems.")
@click.pass_context
def cli_disks(ctx: click.Context, as_json: bool, as_jsonl: bool, limit: int | None) -> None:
    """List mounted filesystems and their usage as typed records (native).

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_disks, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with bind_command(ctx):
        logger.info("Listing mounted filesystems")
        pipeline = ps.get_volume()
        if limit is not None:
            pipeline = pipeline.take(limit)
        emit(pipeline, fmt)


__all__ = ["cli_disks"]
