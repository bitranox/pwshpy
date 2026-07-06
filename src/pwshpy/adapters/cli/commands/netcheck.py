"""``pwshpy test-connection HOST`` — probe TCP reachability through the pipeline.

A thin front end over the ``ps`` facade (Tier A / stdlib socket), mirroring
``Test-Connection`` (TCP-connect form).  Emits a single
:class:`~pwshpy.domain.records.ConnectionTest` record.

Contents:
    * :func:`cli_test_connection` — the ``test-connection`` subcommand.
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


@click.command("test-connection", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("host")
@option("--port", "-p", "port", type=int, default=443, help="Target TCP port (default 443).")
@option("--timeout", "timeout", type=float, default=5.0, help="Timeout in seconds (default 5).")
@option("--json", "as_json", is_flag=True, default=False, help="Emit the record as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Emit the record as one JSON line.")
@click.pass_context
def cli_test_connection(
    ctx: click.Context,
    host: str,
    port: int,
    timeout: float,
    as_json: bool,
    as_jsonl: bool,
) -> None:
    """Probe TCP reachability of HOST:PORT as a typed record (Tier A, native).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_test_connection, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-test-connection", extra={"command": "test-connection"}):
        logger.info("Probing TCP reachability")
        emit([ps.test_connection(host, port=port, timeout=timeout)], fmt)


__all__ = ["cli_test_connection"]
