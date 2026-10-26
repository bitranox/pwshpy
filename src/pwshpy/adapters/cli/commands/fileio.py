"""``pwshpy`` file-output command - write stdin to a file with a predictable encoding.

``write_text`` reads stdin and writes it as UTF-8 with **no BOM** and LF newlines by
default - the same bytes on every OS, unlike a shell ``>`` redirect or PowerShell's
``Out-File``. A piped stdin is read as UTF-8 on every OS, whatever the console code page:
pwsh 7 pipes UTF-8 into a native command, and so does pwshpy itself. ``--input-encoding``
names another source encoding for a piped stdin; a terminal stdin keeps its own.

Contents:
    * :func:`cli_write_text`, exported as ``FILEIO_COMMANDS``.
"""

from __future__ import annotations

import io
import sys

import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import is_terminal
from ..typed_click import argument, option

#: Read stdin this many characters at a time - big enough to be cheap, small enough to stay bounded.
_STDIN_BLOCK = 65536


def _stdin_encoding(input_encoding: str | None) -> str | None:
    """The encoding to read a piped stdin with, or ``None`` to keep the text layer it already has.

    A piped stdin defaults to UTF-8 rather than the locale code page: on German Windows that is
    cp1252, so UTF-8 from pwsh 7 or from pwshpy itself landed as mojibake, and a byte cp1252 does
    not map (0x8D in the UTF-8 for an I-acute) failed the command (user decision 2026-10-26). The
    default is ``utf-8-sig``, which reads BOM-less UTF-8 the same and drops a leading BOM (Notepad,
    PowerShell 5.1 ``Out-File``) that would otherwise land in a file promised to have none. A
    terminal keeps its own encoding even with ``--input-encoding``: a Windows console hands its text
    layer UTF-8 whatever the option says, so honouring it there would decode the typing wrongly.

    Args:
        input_encoding: The ``--input-encoding`` value, or ``None`` when it was not given.

    Returns:
        The codec name to read stdin with, or ``None`` for a terminal.

    Raises:
        click.BadParameter: ``input_encoding`` names no text encoding (unknown, or a bytes-to-bytes
            codec such as ``base64``).

    Example:
        >>> _stdin_encoding("no-such-codec")
        Traceback (most recent call last):
        click.exceptions.BadParameter: unknown text encoding 'no-such-codec'
    """
    if input_encoding is not None:
        _require_text_encoding(input_encoding)
    if is_terminal(sys.stdin):
        return None
    return input_encoding or "utf-8-sig"


def _require_text_encoding(name: str) -> None:
    """Refuse ``name`` unless a text stream can use it (``codecs.lookup`` also accepts ``base64``)."""
    try:
        io.TextIOWrapper(io.BytesIO(), encoding=name)
    except LookupError:
        raise click.BadParameter(f"unknown text encoding {name!r}", param_hint="'--input-encoding'") from None


def _read_stdin_as(encoding: str | None) -> None:
    """Switch ``sys.stdin`` to ``encoding`` (strict) before its first read; ``None`` leaves it as it is.

    A stdin that is missing or not a ``TextIOWrapper`` has no text layer to switch.
    """
    stream = sys.stdin
    if encoding is None or not isinstance(stream, io.TextIOWrapper):
        return
    stream.reconfigure(encoding=encoding, errors="strict")


@click.command("write_text", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@option("--encoding", default="utf-8", help="Text encoding of the file written (default utf-8).")
@option(
    "--input-encoding",
    default=None,
    help="Encoding of a piped stdin (default utf-8; e.g. cp1252 for a legacy program's output).",
)
@option("--bom", is_flag=True, default=False, help="Write a byte-order mark (default: none).")
@option("--crlf", is_flag=True, default=False, help="Use CRLF line endings instead of LF.")
@click.pass_context
def cli_write_text(
    ctx: click.Context, path: str, encoding: str, input_encoding: str | None, bom: bool, crlf: bool
) -> None:
    """Read stdin and write it to PATH with a predictable encoding (UTF-8, no BOM by default).

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_write_text, ["--help"]).exit_code
        0
    """
    source_encoding = _stdin_encoding(input_encoding)
    _read_stdin_as(source_encoding)
    # Stream stdin in fixed-size blocks so a huge pipe stays memory-bounded (never sys.stdin.read()).
    blocks = iter(lambda: sys.stdin.read(_STDIN_BLOCK), "")
    try:
        written = get_cli_context(ctx).services.ps.write_text_stream(
            path, blocks, encoding=encoding, newline="\r\n" if crlf else "\n", bom=bom
        )
    except UnicodeDecodeError:
        raise click.ClickException(
            f"stdin is not valid {source_encoding or sys.stdin.encoding}; name the encoding of the source "
            f"with --input-encoding (for example --input-encoding cp1252). {path} holds part of the text before it."
        ) from None
    click.echo(f"wrote {written}")


FILEIO_COMMANDS = (cli_write_text,)

__all__ = ["FILEIO_COMMANDS"]
