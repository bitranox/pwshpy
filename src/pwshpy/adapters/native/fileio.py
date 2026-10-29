"""native predictable file output - UTF-8 **without a BOM**, on every OS.

PowerShell's ``Out-File`` and ``>`` default to UTF-16LE on Windows PowerShell 5.1,
inject BOMs that break Unix tools, and differ by host version (UTF-8-no-BOM only
became the default in PowerShell 6+) - so the same redirect produces different
bytes on different machines.  These helpers fix the encoding to a stable invariant:
UTF-8, no BOM, LF newlines, on every OS and Python version, unless you explicitly
ask otherwise.

Portable (stdlib + ``orjson``); works on every OS.  Every native failure maps to
:class:`~pwshpy.domain.errors.NativeCallError`: an unknown or non-text encoding
(``LookupError``), text the encoding cannot represent (``UnicodeEncodeError``) and an
``OSError`` from opening or writing the file (a refusal is a ``NativeAccessDeniedError``).

Contents:
    * :func:`write_text` - write text with a predictable encoding/BOM/newline.
    * :func:`write_records` - write typed records as UTF-8 JSON/JSONL (never a BOM).
"""

from __future__ import annotations

import codecs
import io
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from ...domain.errors import NativeCallError
from ...domain.records import PSRecord
from .marshal import native_error
from .record_json import describe_replacements, record_json_bytes

logger = logging.getLogger(__name__)

#: Map an encoding to its BOM-less codec, so a BOM is added ONLY when asked (Python's
#: bare ``utf-16``/``utf-32`` codecs would otherwise inject one unbidden).
_BOMLESS_CODEC = {
    "utf-8": "utf-8",
    "utf-8-sig": "utf-8",
    "utf-16": "utf-16-le",
    "utf-16-le": "utf-16-le",
    "utf-16-be": "utf-16-be",
    "utf-32": "utf-32-le",
    "utf-32-le": "utf-32-le",
    "utf-32-be": "utf-32-be",
}

#: The BOM bytes for each BOM-less codec (prepended only when ``bom=True``).
_BOM_BYTES = {
    "utf-8": b"\xef\xbb\xbf",
    "utf-16-le": b"\xff\xfe",
    "utf-16-be": b"\xfe\xff",
    "utf-32-le": b"\xff\xfe\x00\x00",
    "utf-32-be": b"\x00\x00\xfe\xff",
}


def write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str = "\n",
    bom: bool = False,
) -> Path:
    """Write ``text`` to ``path`` with a fully predictable encoding, BOM, and newline.

    The default is UTF-8, **no BOM**, LF newlines - identical bytes on every OS and
    Python version, unlike PowerShell's version- and host-dependent ``Out-File``.
    Input newlines (any of ``\\r\\n``/``\\r``/``\\n``) are normalized to ``newline``.

    Args:
        path: Destination file path.
        text: The text to write.
        encoding: Text encoding (default ``"utf-8"``).
        newline: The line terminator to emit (default ``"\\n"``; pass ``"\\r\\n"`` for CRLF).
        bom: Whether to prepend a byte-order mark (default ``False`` - never a BOM).

    Returns:
        The :class:`~pathlib.Path` written.

    Raises:
        NativeCallError: ``encoding`` names no text encoding, ``text`` cannot be encoded in
            it (nothing is written then), or the file cannot be written.

    Example:
        >>> import tempfile, os
        >>> p = os.path.join(tempfile.mkdtemp(), "out.txt")
        >>> _ = write_text(p, "hi")
        >>> Path(p).read_bytes()
        b'hi'
    """
    destination = Path(path)
    codec = _codec_for(encoding, destination)
    data = _encode(_normalize_newlines(text, newline), codec, destination)
    if bom:
        data = _BOM_BYTES.get(codec, b"") + data
    try:
        destination.write_bytes(data)
    except OSError as exc:
        raise native_error(f"cannot write {str(destination)!r}: {exc}", exc) from exc
    return destination


