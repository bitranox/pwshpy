"""``pwshpy env`` — list environment variables through the pipeline.

A thin front end over the ``ps`` facade (Tier A / os.environ), mirroring the
PowerShell ``env:`` drive.  Streams :class:`~pwshpy.domain.records.EnvVar`
records.

Contents:
    * :func:`cli_env` — the ``env`` subcommand.
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


@click.command("env", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@option("--limit", "-n", "limit", type=int, default=None, help="Emit at most N variables.")
@click.pass_context
def cli_env(ctx: click.Context, as_json: bool, as_jsonl: bool, limit: int | None) -> None:
    """List environment variables as typed records (Tier A, native).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_env, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-env", extra={"command": "env"}):
        logger.info("Listing environment variables")
        pipeline = ps.environment()
        if limit is not None:
            pipeline = pipeline.take(limit)
        emit(pipeline, fmt)


__all__ = ["cli_env"]
