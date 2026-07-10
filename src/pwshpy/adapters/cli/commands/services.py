"""``pwshpy services`` - list services through the pipeline (portable).

A thin front end over the ``ps`` facade (win32service on Windows, systemd over
D-Bus on Linux), mirroring ``Get-Service``.  Streams
:class:`~pwshpy.domain.records.ServiceInfo` records;
filtering / sorting is done in Python via the pipeline, not via cmdlet switches
(see ``docs/powershell-switch-mapping.md``).

Contents:
    * :func:`cli_services` - the ``services`` subcommand.
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


@click.command("get_service", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_services(ctx: click.Context, as_json: bool, as_jsonl: bool) -> None:
    """List services as typed records - win32service on Windows, systemd on Linux (native; like Get-Service).

    Filter and sort in Python via the pipeline, not switches, e.g.
    ``ps.get_service().where(lambda s: s.status == ServiceState.RUNNING)``.

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_services, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-services", extra={"command": "services"}):
        logger.info("Listing services")
        emit(ps.get_service(), fmt)


__all__ = ["cli_services"]
