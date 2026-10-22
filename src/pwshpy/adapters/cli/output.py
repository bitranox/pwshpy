"""Record output for the CLI - streaming JSON/JSONL and human tables.

Keeps the lazy pipeline lazy end to end: ``--jsonl`` writes one record at a time,
so ``pwshpy processes --jsonl | head`` stops early and cheaply.  Writes are
wrapped so a downstream reader closing the pipe exits cleanly (exit 0, no
``BrokenPipeError`` traceback) on both POSIX and Windows.

``--json`` streams one array and writes nothing until the source has produced its
first record, so a source that fails up front leaves stdout empty.  An error raised
AFTER records were streamed leaves a partial, unterminated array on stdout: the
streaming trade-off, accepted because closing it would mean buffering the whole
stream.  The exit code is non-zero either way.

JSON output (``--json``, ``--jsonl``, :func:`emit_json`) is written as UTF-8 bytes below
the text layer, whatever the console code page, ``PYTHONIOENCODING`` or ``PYTHONUTF8``
say: JSON is UTF-8 by spec, and a cp1252 stdout (German Windows) turned every non-ASCII
field into mojibake for a consumer reading the pipe.  Lines end in LF on every OS.  The
human table and :func:`emit_lines` stay on the console's own encoding: they are text for
a person or a text tool, not JSON.

Contents:
    * :class:`RecordFormat` - human / json / jsonl selector.
    * :func:`emit` - render an iterable of records in the chosen format.
"""

from __future__ import annotations

import contextlib
import enum
import errno
import itertools
import os
import sys
from collections.abc import Generator, Iterable
from typing import BinaryIO, TextIO

import orjson
from rich.console import Console
from rich.table import Table

from ...domain.records import PSRecord

#: Windows error codes for a downstream reader closing the pipe: ERROR_BROKEN_PIPE (109)
#: and ERROR_NO_DATA (232, "the pipe is being closed"). On Windows these surface as a
#: plain ``OSError``, not ``BrokenPipeError``.
_WINDOWS_PIPE_CLOSED = frozenset({109, 232})

#: Soft cap on rows rendered by the human table (which cannot stream). Beyond this,
#: the table is truncated and the user is pointed at --jsonl / --limit. Streaming
#: --json / --jsonl output is unaffected and stays fully lazy.
_MAX_TABLE_ROWS = 1000


class RecordFormat(enum.Enum):
    """Output format for a stream of records.

    Example:
        >>> RecordFormat.JSONL.value
        'jsonl'
    """

    HUMAN = "human"
    JSON = "json"
    JSONL = "jsonl"


def is_pipe_closed(exc: OSError) -> bool:
    """Whether ``exc`` means a downstream reader closed the pipe (POSIX or Windows).

    POSIX raises ``BrokenPipeError`` (errno ``EPIPE``); Windows raises a plain
    ``OSError`` carrying ``winerror`` 109/232. Anything else (e.g. ``ENOSPC``) is a
    genuine write failure and must NOT be swallowed.

    Example:
        >>> is_pipe_closed(BrokenPipeError())
        True
        >>> is_pipe_closed(OSError(errno.ENOSPC, "no space"))
        False
    """
    if isinstance(exc, BrokenPipeError):
        return True
    if getattr(exc, "winerror", None) in _WINDOWS_PIPE_CLOSED:
        return True
    return exc.errno == errno.EPIPE


