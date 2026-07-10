"""``pwshpy local-users`` / ``local-groups`` - list local accounts through the pipeline.

Thin front ends over the ``ps`` facade (native / win32net), mirroring
``Get-LocalUser`` and ``Get-LocalGroup``.  Records are identified by SID
(locale-independent); the SAM name is localized for built-ins.  Filtering is done
in Python via the pipeline.

Contents:
    * :func:`cli_local_users` - the ``local-users`` subcommand.
    * :func:`cli_local_groups` - the ``local-groups`` subcommand.
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


@click.command("get_local_user", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_local_users(ctx: click.Context, as_json: bool, as_jsonl: bool) -> None:
    """List local user accounts - win32net on Windows, pwd on Linux (native; like Get-LocalUser).

    Records are identified by SID (locale-independent).  Filter in Python via the
    pipeline, e.g. keep the disabled accounts.

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_local_users, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-local-users", extra={"command": "local-users"}):
        logger.info("Listing local users")
        emit(ps.get_local_user(), fmt)


@click.command("get_local_group", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_local_groups(ctx: click.Context, as_json: bool, as_jsonl: bool) -> None:
    """List local groups - win32net on Windows, grp on Linux (native; like Get-LocalGroup).

    Records are identified by SID (locale-independent).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_local_groups, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-local-groups", extra={"command": "local-groups"}):
        logger.info("Listing local groups")
        emit(ps.get_local_group(), fmt)


__all__ = ["cli_local_groups", "cli_local_users"]
