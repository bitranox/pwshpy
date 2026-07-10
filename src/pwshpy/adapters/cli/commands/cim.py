"""``pwshpy cim`` - query CIM/WMI instances through the pipeline.

A thin front end over the ``ps`` facade (native / WBEM), mirroring
``Get-CimInstance``.  Streams :class:`~pwshpy.domain.records.CimInstance` records
lazily (forward-only), so querying an unbounded class and piping to ``head`` stays
cheap.  CIM instances are open key/value bags - view them with ``--jsonl``.

Contents:
    * :func:`cli_cim` - the ``cim`` subcommand.
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


@click.command("get_cim_instance", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("class_name")
@option("--where", "where", default=None, help='Read-only WQL filter fragment, e.g. "DriveType = 3".')
@option("--namespace", "namespace", default="root/cimv2", help="WMI namespace (default: root/cimv2).")
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_cim(
    ctx: click.Context, class_name: str, where: str | None, namespace: str, as_json: bool, as_jsonl: bool
) -> None:
    """Query CIM/WMI class CLASS_NAME as typed records (native; like Get-CimInstance).

    CLASS_NAME is a WMI class, e.g. ``Win32_OperatingSystem``, ``Win32_LogicalDisk``.
    Streams lazily, so even an unbounded class (``CIM_DataFile``) stays cheap with
    ``--jsonl | head``.

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_cim, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-cim", extra={"command": "cim"}):
        logger.info("Querying CIM/WMI")
        emit(ps.get_cim_instance(class_name, where=where, namespace=namespace), fmt)


__all__ = ["cli_cim"]
