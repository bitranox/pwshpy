"""Record output for the CLI — streaming JSON/JSONL and human tables.

Keeps the lazy pipeline lazy end to end: ``--jsonl`` writes one record at a time,
so ``pwshpy processes --jsonl | head`` stops early and cheaply.  Writes are
wrapped so a downstream reader closing the pipe exits cleanly (exit 0, no
``BrokenPipeError`` traceback) on both POSIX and Windows.

Contents:
    * :class:`RecordFormat` — human / json / jsonl selector.
    * :func:`emit` — render an iterable of records in the chosen format.
"""

from __future__ import annotations

import contextlib
import enum
import errno
import itertools
import os
import sys
from collections.abc import Generator, Iterable

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


def _dumps(record: PSRecord) -> str:
    return orjson.dumps(record.to_dict()).decode("utf-8")


def _write_jsonl(records: Iterable[PSRecord]) -> None:
    out = sys.stdout
    for record in records:
        out.write(_dumps(record))
        out.write("\n")
        out.flush()


def _write_json_array(records: Iterable[PSRecord]) -> None:
    out = sys.stdout
    out.write("[")
    for index, record in enumerate(records):
        if index:
            out.write(",")
        out.write(_dumps(record))
    out.write("]\n")
    out.flush()


def _cell(value: object) -> str:
    """Render one record field as a table cell (``None`` → empty string)."""
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


__all__ = ["RecordFormat", "emit", "is_pipe_closed"]
