"""Text and JSON to stdout/stderr: UTF-8 when piped, a closed pipe exits cleanly.

The writers every output path shares, in a neutral adapters module rather than in the CLI
package, so the configuration adapter (``display_config``'s JSON) can use them without importing
``pwshpy.adapters.cli``: that loaded every command module on a plain ``import pwshpy`` and closed an
import cycle back into ``pwshpy.adapters.config``. :mod:`pwshpy.adapters.cli.output` re-exports
them for the commands.

JSON (:func:`emit_json`) is written as UTF-8 bytes below the text layer, whatever the console code
page says, with LF line ends; a value holding a byte that is not valid UTF-8 is written with U+FFFD
for that byte and one ``warning:`` line on stderr names where. Passed-through text
(:func:`emit_text`) is written the same way when its stream is not a terminal, and through
``click.echo`` at a terminal. A missing stream (``None``, pythonw, a detached service) drops the
output. A downstream reader closing the pipe (``| head``) ends the write cleanly, exit 0.

Contents:
    * :func:`emit_json` - one JSON value to stdout.
    * :func:`emit_text` - pass a text body through, UTF-8 when piped.
    * :class:`Utf8Writer` - UTF-8 bytes below a text stream's encoding.
    * :func:`clean_broken_pipe` - exit cleanly when the reader closes the pipe.
    * :func:`is_pipe_closed` / :func:`is_terminal` - the two stream questions the writers ask.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
from collections.abc import Generator
from typing import BinaryIO, TextIO

import rich_click as click

from .native.record_json import describe_output_replacements, json_bytes

#: Windows error codes for a downstream reader closing the pipe: ERROR_BROKEN_PIPE (109)
#: and ERROR_NO_DATA (232, "the pipe is being closed"). On Windows these surface as a
#: plain ``OSError``, not ``BrokenPipeError``.
_WINDOWS_PIPE_CLOSED = frozenset({109, 232})


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
def clean_broken_pipe() -> Generator[None, None, None]:
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


class Utf8Writer:
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
        >>> out = Utf8Writer(sys.stdout)
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
    with clean_broken_pipe():
        result = json_bytes(data, default=str, indent=indent)
        if result.replaced:
            emit_text(f"warning: {describe_output_replacements(result.replaced)}\n", err=True)
        out = Utf8Writer(sys.stdout)
        out.write(result.data + b"\n")
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
    with clean_broken_pipe():
        _write_text_body(text, stream, err=False)


def _write_text_body(text: str, stream: TextIO | None, *, err: bool) -> None:
    """Write ``text`` to ``stream``: via ``click.echo`` at a terminal, as UTF-8 bytes otherwise."""
    # click.echo is kept at a terminal so a person sees exactly what they saw before (on Windows
    # click routes console output through its own wrapper); only the piped path changes.
    if is_terminal(stream):
        click.echo(text, nl=False, err=err)
        return
    out = Utf8Writer(stream)
    out.write(text.encode("utf-8"))
    out.flush()


__all__ = [
    "Utf8Writer",
    "clean_broken_pipe",
    "emit_json",
    "emit_text",
    "is_pipe_closed",
    "is_terminal",
]
