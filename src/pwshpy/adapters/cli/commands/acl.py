"""``pwshpy acl`` - list a path's DACL entries through the pipeline.

A thin front end over the ``ps`` facade (native / win32security), mirroring
``Get-Acl``.  Emits one :class:`~pwshpy.domain.records.AclEntry` per
access-control entry, identified by trustee SID (locale-independent).  Filtering
is done in Python via the pipeline.

Contents:
    * :func:`cli_acl` - the ``acl`` subcommand.
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


@click.command("get_acl", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_acl(ctx: click.Context, path: str, as_json: bool, as_jsonl: bool) -> None:
    """List the ACL entries of PATH - win32 DACLs on Windows, POSIX ACLs on Linux (native; like Get-Acl).

    One record per access-control entry, identified by trustee SID.  Filter in
    Python via the pipeline, e.g. keep the Deny entries.

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_acl, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-acl", extra={"command": "acl"}):
        logger.info("Reading ACL")
        emit(ps.get_acl(path), fmt)


__all__ = ["cli_acl"]
