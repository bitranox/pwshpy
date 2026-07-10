"""``pwshpy processes`` — list running processes through the shared pipeline.

A thin front end over the ``ps`` facade (native / psutil): it parses options,
picks an output format, and streams :class:`~pwshpy.domain.records.ProcessInfo`
records.  ``--jsonl`` stays lazy end to end so ``| head`` is cheap and exits
cleanly.

Contents:
    * :func:`cli_processes` — the ``processes`` subcommand.
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


@click.command("get_process", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@option("--limit", "-n", "limit", type=int, default=None, help="Emit at most N processes.")
@click.pass_context
def cli_processes(ctx: click.Context, as_json: bool, as_jsonl: bool, limit: int | None) -> None:
    """List running processes as typed records (native, native — no pwsh subprocess).

    The ``ps`` facade is injected via the CLI context (wired at the composition
    root), so this adapter never imports the composition layer.

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_processes, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-processes", extra={"command": "processes"}):
        logger.info("Listing processes")
        pipeline = ps.get_process()
        if limit is not None:
            pipeline = pipeline.take(limit)
        emit(pipeline, fmt)


__all__ = ["cli_processes"]