def write_text_stream(
    path: str | Path,
    chunks: Iterable[str],
    *,
    encoding: str = "utf-8",
    newline: str = "\n",
    bom: bool = False,
) -> Path:
    """Stream text ``chunks`` to ``path`` with the same predictable encoding as :func:`write_text`.

    **Memory-bounded**: writes one chunk at a time (never joining them into one string),
    so piping a huge or unbounded source (e.g. stdin line-by-line) stays flat in memory -
    the same discipline as :func:`write_records`.  Feed it newline-complete chunks (lines);
    each chunk's newlines are normalized to ``newline`` independently.

    Args:
        path: Destination file path.
        chunks: An iterable of text pieces (e.g. ``sys.stdin``, consumed lazily).
        encoding: Text encoding (default ``"utf-8"``).
        newline: The line terminator to emit (default ``"\\n"``).
        bom: Whether to prepend a byte-order mark (default ``False``).

    Returns:
        The :class:`~pathlib.Path` written.

    Raises:
        NativeCallError: ``encoding`` names no text encoding (nothing is opened), a chunk
            cannot be encoded in it, or the file cannot be opened or written; the chunks
            before a failure stay written. An exception the ``chunks`` iterable itself raises
            (a ``UnicodeDecodeError`` reading stdin) passes through unchanged, except an
            ``OSError``, which is reported like a write failure.

    Example:
        >>> import tempfile, os
        >>> p = os.path.join(tempfile.mkdtemp(), "stream.txt")
        >>> _ = write_text_stream(p, ["line1\\n", "line2\\n"])
        >>> Path(p).read_bytes()
        b'line1\\nline2\\n'
    """
    destination = Path(path)
    codec = _codec_for(encoding, destination)
    try:
        with destination.open("wb") as handle:
            if bom:
                handle.write(_BOM_BYTES.get(codec, b""))
            # A chunk ending in CR is held back by one character: its LF may start the next chunk, and
            # normalizing the two halves apart wrote one line end twice (POSIX stdin does no newline
            # translation, so write_text's fixed-size blocks split a CRLF wherever it falls).
            pending = ""
            for chunk in chunks:
                text = pending + chunk
                pending = "\r" if text.endswith("\r") else ""
                complete = _normalize_newlines(text[: len(text) - len(pending)], newline)
                handle.write(_encode(complete, codec, destination))
            if pending:
                handle.write(_encode(_normalize_newlines(pending, newline), codec, destination))
    except NativeCallError:
        raise  # already typed (an encode failure, or a refusal that is a PermissionError too)
    except OSError as exc:
        raise native_error(f"cannot write {str(destination)!r}: {exc}", exc) from exc
    return destination


def _codec_for(encoding: str, destination: Path) -> str:
    """:func:`_bomless_codec`, with an unknown or non-text encoding raised as NativeCallError."""
    try:
        return _bomless_codec(encoding)
    except LookupError as exc:
        raise native_error(f"cannot write {str(destination)!r}: {exc}", exc) from exc


def _encode(text: str, codec: str, destination: Path) -> bytes:
    """``text`` encoded with ``codec``; a character the codec cannot represent raises NativeCallError."""
    try:
        return text.encode(codec)
    except UnicodeEncodeError as exc:
        raise native_error(f"cannot write {str(destination)!r} as {codec}: {exc}", exc) from exc


def _bomless_codec(encoding: str) -> str:
    """The codec that writes ``encoding`` with no BOM, whatever spelling names it.

    The map is keyed on Python's canonical names, so ``utf_16``, ``utf16`` and ``UTF-16`` all reach
    ``utf-16-le``; looking up only the lower-cased spelling let an alias through to a codec that
    writes a BOM on every ``encode`` call - one per chunk, and one for an empty string.

    Raises:
        LookupError: ``encoding`` names no codec, or one that is not a text encoding (``base64``,
            ``rot13``): ``codecs.lookup`` accepts those, so a text stream is asked instead.

    Example:
        >>> _bomless_codec("utf_16"), _bomless_codec("UTF8"), _bomless_codec("cp1252")
        ('utf-16-le', 'utf-8', 'cp1252')
    """
    io.TextIOWrapper(io.BytesIO(), encoding=encoding)
    name = codecs.lookup(encoding).name
    return _BOMLESS_CODEC.get(name, name)


