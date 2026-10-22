"""``pwshpy scheduled-tasks`` - list Windows scheduled tasks through the pipeline.

A thin front end over the ``ps`` facade (native / Task Scheduler COM), mirroring
``Get-ScheduledTask``.  Streams :class:`~pwshpy.domain.records.ScheduledTaskInfo`
records while recursing the folder tree; filtering / sorting is done in Python via
the pipeline (see ``docs/powershell-switch-mapping.md``).

Contents:
    * :func:`cli_scheduled_tasks` - the ``scheduled-tasks`` subcommand.
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


@click.command("get_scheduled_task", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--folder", "folder_path", default="\\", help="Task-folder path to recurse (default: root).")
@option(
    "--strict",
    is_flag=True,
    default=False,
    help="Fail on a folder, task or timer unit that cannot be read, instead of skipping it with a warning.",
)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_scheduled_tasks(ctx: click.Context, folder_path: str, strict: bool, as_json: bool, as_jsonl: bool) -> None:
    """List scheduled tasks - Task Scheduler on Windows, systemd timers on Linux (native; like Get-ScheduledTask).

    Recurses the Task Scheduler folder tree from ``--folder`` (default root).
    Filter and sort in Python via the pipeline, not switches. What cannot be read is skipped
    with a warning; with ``--strict`` it exits 1 naming the folder, task or unit instead.

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_scheduled_tasks, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-scheduled-tasks", extra={"command": "scheduled-tasks"}):
        logger.info("Listing scheduled tasks")
        emit(ps.get_scheduled_task(folder_path, strict=strict), fmt)


__all__ = ["cli_scheduled_tasks"]
