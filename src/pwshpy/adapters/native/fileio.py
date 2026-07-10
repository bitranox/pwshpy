"""native predictable file output - UTF-8 **without a BOM**, on every OS.

PowerShell's ``Out-File`` and ``>`` default to UTF-16LE on Windows PowerShell 5.1,
inject BOMs that break Unix tools, and differ by host version (UTF-8-no-BOM only
became the default in PowerShell 6+) - so the same redirect produces different
bytes on different machines.  These helpers fix the encoding to a stable invariant:
UTF-8, no BOM, LF newlines, on every OS and Python version, unless you explicitly
ask otherwise.

Portable (stdlib + ``orjson``); works on every OS.

Contents:
    * :func:`write_text` - write text with a predictable encoding/BOM/newline.
    * :func:`write_records` - write typed records as UTF-8 JSON/JSONL (never a BOM).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import orjson

from ...domain.records import PSRecord

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

    Example:
        >>> import tempfile, os
        >>> p = os.path.join(tempfile.mkdtemp(), "out.txt")
        >>> _ = write_text(p, "hi")
        >>> Path(p).read_bytes()
        b'hi'
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline != "\n":
        normalized = normalized.replace("\n", newline)
    codec = _BOMLESS_CODEC.get(encoding.lower(), encoding)
    data = normalized.encode(codec)
    if bom:
        data = _BOM_BYTES.get(codec, b"") + data
    destination = Path(path)
    destination.write_bytes(data)
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

    Example:
        >>> import tempfile, os
        >>> p = os.path.join(tempfile.mkdtemp(), "stream.txt")
        >>> _ = write_text_stream(p, ["line1\\n", "line2\\n"])
        >>> Path(p).read_bytes()
        b'line1\\nline2\\n'
    """
    codec = _BOMLESS_CODEC.get(encoding.lower(), encoding)
    destination = Path(path)
    with destination.open("wb") as handle:
        if bom:
            handle.write(_BOM_BYTES.get(codec, b""))
        for chunk in chunks:
            normalized = chunk.replace("\r\n", "\n").replace("\r", "\n")
            if newline != "\n":
                normalized = normalized.replace("\n", newline)
            handle.write(normalized.encode(codec))
    return destination


def write_records(path: str | Path, records: Iterable[PSRecord], *, jsonl: bool = True) -> Path:
    """Write typed records to ``path`` as UTF-8 JSON/JSONL, always without a BOM.

    **Streams** one record at a time straight to the file, so it stays memory-bounded
    even for a huge or unbounded source (it never materializes the whole set) - the
    same discipline as the read pipelines.  ``jsonl=True`` (default) writes one JSON
    object per line (append-friendly, cheap through ``| head``); ``jsonl=False`` writes
    a single JSON array. The bytes are UTF-8 with no BOM - the same invariant on every OS.

    Args:
        path: Destination file path.
        records: The records to serialize (any iterable of :class:`PSRecord`, incl. a
            lazy generator/pipeline - consumed one record at a time).
        jsonl: One-object-per-line when ``True``; a single JSON array when ``False``.

    Returns:
        The :class:`~pathlib.Path` written.

    Example:
        >>> import tempfile, os
        >>> from pwshpy.domain.records import EnvVar
        >>> p = os.path.join(tempfile.mkdtemp(), "recs.jsonl")
        >>> _ = write_records(p, [EnvVar(name="A", value="1")])
        >>> Path(p).read_bytes()
        b'{"name":"A","value":"1"}\\n'
    """
    destination = Path(path)
    with destination.open("wb") as handle:
        if jsonl:
            for record in records:
                handle.write(orjson.dumps(record.to_dict()))
                handle.write(b"\n")
        else:
            handle.write(b"[")
            for index, record in enumerate(records):
                if index:
                    handle.write(b",")
                handle.write(orjson.dumps(record.to_dict()))
            handle.write(b"]")
    return destination


__all__ = ["write_records", "write_text", "write_text_stream"]