def _normalize_newlines(text: str, newline: str) -> str:
    """Turn every CRLF, CR and LF in ``text`` into ``newline``.

    Example:
        >>> _normalize_newlines("a\\r\\nb\\rc\\n", "\\r\\n")
        'a\\r\\nb\\r\\nc\\r\\n'
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if newline == "\n" else normalized.replace("\n", newline)


def write_records(path: str | Path, records: Iterable[PSRecord], *, jsonl: bool = True) -> Path:
    """Write typed records to ``path`` as UTF-8 JSON/JSONL, always without a BOM.

    **Streams** one record at a time straight to the file, so it stays memory-bounded
    even for a huge or unbounded source (it never materializes the whole set) - the
    same discipline as the read pipelines.  ``jsonl=True`` (default) writes one JSON
    object per line (append-friendly, cheap through ``| head``); ``jsonl=False`` writes
    a single JSON array. The bytes are UTF-8 with no BOM - the same invariant on every OS.

    A field holding a byte that is not valid UTF-8 (a POSIX name or environment value Python
    decoded to a lone surrogate) is written with U+FFFD for each such byte, so the file stays
    valid UTF-8 JSON, and one WARNING per record is logged on this module's logger naming the
    record type, its 1-based position and the field paths (never the value). Every record is
    written; the original bytes cannot be recovered from the file.

    Args:
        path: Destination file path.
        records: The records to serialize (any iterable of :class:`PSRecord`, incl. a
            lazy generator/pipeline - consumed one record at a time).
        jsonl: One-object-per-line when ``True``; a single JSON array when ``False``.

    Returns:
        The :class:`~pathlib.Path` written.

    Raises:
        orjson.JSONEncodeError: A record holds a value orjson cannot write other than a lone
            surrogate (an integer outside the 64-bit range); the records before it are written.
        NativeCallError: The file cannot be opened or written. A pwshpy error the ``records``
            source raises passes through unchanged.

    Example:
        >>> import tempfile, os
        >>> from pwshpy.domain.records import EnvVar
        >>> p = os.path.join(tempfile.mkdtemp(), "recs.jsonl")
        >>> _ = write_records(p, [EnvVar(name="A", value="1")])
        >>> Path(p).read_bytes()
        b'{"name":"A","value":"1"}\\n'
    """
    # record_json_bytes rather than model_dump_json(): the same bytes for every ordinary record,
    # but the two disagree on integers beyond 64 bits, lone surrogates and non-UTF-8 bytes
    # (tests/test_record_json_bytes.py), and the CLI's --json/--jsonl use this serialiser too.
    destination = Path(path)
    try:
        with destination.open("wb") as handle:
            _write_record_stream(handle, records, jsonl=jsonl)
    except NativeCallError:
        raise  # the record source's own pwshpy error (a PermissionError too when it is a refusal)
    except OSError as exc:
        raise native_error(f"cannot write {str(destination)!r}: {exc}", exc) from exc
    return destination


def _write_record_stream(handle: BinaryIO, records: Iterable[PSRecord], *, jsonl: bool) -> None:
    """Write ``records`` to ``handle`` as JSONL, or as one JSON array when ``jsonl`` is False."""
    if jsonl:
        for position, record in enumerate(records, start=1):
            handle.write(_record_bytes(record, position))
            handle.write(b"\n")
        return
    handle.write(b"[")
    for position, record in enumerate(records, start=1):
        if position > 1:
            handle.write(b",")
        handle.write(_record_bytes(record, position))
    handle.write(b"]")


def _record_bytes(record: PSRecord, position: int) -> bytes:
    """``record`` as JSON bytes; a field that was not valid UTF-8 is logged as a WARNING."""
    result = record_json_bytes(record)
    if result.replaced:
        logger.warning("%s", describe_replacements(record, position, result.replaced))
    return result.data


__all__ = ["write_records", "write_text", "write_text_stream"]
