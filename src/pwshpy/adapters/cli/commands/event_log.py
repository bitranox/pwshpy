"""``pwshpy eventlog`` - stream a Windows event log through the pipeline.

A thin front end over the ``ps`` facade (native / win32evtlog), mirroring
``Get-WinEvent``.  Streams :class:`~pwshpy.domain.records.EventLogEntry` records
newest-first and lazily, so ``pwshpy eventlog System --jsonl | head`` reads only
a handful of events (memory-bounded).  Filtering / sorting is done in Python via
the pipeline (see ``docs/powershell-switch-mapping.md``).

Contents:
    * :func:`cli_event_log` - the ``eventlog`` subcommand.
"""

from __future__ import annotations

import logging

import lib_log_rich.runtime
import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit
from ..typed_click import argument, option
from ._common import resolve_format

logger = logging.getLogger(__name__)


@click.command("get_win_event", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("log_name")
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_event_log(ctx: click.Context, log_name: str, as_json: bool, as_jsonl: bool) -> None:
    """Stream event log LOG_NAME - win32evtlog on Windows, journald on Linux (native; like Get-WinEvent).

    LOG_NAME is a channel, e.g. ``System``, ``Application``, ``Security``.  Reads
    newest-first and streams lazily, so ``eventlog System --jsonl | head`` is cheap.

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_event_log, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-eventlog", extra={"command": "eventlog"}):
        logger.info("Reading event log")
        emit(ps.get_win_event(log_name), fmt)


__all__ = ["cli_event_log"]
