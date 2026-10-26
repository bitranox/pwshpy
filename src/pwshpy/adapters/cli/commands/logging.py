"""Logging demonstration CLI command.

Provides a command to preview log output using lib_log_rich's logdemo facility.

Contents:
    * :func:`cli_logdemo` - Run a logging demonstration.
"""

from __future__ import annotations

import rich_click as click
from lib_log_rich.domain import ConsoleTheme

from ..constants import CLICK_CONTEXT_SETTINGS
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
    """Run a logging demonstration to preview log output."""
    import lib_log_rich
    import lib_log_rich.runtime

    # logdemo() requires uninitialized runtime
    if lib_log_rich.runtime.is_initialised():
        lib_log_rich.runtime.shutdown()

    result = lib_log_rich.logdemo(theme=ConsoleTheme(theme))
    click.echo(f"\nLog demo completed (theme: {result.theme})")


__all__ = ["cli_logdemo"]
