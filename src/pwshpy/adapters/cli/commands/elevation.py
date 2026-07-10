"""``pwshpy is-elevated`` — report administrative-privilege status.

A thin front end over the ``ps`` facade (native, portable).  Prints whether the
process is elevated and mirrors it in the exit code (0 = elevated, 1 = not), so it
scripts cleanly: ``pwshpy is-elevated -q && pwshpy clear-event-log Application``.

The companion global flag ``pwshpy --elevate <subcommand …>`` (defined on the root
group) relaunches the command elevated via UAC when needed.

Contents:
    * :func:`cli_is_elevated` — the ``is-elevated`` subcommand.
"""

from __future__ import annotations

import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..typed_click import option


@click.command("is-elevated", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--quiet", "-q", is_flag=True, default=False, help="Suppress output; signal via the exit code only.")
@click.pass_context
def cli_is_elevated(ctx: click.Context, quiet: bool) -> None:
    """Report whether pwshpy is elevated; exit 0 if elevated, 1 if not (native, portable).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_is_elevated, ["--help"]).exit_code
        0
    """
    elevated = get_cli_context(ctx).services.ps.is_elevated()
    if not quiet:
        click.echo("elevated" if elevated else "not elevated")
    ctx.exit(0 if elevated else 1)


__all__ = ["cli_is_elevated"]
