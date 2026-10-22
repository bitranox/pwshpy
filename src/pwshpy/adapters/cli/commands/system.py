"""``pwshpy`` system commands - inventory reads and process control.

``get_hotfix`` / ``get_computer_info`` / ``get_windows_image`` (read) mirror Get-Hotfix /
Get-ComputerInfo / Get-WindowsImage;
``get_windows_update`` searches Windows Update (read-only, bounded by ``--timeout``);
``stop_process`` / ``wait_process`` mirror Stop-Process / Wait-Process.  The destructive
``restart_computer`` / ``stop_computer`` act only with an explicit ``--yes``, so a mistyped
command never reboots the machine.

Contents:
    * the ``cli_*`` system commands, collected in ``SYSTEM_COMMANDS``.
"""

from __future__ import annotations

from typing import Any

import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit
from ..typed_click import argument, option
from ._common import TIMEOUT_RANGE, resolve_format


@click.command("get_hotfix", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_get_hotfix(ctx: click.Context, as_json: bool, as_jsonl: bool) -> None:
    """List installed Windows updates as typed records (native, Windows; like Get-Hotfix).

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_get_hotfix, ["--help"]).exit_code
        0
    """
    ps = get_cli_context(ctx).services.ps
    emit(ps.get_hotfix(), resolve_format(as_json, as_jsonl))


@click.command("get_windows_update", context_settings=CLICK_CONTEXT_SETTINGS)
@option(
    "--criteria",
    default=None,
    help="WUA search criteria (default: software updates not installed, \"IsInstalled=0 and Type='Software'\").",
)
@option(
    "--timeout",
    type=TIMEOUT_RANGE,
    default=None,
    help="Seconds before the search is abandoned and exits 110 (default 300).",
)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_get_windows_update(
    ctx: click.Context, criteria: str | None, timeout: float | None, as_json: bool, as_jsonl: bool
) -> None:
    """Search Windows Update for updates (native, Windows, read-only; exit 110 on timeout).

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_get_windows_update, ["--help"]).exit_code
        0
    """
    # Only the options given are forwarded, so the facade stays the one owner of the defaults.
    overrides: dict[str, Any] = {"criteria": criteria, "timeout": timeout}
    ps = get_cli_context(ctx).services.ps
    # An OperationTimeoutError propagates: main() maps it to exit 110 for every command.
    emit(
        ps.get_windows_update(**{key: value for key, value in overrides.items() if value is not None}),
        resolve_format(as_json, as_jsonl),
    )


@click.command("get_windows_image", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@option(
    "--index",
    type=click.IntRange(min=1),
    default=None,
    help="Show only the image with this 1-based index; an index the file lacks is an error.",
)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_get_windows_image(ctx: click.Context, path: str, index: int | None, as_json: bool, as_jsonl: bool) -> None:
    """List the images of a WIM file as typed records (native, portable; like Get-WindowsImage).

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_get_windows_image, ["--help"]).exit_code
        0
    """
    ps = get_cli_context(ctx).services.ps
    emit(ps.get_windows_image(path, index=index), resolve_format(as_json, as_jsonl))


@click.command("get_computer_info", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--json", "as_json", is_flag=True, default=False, help="Emit the record as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Emit the record as one JSON line.")
@click.pass_context
def cli_get_computer_info(ctx: click.Context, as_json: bool, as_jsonl: bool) -> None:
    """Show a summary of the local computer (native, portable; like Get-ComputerInfo)."""
    ps = get_cli_context(ctx).services.ps
    emit([ps.get_computer_info()], resolve_format(as_json, as_jsonl))


@click.command("stop_process", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("pid", type=int)
@option("--force", "-f", is_flag=True, default=False, help="Hard-kill instead of a graceful terminate.")
@click.pass_context
def cli_stop_process(ctx: click.Context, pid: int, force: bool) -> None:
    """Terminate a process by PID (mutating; like Stop-Process)."""
    get_cli_context(ctx).services.ps.stop_process(pid, force=force)
    click.echo(f"stopped PID {pid}")


@click.command("wait_process", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("pid", type=int)
@option("--timeout", type=float, default=None, help="Seconds to wait before failing.")
@click.pass_context
def cli_wait_process(ctx: click.Context, pid: int, timeout: float | None) -> None:
    """Wait for a process to exit and print its exit code (like Wait-Process)."""
    code = get_cli_context(ctx).services.ps.wait_process(pid, timeout=timeout)
    click.echo(f"PID {pid} exited with {code}")


@click.command("restart_computer", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--delay", "delay_seconds", type=int, default=0, help="Seconds to wait before restarting.")
@option("--yes", is_flag=True, default=False, help="Required to actually restart (a safety guard).")
@click.pass_context
def cli_restart_computer(ctx: click.Context, delay_seconds: int, yes: bool) -> None:
    """Restart the computer (Windows, DESTRUCTIVE; like Restart-Computer). Requires --yes."""
    if not yes:
        raise click.ClickException("refusing to restart without --yes")
    get_cli_context(ctx).services.ps.restart_computer(delay_seconds=delay_seconds, force=True)
    click.echo(f"restart scheduled in {delay_seconds}s")


@click.command("stop_computer", context_settings=CLICK_CONTEXT_SETTINGS)
@option("--delay", "delay_seconds", type=int, default=0, help="Seconds to wait before shutting down.")
@option("--yes", is_flag=True, default=False, help="Required to actually shut down (a safety guard).")
@click.pass_context
def cli_stop_computer(ctx: click.Context, delay_seconds: int, yes: bool) -> None:
    """Shut the computer down (Windows, DESTRUCTIVE; like Stop-Computer). Requires --yes."""
    if not yes:
        raise click.ClickException("refusing to shut down without --yes")
    get_cli_context(ctx).services.ps.stop_computer(delay_seconds=delay_seconds, force=True)
    click.echo(f"shutdown scheduled in {delay_seconds}s")


SYSTEM_COMMANDS = (
    cli_get_hotfix,
    cli_get_windows_update,
    cli_get_computer_info,
    cli_get_windows_image,
    cli_stop_process,
    cli_wait_process,
    cli_restart_computer,
    cli_stop_computer,
)

__all__ = ["SYSTEM_COMMANDS"]