def _redirect_stdout_to_devnull() -> None:
    """Point the real stdout fd at the null device so the shutdown flush is silent."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())


@contextlib.contextmanager
def _clean_broken_pipe() -> Generator[None, None, None]:
    """Exit cleanly when a downstream reader closes the pipe (e.g. ``| head``).

    On a pipe-closed error (POSIX ``BrokenPipeError`` OR the Windows ``OSError``
    variant), redirect the real stdout fd to the null device so the interpreter's
    shutdown flush does not re-raise, then return normally (exit 0). Genuine write
    errors (disk full, etc.) propagate unchanged.
    """
    try:
        yield
    except OSError as exc:
        if not is_pipe_closed(exc):
            raise
        _redirect_stdout_to_devnull()


class _Utf8Stdout:
    """UTF-8 byte writer for stdout that bypasses the text layer's encoding.

    Writes go to ``sys.stdout.buffer`` so the bytes are UTF-8 whatever encoding the
    text layer was opened with. Whatever the text layer still holds is flushed first,
    so earlier text output stays ahead of the JSON. A stdout with no ``buffer`` (a
    ``StringIO`` stand-in, doctest's capture) has no encoding step to bypass, so the
    UTF-8 is decoded and written as text there.

    Example:
        >>> out = _Utf8Stdout()
        >>> out.write(b"ok\\n"); out.flush()
        ok
    """

    def __init__(self) -> None:
        self._text: TextIO = sys.stdout
        self._binary: BinaryIO | None = getattr(self._text, "buffer", None)
        if self._binary is not None:
            self._text.flush()

    def write(self, data: bytes) -> None:
        """Write UTF-8 ``data`` to stdout."""
        if self._binary is None:
            self._text.write(data.decode("utf-8"))
        else:
            self._binary.write(data)

    def flush(self) -> None:
        """Flush the stream the bytes went to."""
        if self._binary is None:
            self._text.flush()
        else:
            self._binary.flush()


def _dumps(record: PSRecord) -> bytes:
    return orjson.dumps(record.to_dict())


def _write_jsonl(records: Iterable[PSRecord]) -> None:
    out = _Utf8Stdout()
    for record in records:
        out.write(_dumps(record) + b"\n")
        out.flush()


def _write_json_array(records: Iterable[PSRecord]) -> None:
    # Pull the first record BEFORE writing "[": a source that fails before yielding
    # anything (missing path, refused native call) must leave stdout empty, so a
    # caller piping into a JSON parser sees only the stderr error and exit code. An
    # error after records were written still leaves a partial array; buffering the
    # whole stream to avoid that would end --json's lazy, memory-bounded streaming.
    iterator = iter(records)
    first = next(iterator, None)
    out = _Utf8Stdout()
    if first is None:
        out.write(b"[]\n")
        out.flush()
        return
    out.write(b"[" + _dumps(first))
    for record in iterator:
        out.write(b"," + _dumps(record))
    out.write(b"]\n")
    out.flush()


def _cell(value: object) -> str:
    """Render one record field as a table cell (``None`` -> empty string)."""
    return "" if value is None else str(value)


def _render_table(records: Iterable[PSRecord]) -> None:
    # The human table must see every row it renders to size columns, so it cannot
    # stay lazy like --jsonl. Bound memory with a soft cap: render the first
    # _MAX_TABLE_ROWS and, if more remain, point the user at the streaming path.
    iterator = iter(records)
    materialized = [record.to_dict() for record in itertools.islice(iterator, _MAX_TABLE_ROWS)]
    truncated = next(iterator, None) is not None
    console = Console()
    if not materialized:
        console.print("[dim]No records.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    for column in materialized[0]:
        table.add_column(column)
    for row in materialized:
        table.add_row(*(_cell(value) for value in row.values()))
    console.print(table)
    if truncated:
        console.print(
            f"[dim]... showing the first {_MAX_TABLE_ROWS} rows; use --jsonl for the full stream, or --limit N.[/dim]"
        )


def emit_json(data: object) -> None:
    """Render arbitrary (non-record) data as one JSON value, broken-pipe safe.

    For .NET / web output that is not a typed :class:`PSRecord` (marshaled PowerShell
    objects, parsed REST JSON). ``default=str`` keeps it total - a ``SecretStr`` renders
    masked, a ``datetime`` as ISO text - so it never raises on an exotic value.

    Example:
        >>> emit_json({"ok": True, "n": 1})
        {"ok":true,"n":1}
    """
    with _clean_broken_pipe():
        out = _Utf8Stdout()
        out.write(orjson.dumps(data, default=str) + b"\n")
        out.flush()


def emit_lines(lines: Iterable[str]) -> None:
    """Stream raw text lines to stdout, broken-pipe safe and memory-bounded.

    For streaming a file's content (``get_content_lines``) - one line at a time, so a huge
    file never materializes and a downstream ``| head`` closes cleanly.

    Example:
        >>> emit_lines(["a\\n", "b\\n"])
        a
        b
    """
    with _clean_broken_pipe():
        out = sys.stdout
        for line in lines:
            out.write(line)
        out.flush()


def emit(records: Iterable[PSRecord], fmt: RecordFormat) -> None:
    """Render ``records`` to stdout in the chosen format, safe against broken pipes.

    Example:
        >>> from pwshpy.domain.records import ProcessInfo
        >>> emit([ProcessInfo(pid=1, name="init")], RecordFormat.JSONL)
        {"pid":1,"ppid":null,"name":"init","status":"unknown","username":null,"memory_rss":null,"create_time":null}
    """
    with _clean_broken_pipe():
        if fmt is RecordFormat.JSONL:
            _write_jsonl(records)
        elif fmt is RecordFormat.JSON:
            _write_json_array(records)
        else:
            _render_table(records)


__all__ = ["RecordFormat", "emit", "emit_json", "emit_lines", "is_pipe_closed"]
