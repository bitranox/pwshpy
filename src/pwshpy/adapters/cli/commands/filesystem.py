"""``pwshpy`` filesystem commands - typed file/directory operations (native, portable).

Read: ``get_child_item`` / ``get_item`` (records), ``get_content`` (text), ``test_path``
(exit 0 if the path exists, 1 if not). Mutating: ``new_item`` / ``copy_item`` /
``move_item`` / ``remove_item``. Thin front ends over the ``ps`` facade
(``pathlib``/``shutil``), mirroring the PowerShell filesystem-provider cmdlets.

Contents:
    * the ``cli_*`` filesystem commands (read + mutating), grouped below.
"""

from __future__ import annotations

import rich_click as click

from ....domain.enums import FileItemType
from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit, emit_lines
from ..typed_click import argument, option
from ._common import resolve_format


@click.command("get_child_item", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@option("--recurse", "-r", is_flag=True, default=False, help="Walk the directory tree.")
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_get_child_item(ctx: click.Context, path: str, recurse: bool, as_json: bool, as_jsonl: bool) -> None:
    """List a directory's entries as typed records (native; like Get-ChildItem).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_get_child_item, ["--help"]).exit_code
        0
    """
    ps = get_cli_context(ctx).services.ps
    emit(ps.get_child_item(path, recurse=recurse), resolve_format(as_json, as_jsonl))


@click.command("get_item", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@option("--json", "as_json", is_flag=True, default=False, help="Emit the record as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Emit the record as one JSON line.")
@click.pass_context
def cli_get_item(ctx: click.Context, path: str, as_json: bool, as_jsonl: bool) -> None:
    """Show one file/directory as a typed record (native; like Get-Item)."""
    ps = get_cli_context(ctx).services.ps
    emit([ps.get_item(path)], resolve_format(as_json, as_jsonl))


@click.command("get_content", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@option("--encoding", default="utf-8", help="Text encoding (default utf-8).")
@click.pass_context
def cli_get_content(ctx: click.Context, path: str, encoding: str) -> None:
    """Print a file's text content, streamed line-by-line and memory-bounded (native; like Get-Content)."""
    ps = get_cli_context(ctx).services.ps
    emit_lines(ps.get_content_lines(path, encoding=encoding))


@click.command("test_path", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@option("--quiet", "-q", is_flag=True, default=False, help="Suppress output; use the exit code only.")
@click.pass_context
def cli_test_path(ctx: click.Context, path: str, quiet: bool) -> None:
    """Report whether PATH exists; exit 0 if it does, 1 if not (native; like Test-Path)."""
    exists = get_cli_context(ctx).services.ps.test_path(path)
    if not quiet:
        click.echo("True" if exists else "False")
    ctx.exit(0 if exists else 1)


@click.command("new_item", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@option(
    "--type",
    "item_type",
    type=click.Choice([kind.value for kind in FileItemType]),
    default=FileItemType.FILE.value,
    help="What to create.",
)
@click.pass_context
def cli_new_item(ctx: click.Context, path: str, item_type: str) -> None:
    """Create a file or directory (mutating; like New-Item)."""
    kind = FileItemType(item_type)  # string -> Enum at the CLI boundary
    get_cli_context(ctx).services.ps.new_item(path, item_type=kind)
    click.echo(f"created {kind.value}: {path}")


@click.command("copy_item", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("source")
@argument("destination")
@option("--recurse", "-r", is_flag=True, default=False, help="Copy a directory tree.")
@click.pass_context
def cli_copy_item(ctx: click.Context, source: str, destination: str, recurse: bool) -> None:
    """Copy a file or directory tree (mutating; like Copy-Item)."""
    get_cli_context(ctx).services.ps.copy_item(source, destination, recurse=recurse)
    click.echo(f"copied {source} -> {destination}")


@click.command("move_item", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("source")
@argument("destination")
@click.pass_context
def cli_move_item(ctx: click.Context, source: str, destination: str) -> None:
    """Move or rename a file/directory (mutating; like Move-Item)."""
    get_cli_context(ctx).services.ps.move_item(source, destination)
    click.echo(f"moved {source} -> {destination}")


@click.command("remove_item", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@option("--recurse", "-r", is_flag=True, default=False, help="Remove a directory's contents too.")
@click.pass_context
def cli_remove_item(ctx: click.Context, path: str, recurse: bool) -> None:
    """Delete a file or directory (mutating; like Remove-Item)."""
    get_cli_context(ctx).services.ps.remove_item(path, recurse=recurse)
    click.echo(f"removed {path}")


FILESYSTEM_COMMANDS = (
    cli_get_child_item,
    cli_get_item,
    cli_get_content,
    cli_test_path,
    cli_new_item,
    cli_copy_item,
    cli_move_item,
    cli_remove_item,
)

__all__ = ["FILESYSTEM_COMMANDS"]
