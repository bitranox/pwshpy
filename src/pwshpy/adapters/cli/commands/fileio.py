"""``pwshpy`` file-output command - write stdin to a file with a predictable encoding.

``write_text`` reads stdin and writes it as UTF-8 with **no BOM** and LF newlines by
default - the same bytes on every OS, unlike a shell ``>`` redirect or PowerShell's
``Out-File``.

Contents:
    * :func:`cli_write_text`, exported as ``FILEIO_COMMANDS``.
"""

from __future__ import annotations

import sys

import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..typed_click import argument, option

#: Read stdin this many characters at a time - big enough to be cheap, small enough to stay bounded.
_STDIN_BLOCK = 65536


@click.command("write_text", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@option("--encoding", default="utf-8", help="Text encoding (default utf-8).")
@option("--bom", is_flag=True, default=False, help="Write a byte-order mark (default: none).")
@option("--crlf", is_flag=True, default=False, help="Use CRLF line endings instead of LF.")
@click.pass_context
def cli_write_text(ctx: click.Context, path: str, encoding: str, bom: bool, crlf: bool) -> None:
    """Read stdin and write it to PATH with a predictable encoding (UTF-8, no BOM by default).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_write_text, ["--help"]).exit_code
        0
    """
    # Stream stdin in fixed-size blocks so a huge pipe stays memory-bounded (never sys.stdin.read()).
    blocks = iter(lambda: sys.stdin.read(_STDIN_BLOCK), "")
    written = get_cli_context(ctx).services.ps.write_text_stream(
        path, blocks, encoding=encoding, newline="\r\n" if crlf else "\n", bom=bom
    )
    click.echo(f"wrote {written}")


FILEIO_COMMANDS = (cli_write_text,)

__all__ = ["FILEIO_COMMANDS"]
