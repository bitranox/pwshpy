"""``pwshpy`` packer commands - ship a Python script as a single self-extracting ``.ps1``.

``pack`` embeds an entry script and the local modules it imports into a PowerShell runner
that unpacks itself, provisions ``uv`` when the target machine has none, runs the script and
returns its exit code.  ``unpack`` restores those sources so a pack can be edited and packed
again.

Contents:
    * :func:`cli_pack`, :func:`cli_unpack`, exported as ``PACK_COMMANDS``.
"""

from __future__ import annotations

import rich_click as click

from ....domain.packing import PackOptions
from ....domain.records import PackedScript
from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit
from ..typed_click import argument, option
from ._common import resolve_format


@click.command("pack", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("entry")
@option("--out", "-o", "dest", default=None, help="Output .ps1 path (default: the entry with a .ps1 suffix).")
@option("--include", "-i", multiple=True, help="Extra file to embed; repeatable (data files, dynamic imports).")
@option("--with", "-w", "with_packages", multiple=True, help="Extra dependency for uv run; repeatable.")
@option("--root", default=None, help="Directory local imports resolve against (default: the entry's parent).")
@option("--force", "-f", is_flag=True, default=False, help="Overwrite the output if it already exists.")
@option("--json", "as_json", is_flag=True, default=False, help="Emit the manifest as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Emit the manifest as one JSON line.")
@click.pass_context
def cli_pack(
    ctx: click.Context,
    *,
    entry: str,
    dest: str | None,
    include: tuple[str, ...],
    with_packages: tuple[str, ...],
    root: str | None,
    force: bool,
    as_json: bool,
    as_jsonl: bool,
) -> None:
    """Pack ENTRY and its local modules into a self-extracting PowerShell script.

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_pack, ["--help"]).exit_code
        0
    """
    options = PackOptions(include=list(include), with_packages=list(with_packages), root=root, force=force)
    manifest = get_cli_context(ctx).services.ps.pack_script(entry, dest, options=options)
    _warn_on_undeclared(manifest)
    emit([manifest], resolve_format(as_json, as_jsonl))


@click.command("unpack", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("source")
@option("--out", "-o", "dest", default=".", help="Directory to restore the sources into (default: cwd).")
@option("--force", "-f", is_flag=True, default=False, help="Overwrite files that already exist.")
@option("--json", "as_json", is_flag=True, default=False, help="Emit the manifest as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Emit the manifest as one JSON line.")
@click.pass_context
def cli_unpack(ctx: click.Context, *, source: str, dest: str, force: bool, as_json: bool, as_jsonl: bool) -> None:
    """Restore the Python sources embedded in a packed .ps1 (the inverse of ``pack``).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_unpack, ["--help"]).exit_code
        0
    """
    manifest = get_cli_context(ctx).services.ps.unpack_script(source, dest, force=force)
    emit([manifest], resolve_format(as_json, as_jsonl))


def _warn_on_undeclared(manifest: PackedScript) -> None:
    """Warn when a script imports third-party modules but declares no dependencies at all.

    Deliberately coarse: it fires only when nothing is declared anywhere - no PEP 723 block
    and no ``--with`` - because checking name-by-name would mean mapping an import name to a
    distribution name, and that mapping is exactly the guess this feature refuses to make
    (``yaml`` is PyYAML, ``cv2`` is opencv-python).  A declared script is never nagged.
    """
    if not manifest.external_imports or manifest.has_script_metadata or manifest.uv_args:
        return
    click.echo(
        f"warning: {manifest.entry} imports {', '.join(manifest.external_imports)} but declares no "
        "dependencies. Add a PEP 723 block to it (or pass --with) or the pack will fail on the target machine.",
        err=True,
    )


PACK_COMMANDS = (cli_pack, cli_unpack)

__all__ = ["PACK_COMMANDS"]
