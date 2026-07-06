"""``pwshpy disks`` — list mounted filesystems through the pipeline.

A thin front end over the ``ps`` facade (Tier A / psutil), mirroring ``processes``
and ``connections``: parse options, pick a format, stream
:class:`~pwshpy.domain.records.DiskUsage` records.

Contents:
    * :func:`cli_disks` — the ``disks`` subcommand.
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


@click.command("disks", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@option("--limit", "-n", "limit", type=int, default=None, help="Emit at most N filesystems.")
@click.pass_context
def cli_disks(ctx: click.Context, as_json: bool, as_jsonl: bool, limit: int | None) -> None:
    """List mounted filesystems and their usage as typed records (Tier A, native).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_disks, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-disks", extra={"command": "disks"}):
        logger.info("Listing mounted filesystems")
        pipeline = ps.disks()
        if limit is not None:
            pipeline = pipeline.take(limit)
        emit(pipeline, fmt)


__all__ = ["cli_disks"]
