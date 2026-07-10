"""``pwshpy resolve NAME`` — resolve a host name through the pipeline.

A thin front end over the ``ps`` facade (native / stdlib socket), mirroring
``Resolve-DnsName``.  Streams :class:`~pwshpy.domain.records.DnsRecord` records;
a resolution failure surfaces as a clean CLI error via the exit-code machinery.

Contents:
    * :func:`cli_resolve` — the ``resolve`` subcommand.
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


@click.command("resolve_dns_name", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("name")
@option("--timeout", "timeout", type=float, default=None, help="Resolution timeout in seconds.")
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_resolve(ctx: click.Context, name: str, timeout: float | None, as_json: bool, as_jsonl: bool) -> None:
    """Resolve NAME to its addresses as typed records (native, native).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_resolve, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-resolve", extra={"command": "resolve"}):
        logger.info("Resolving host name")
        emit(ps.resolve_dns_name(name, timeout=timeout), fmt)


__all__ = ["cli_resolve"]
