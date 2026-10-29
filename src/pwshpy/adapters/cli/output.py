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
field into mojibake for a consumer reading the pipe.  Lines end in LF on every OS.  A record
field (in JSON or in a human-table cell) or JSON value holding a byte that is not valid UTF-8 is
written with U+FFFD for that byte, and one ``warning:`` line on stderr names the record and field;
the stream goes on.  Text
passed through from elsewhere (:func:`emit_text`: a child's output, a web response, a status
line naming a path; :func:`emit_lines`: a file's lines) is written the same way when its stream
is not a terminal, because a character outside cp1252 raised ``UnicodeEncodeError`` there after
the work was done; at a terminal it goes through the text layer as before.  A missing stream
(``None``, as under pythonw or a detached service) drops the output, as ``click.echo`` does.
Everything else (the human table, status and error lines, ``config``'s human view) goes through
the text layer, which :func:`use_utf8_when_piped` switches to UTF-8 with LF line ends for a piped
stream when the process starts.

The human table fits a terminal's width. Piped or redirected it is sized to its content up to
``_MAX_PIPED_WIDTH`` columns, so no cell is cut: every value is whole, a value without line
breaks stays on one line, and content wider than the ceiling folds onto further lines rather than
losing characters. An explicit ``COLUMNS`` still sets the width either way. Every cell prints its
value literally: a value that looks like rich markup or an emoji code is shown as it is.

Contents:
    * :class:`RecordFormat` - human / json / jsonl selector.
    * :func:`emit` - render an iterable of records in the chosen format.
    * :func:`emit_text` - pass a text body through, UTF-8 when piped.
    * :func:`emit_lines` - stream text lines, UTF-8 when piped.
    * :func:`use_utf8_when_piped` - switch a piped stdout/stderr to UTF-8 with LF, at process start.
"""

from __future__ import annotations

import contextlib
import enum
import errno
import io
import itertools
import os
import sys
from collections.abc import Generator, Iterable, Mapping
from typing import Any, BinaryIO, TextIO, cast

import rich_click as click
from pydantic import BaseModel
from rich import box
from rich.console import Console
from rich.measure import Measurement
from rich.table import Table
from rich.text import Text

from ...domain.records import PSRecord
from ..native.record_json import (
    describe_output_replacements,
    describe_replacements,
    json_bytes,
    record_json_bytes,
    repair_surrogates,
)

#: Windows error codes for a downstream reader closing the pipe: ERROR_BROKEN_PIPE (109)
#: and ERROR_NO_DATA (232, "the pipe is being closed"). On Windows these surface as a
#: plain ``OSError``, not ``BrokenPipeError``.
_WINDOWS_PIPE_CLOSED = frozenset({109, 232})

#: Soft cap on rows rendered by the human table (which cannot stream). Beyond this,
#: the table is truncated and the user is pointed at --jsonl / --limit. Streaming
#: --json / --jsonl output is unaffected and stays fully lazy.
_MAX_TABLE_ROWS = 1000

#: Ceiling on the width of a piped (non-terminal) human table. rich pads every row to the widest
#: cell, so without it one 128 KB value was copied into all 1000 rows (132 MB of output). 8192 keeps
#: a POSIX PATH_MAX (4096) value plus its row's other columns on one line; wider content folds.
_MAX_PIPED_WIDTH = 8192


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


class _Utf8Writer:
    """UTF-8 byte writer for a text stream that bypasses the stream's encoding.

    Writes go to the stream's ``buffer`` so the bytes are UTF-8 whatever encoding the
    text layer was opened with. Whatever the text layer still holds is flushed first,
    so earlier text output stays ahead of the bytes. A stream with no ``buffer`` (a
    ``StringIO`` stand-in, doctest's capture) has no encoding step to bypass, so the
    UTF-8 is decoded and written as text there. A ``None`` stream (``sys.stderr`` or
    ``sys.stdout`` under pythonw or a detached service) drops every write, as
    ``click.echo`` does; the stream is required rather than defaulted so a missing
    stderr can never fall back to stdout and mix a child's errors into its output.

    Args:
        stream: The text stream to write below, or ``None`` when there is none.

    Example:
        >>> out = _Utf8Writer(sys.stdout)
        >>> out.write(b"ok\\n"); out.flush()
        ok
    """

    def __init__(self, stream: TextIO | None) -> None:
        self._text = stream
        self._binary: BinaryIO | None = None if stream is None else getattr(stream, "buffer", None)
        if stream is not None and self._binary is not None:
            stream.flush()

    def write(self, data: bytes) -> None:
        """Write UTF-8 ``data`` to the stream (nothing when there is no stream)."""
        if self._binary is not None:
            self._binary.write(data)
        elif self._text is not None:
            self._text.write(data.decode("utf-8"))

    def flush(self) -> None:
        """Flush the stream the bytes went to (nothing when there is no stream)."""
        if self._binary is not None:
            self._binary.flush()
        elif self._text is not None:
            self._text.flush()


def _dumps(record: PSRecord, position: int) -> bytes:
    # record_json_bytes, not record.model_dump_json(): its bytes match for every ordinary record,
    # but it writes an integer outside orjson's 64-bit range where this raises, and refuses a lone
    # surrogate where this writes U+FFFD. tests/test_record_json_bytes.py pins each case. The
    # warning goes to stderr so the JSON on stdout stays parseable and the stream goes on.
    result = record_json_bytes(record)
    if result.replaced:
        emit_text(f"warning: {describe_replacements(record, position, result.replaced)}\n", err=True)
    return result.data


def _write_jsonl(records: Iterable[PSRecord]) -> None:
    out = _Utf8Writer(sys.stdout)
    for position, record in enumerate(records, start=1):
        out.write(_dumps(record, position) + b"\n")
        out.flush()


def _write_json_array(records: Iterable[PSRecord]) -> None:
    # Pull the first record BEFORE writing "[": a source that fails before yielding
    # anything (missing path, refused native call) must leave stdout empty, so a
    # caller piping into a JSON parser sees only the stderr error and exit code. An
    # error after records were written still leaves a partial array; buffering the
    # whole stream to avoid that would end --json's lazy, memory-bounded streaming.
    iterator = iter(records)
    first = next(iterator, None)
    out = _Utf8Writer(sys.stdout)
    if first is None:
        out.write(b"[]\n")
        out.flush()
        return
    out.write(b"[" + _dumps(first, 1))
    for position, record in enumerate(iterator, start=2):
        out.write(b"," + _dumps(record, position))
    out.write(b"]\n")
    out.flush()


def _cell(value: object) -> Text:
    """Render one record field as a literal table cell (``None`` -> empty text).

    A ``str`` cell is parsed by rich as console markup and emoji codes, so a record value was
    treated as instructions: ``a[/]b`` raised MarkupError and failed the command, ``[bold]x[/bold]``
    lost its tags, ``:smile:`` became an emoji and ``C:\\[x]`` lost its backslash. A ``Text`` is
    printed exactly as given, and the table measures exactly the characters it prints, so the
    piped-width sizing stays right. ``rich.markup.escape()`` was rejected: it covers markup only,
    so emoji codes would still be replaced.

    Example:
        >>> _cell("a[/]b").plain
        'a[/]b'
        >>> _cell(None).plain
        ''
    """
    return Text("" if value is None else str(value))


def _table_row(record: PSRecord, position: int) -> dict[str, Any]:
    """The record's JSON-mode fields as table cells, repaired and with nested records compacted.

    Text holding a byte that is not valid UTF-8 (a lone surrogate) is written with U+FFFD for each
    such byte and one ``warning:`` line on stderr names the record and field, never the value: the
    same rule and line as ``--json`` (the raw byte reached a redirected table through stdout's
    ``surrogateescape`` handler, so the file was not valid UTF-8).

    A nested model (``ScheduledTaskInfo.settings``) would otherwise print as a dict repr, braces,
    quotes and every unset ``None`` included, in one wide cell. Only its set fields are kept, in
    their JSON form, so the cell reads like the ``--json`` object it stands for. A list of models or
    mappings (``WindowsUpdate.category_details``) gets the same pairs per item, items joined by
    ``"; "``. ``--json`` and ``--jsonl`` dump the record themselves and are unaffected. A plain
    mapping field (``CimInstance.properties``) and any other list keep their rendering.

    Args:
        record: The record to render.
        position: Its 1-based row number, named in the warning.

    Returns:
        Field name to cell value, in field order.

    Example:
        >>> from pwshpy.domain.enums import TaskState
        >>> from pwshpy.domain.records import ScheduledTaskInfo
        >>> from pwshpy.domain.scheduling import TaskSettings
        >>> task = ScheduledTaskInfo(task_name="T", task_path="Root", state=TaskState.READY,
        ...                          settings=TaskSettings(allow_hard_terminate=False))
        >>> _table_row(task, 1)["settings"]
        'allow_hard_terminate=False'
    """
    repaired = repair_surrogates(record.to_dict())
    if repaired.replaced:
        emit_text(f"warning: {describe_replacements(record, position, repaired.replaced)}\n", err=True)
    row = cast("dict[str, Any]", repaired.value)
    return {name: _compact(value, nested=isinstance(getattr(record, name), BaseModel)) for name, value in row.items()}


def _compact(value: object, *, nested: bool) -> object:
    """A nested model's JSON object, or a list of JSON objects, as ``k=v`` pairs; anything else as is."""
    if nested and isinstance(value, Mapping):
        return _pairs(cast("Mapping[object, object]", value))
    mappings = _mapping_items(value)
    if mappings:
        return "; ".join(_pairs(mapping) for mapping in mappings)
    return value


def _mapping_items(value: object) -> list[Mapping[object, object]] | None:
    """The items of a non-empty list whose every item is a mapping; None for anything else."""
    if not isinstance(value, list):
        return None
    items = cast("list[object]", value)
    if not items or not all(isinstance(item, Mapping) for item in items):
        return None
    return cast("list[Mapping[object, object]]", items)


def _pairs(mapping: Mapping[object, object]) -> str:
    """``key=value, key=value`` of the entries that are not ``None``."""
    return ", ".join(f"{key}={value}" for key, value in mapping.items() if value is not None)


def is_terminal(stream: TextIO | None) -> bool:
    """Whether ``stream`` is a real terminal (a closed, missing or odd stream counts as not one).

    Asks the stream itself rather than ``Console.is_terminal``: rich reports a terminal whenever
    FORCE_COLOR is set, even into a pipe, and still has no terminal width to read there.

    Args:
        stream: The stream to ask, usually ``sys.stdout`` or ``sys.stderr``; ``None`` is no terminal.

    Returns:
        True only when the stream answers ``isatty()`` with a true value.

    Example:
        >>> is_terminal(sys.stdout)  # doctest captures stdout, so it is no terminal here
        False
    """
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _columns_is_explicit() -> bool:
    """Whether COLUMNS names a width rich will use (the same digits-only test rich applies).

    Example:
        >>> import os
        >>> _columns_is_explicit() == os.environ.get("COLUMNS", "").isdigit()
        True
    """
    return os.environ.get("COLUMNS", "").isdigit()


def _table_console(table: Table) -> Console:
    """Build the console that prints ``table``: as wide as its content (up to a ceiling) when piped.

    Off a terminal rich has no width to read and falls back to 80 columns, so every wider cell was
    cut with an ellipsis and ``pwshpy env | grep`` found half a value. Sizing the console to the
    table's natural width keeps each value whole, and a value without line breaks on one line;
    folding every long value was rejected because it splits a SID or a path across lines, which a
    grep cannot match. The width stops at ``_MAX_PIPED_WIDTH`` because rich pads every row to the
    widest cell: one huge value would otherwise be copied into all rows. Content wider than that
    folds: every column of ``table`` is set to fold, because rich narrows the widest column to fit
    and a shorter neighbour in it would otherwise get an ellipsis. A terminal keeps its own width,
    and an explicit COLUMNS still wins, so neither a person at a terminal nor a user who chose a
    width sees a change. The floor is the default width, so a narrow table never wraps the
    truncation hint printed after it.

    Example:
        >>> table = Table("name")
        >>> table.add_row("x" * 200)
        >>> _columns_is_explicit() or _table_console(table).width >= 200
        True
    """
    console = Console()
    if is_terminal(sys.stdout) or _columns_is_explicit():
        return console
    # Measure without a ceiling: rich clamps a measurement to the width it is offered, and the
    # point is to learn the width the content needs rather than the one rich guessed.
    natural = Measurement.get(console, console.options.update_width(sys.maxsize), table).maximum
    if natural > _MAX_PIPED_WIDTH:
        for column in table.columns:
            column.overflow = "fold"
    return Console(width=max(console.width, min(natural, _MAX_PIPED_WIDTH)))


def _render_table(records: Iterable[PSRecord]) -> None:
    # The human table must see every row it renders to size columns, so it cannot
    # stay lazy like --jsonl. Bound memory with a soft cap: render the first
    # _MAX_TABLE_ROWS and, if more remain, point the user at the streaming path.
    iterator = iter(records)
    materialized = [
        _table_row(record, position)
        for position, record in enumerate(itertools.islice(iterator, _MAX_TABLE_ROWS), start=1)
    ]
    truncated = next(iterator, None) is not None
    if not materialized:
        Console().print("[dim]No records.[/dim]")
        return
    # Piped, the frame is ASCII on every OS: rich's own boxes are outside every legacy code page and
    # differ by OS (heavy on Linux, square for a legacy Windows console), so a reader decoding the
    # pipe as cp850/cp1252 saw a garbled frame (user decision 2026-10-26). A terminal keeps its box.
    frame = box.HEAVY_HEAD if is_terminal(sys.stdout) else box.ASCII
    table = Table(show_header=True, header_style="bold", box=frame)
    for column in materialized[0]:
        table.add_column(column)
    for row in materialized:
        table.add_row(*(_cell(value) for value in row.values()))
    console = _table_console(table)
    console.print(table)
    if truncated:
        console.print(
            f"[dim]... showing the first {_MAX_TABLE_ROWS} rows; use --jsonl for the full stream, or --limit N.[/dim]"
        )


def emit_json(data: object, *, indent: bool = False) -> None:
    """Render arbitrary (non-record) data as one JSON value, broken-pipe safe.

    For .NET / web output that is not a typed :class:`PSRecord` (marshaled PowerShell
    objects, parsed REST JSON) and the merged configuration (``config --format json``).
    ``default=str`` keeps it total - a ``SecretStr`` renders masked, a ``datetime`` as ISO
    text - so it never raises on an exotic value. Text holding a byte that is not valid UTF-8
    is written with U+FFFD for that byte, and one ``warning: output`` line on stderr names
    where (never the value).

    Args:
        data: The value to write.
        indent: Indent by two spaces (``config --format json``); compact otherwise.

    Example:
        >>> emit_json({"ok": True, "n": 1})
        {"ok":true,"n":1}
    """
    with _clean_broken_pipe():
        result = json_bytes(data, default=str, indent=indent)
        if result.replaced:
            emit_text(f"warning: {describe_output_replacements(result.replaced)}\n", err=True)
        out = _Utf8Writer(sys.stdout)
        out.write(result.data + b"\n")
        out.flush()


def emit_lines(lines: Iterable[str]) -> None:
    """Stream raw text lines to stdout, broken-pipe safe and memory-bounded.

    For streaming a file's content (``get_content_lines``) - one line at a time, so a huge
    file never materializes and a downstream ``| head`` closes cleanly. Piped or redirected
    the lines are written as UTF-8 bytes, like :func:`emit_text` (a line holding a character
    outside a cp1252 console's code page raised ``UnicodeEncodeError`` mid-file); at a
    terminal they go through the text layer as before.

    Args:
        lines: The lines to write, each with its own line end.

    Example:
        >>> emit_lines(["a\\n", "b\\n"])
        a
        b
    """
    with _clean_broken_pipe():
        stream = sys.stdout
        if is_terminal(stream):
            _write_lines_as_text(lines, stream)
        else:
            _write_lines_as_utf8(lines, _Utf8Writer(stream))


def _write_lines_as_text(lines: Iterable[str], stream: TextIO) -> None:
    for line in lines:
        stream.write(line)
    stream.flush()


def _write_lines_as_utf8(lines: Iterable[str], out: _Utf8Writer) -> None:
    for line in lines:
        out.write(line.encode("utf-8"))
    out.flush()


def emit_text(text: str, *, err: bool = False) -> None:
    """Pass a text body through to stdout (or stderr): UTF-8 bytes when the stream is piped.

    For text that came from elsewhere - an external program's output, an HTTP response body.
    Piped or redirected, the body is written as UTF-8 bytes below the text layer, like JSON:
    on a cp1252 stdout (German Windows) a character outside cp1252 raised ``UnicodeEncodeError``
    after the child had already run, and stderr turned it into backslash escapes. The bytes are
    exactly the text's own, so line ends are not translated and no ANSI sequence is stripped. At a
    terminal the body goes through ``click.echo`` as before, which a Windows console already
    renders as Unicode. Stdout is broken-pipe safe (``| head`` exits cleanly); stderr is written
    without that guard, because the guard redirects stdout. A missing (``None``) stream drops
    the body; a missing stderr is never replaced by stdout.

    Args:
        text: The body to write, verbatim (no newline is added).
        err: Write to stderr instead of stdout.

    Example:
        >>> emit_text("a\\nb\\n")
        a
        b
    """
    stream = sys.stderr if err else sys.stdout
    if err:
        _write_text_body(text, stream, err=True)
        return
    with _clean_broken_pipe():
        _write_text_body(text, stream, err=False)


def _write_text_body(text: str, stream: TextIO | None, *, err: bool) -> None:
    """Write ``text`` to ``stream``: via ``click.echo`` at a terminal, as UTF-8 bytes otherwise."""
    # click.echo is kept at a terminal so a person sees exactly what they saw before (on Windows
    # click routes console output through its own wrapper); only the piped path changes.
    if is_terminal(stream):
        click.echo(text, nl=False, err=err)
        return
    out = _Utf8Writer(stream)
    out.write(text.encode("utf-8"))
    out.flush()


def use_utf8_when_piped() -> None:
    """Switch a piped or redirected stdout and stderr to UTF-8 with LF line ends, for the whole run.

    Called once by the process entry, before any output. A cp1252 text stdout (German Windows)
    raised ``UnicodeEncodeError`` on a character it cannot hold, so ``pwshpy env | more``, every
    status line naming such a path, an error line and ``pwshpy config`` (rendered by
    lib_layered_config's own console) failed after the work was done. Switching the stream itself
    reaches every writer at once - click, rich and that library - where wrapping each call site
    leaves the next one to be forgotten. LF makes the piped bytes the same on every OS, as the JSON
    output already is. A terminal keeps its encoding: a Windows console renders Unicode already. Each
    stream keeps its error handler (``reconfigure`` would reset it to strict), so this changes the
    encoding and nothing else. A stream that is missing or not a ``TextIOWrapper`` (pythonw, a test
    harness' stand-in) is left alone. Library use (``from pwshpy import ps``) never calls this, so a
    host program's streams are never touched.

    Example:
        >>> saved = sys.stdout, sys.stderr
        >>> sys.stdout, sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252"), None
        >>> use_utf8_when_piped()
        >>> piped = sys.stdout
        >>> sys.stdout, sys.stderr = saved
        >>> piped.encoding
        'utf-8'
    """
    for stream in (sys.stdout, sys.stderr):
        if is_terminal(stream) or not isinstance(stream, io.TextIOWrapper):
            continue
        stream.reconfigure(encoding="utf-8", errors=stream.errors, newline="\n")


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


__all__ = [
    "RecordFormat",
    "emit",
    "emit_json",
    "emit_lines",
    "emit_text",
    "is_pipe_closed",
    "is_terminal",
    "use_utf8_when_piped",
]
