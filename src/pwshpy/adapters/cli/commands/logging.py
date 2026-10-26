"""Logging demonstration CLI command.

Provides a command to preview log output using lib_log_rich's logdemo facility.

Contents:
    * :func:`cli_logdemo` - Run a logging demonstration.
"""

from __future__ import annotations

import rich_click as click
from lib_log_rich.domain import ConsoleTheme

from pwshpy.adapters.logging.setup import InvalidLoggingConfigError, run_logdemo

from ..config_load import report_load_failure
from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..exit_codes import ExitCode
from ..typed_click import option


@click.command("logdemo", context_settings=CLICK_CONTEXT_SETTINGS)
@option(
    "--theme",
    # click matches an Enum member on its NAME, so offer the lowercase values and convert below.
    type=click.Choice(sorted(theme.value for theme in ConsoleTheme)),
    default=ConsoleTheme.CLASSIC.value,
    help="Logging theme to preview",
)
@click.pass_context
def cli_logdemo(ctx: click.Context, theme: str) -> None:
    """Run a logging demonstration to preview log output.

    A ``LOG_*`` variable lib_log_rich refuses ends the command with exit 78 and lib_log_rich's
    own line, as it ends ``config``.
    """
    try:
        result = run_logdemo(ConsoleTheme(theme))
    except InvalidLoggingConfigError as exc:
        report_load_failure(exc, show_traceback=get_cli_context(ctx).traceback)
        ctx.exit(ExitCode.CONFIG_ERROR)
    click.echo(f"\nLog demo completed (theme: {result.theme})")


__all__ = ["cli_logdemo"]
