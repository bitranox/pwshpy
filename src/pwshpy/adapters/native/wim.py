"""Read the image list of a WIM file without DISM: header plus the XML resource (portable, stdlib).

A WIM keeps its image metadata as a UTF-16LE XML document whose location sits in the
208-byte header: the XML resource header at byte 72 is a 7-byte stored size and one flag
byte, followed by the offset (u64 at 80) and the original size (u64 at 88). Reading that
one resource answers ``Get-WindowsImage -ImagePath`` on any OS, with no DISM, no wimlib
and no mounting.

The file is untrusted input, so every number taken from it is checked before use: the XML
size is capped at :data:`MAX_XML_BYTES`, an offset or size reaching past the end of the
file is reported as a truncated copy (the XML is usually the LAST thing in a WIM, so a cut
copy loses it first), and the XML parser refuses any DTD - no WIM writer emits one, and
entity declarations are the expansion-attack vector.

Scope: a split part (``.swm``) carries the full XML for every image, so each part lists
the images like a ``.wim`` does; the part number in header bytes 40-43 is not needed for
that and is not read. A compressed XML resource (resource flag 0x04) would need a
decompressor and is refused rather than guessed at.

Contents:
    * :func:`iter_wim_images` - yield one :class:`WindowsImage` per ``<IMAGE>``.
    * :data:`MAX_XML_BYTES` - the cap on the XML resource size read from a file.
"""

from __future__ import annotations

import io
import os
import re
import struct
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import iterparse as _xml_iterparse

from ...domain.enums import ImageArchitecture
from ...domain.errors import NativeCallError
from ...domain.records import WindowsImage

# Real install images carry a few KB of XML per image; the cap only bounds a hostile header.
MAX_XML_BYTES = 16 * 1024 * 1024

_MAGIC = b"MSWIM\0\0\0"
_HEADER_SIZE = 208
_RESHDR_SIZE_SLICE = slice(72, 79)
_RESHDR_FLAGS_AT = 79
_RESHDR_OFFSET_AT = 80
# Only the compressed bit decides anything here. Real WIMs set 0x02 (metadata) on the XML
# resource as well, so testing the flag byte as a whole would refuse every genuine file.
_RESHDR_COMPRESSED = 0x04
# A u64 has at most 20 decimal digits; the bound also keeps int() away from its 4300-digit
# limit, which would raise ValueError on a hostile value instead of reading it as absent.
_DECIMAL = re.compile(r"[0-9]{1,20}")
_ARCH = {
    0: ImageArchitecture.X86,
    5: ImageArchitecture.ARM,
    6: ImageArchitecture.IA64,
    9: ImageArchitecture.X64,
    12: ImageArchitecture.ARM64,
}


class _XmlNode(Protocol):
    """The slice of an ElementTree element this reader uses.

    Declared here rather than imported from ``xml.etree`` so the module never names the
    stdlib XML package (bandit B405 flags that import even when it is only a type); every
    parse goes through defusedxml, whose stub types its iterator as ``Any``.
    """

    tag: str
    text: str | None

    def get(self, key: str, default: str) -> str: ...

    def find(self, path: str) -> _XmlNode | None: ...

    def findall(self, path: str) -> Iterable[_XmlNode]: ...

    def clear(self) -> None: ...


def _xml_location(header: bytes, path: Path) -> tuple[int, int]:
    """Return ``(offset, size)`` of the XML resource after checking the header that names it."""
    if len(header) < _HEADER_SIZE:
        raise NativeCallError(f"{path}: not a WIM file (shorter than the {_HEADER_SIZE}-byte header)")
    if header[:8] != _MAGIC:
        raise NativeCallError(f"{path}: not a WIM file (bad header magic)")
    if header[_RESHDR_FLAGS_AT] & _RESHDR_COMPRESSED:
        raise NativeCallError(
            f"{path}: compressed XML resource (resource flag 0x04) is not supported; use DISM on Windows"
        )
    size = int.from_bytes(header[_RESHDR_SIZE_SLICE], "little")
    (offset,) = struct.unpack_from("<Q", header, _RESHDR_OFFSET_AT)
    if size == 0:
        raise NativeCallError(f"{path}: the header points at no XML resource")
    if size > MAX_XML_BYTES:
        raise NativeCallError(f"{path}: XML resource of {size} bytes exceeds the {MAX_XML_BYTES}-byte limit")
    if offset < _HEADER_SIZE:
        raise NativeCallError(f"{path}: XML resource offset {offset} lies inside the header")
    return offset, size


def _truncated(path: Path, offset: int, size: int, remaining: int) -> NativeCallError:
    """Name a resource that runs past the end of the file."""
    return NativeCallError(
        f"{path}: truncated - the XML resource at offset {offset} needs {size} bytes, only {remaining} remain"
    )


def _read_xml_resource(path: Path) -> bytes:
    """Return the raw XML resource bytes; the handle is closed before any image is yielded."""
    try:
        with path.open("rb") as handle:
            offset, size = _xml_location(handle.read(_HEADER_SIZE), path)
            file_size = os.fstat(handle.fileno()).st_size
            # The offset is an unsigned u64 from untrusted bytes: seek() raises ValueError, not
            # OSError, for one past the signed 64-bit range, so it must be refused before seeking.
            if offset + size > file_size:
                raise _truncated(path, offset, size, max(0, file_size - offset))
            handle.seek(offset)
            raw = handle.read(size)
    except OSError as exc:
        raise NativeCallError(f"cannot read {path}: {exc}") from exc
    if len(raw) != size:
        # The size check saw the file as it was; one cut between that check and the read
        # shows up only here.
        raise _truncated(path, offset, size, len(raw))
    return raw


def _top_level_elements(raw: bytes, path: Path) -> Iterator[_XmlNode]:
    """Yield each direct child of the XML root as it completes, then drop it.

    Clearing the root after every child keeps memory at one image subtree whatever the
    XML holds, so a consumer that stops early (``.take(1)``) never pays for the rest.
    The bytes go to expat undecoded: it reads the UTF-16 BOM real WIMs carry, and sniffs
    UTF-16LE from the leading ``<`` when a writer left the BOM out.
    """
    depth = 0
    root: _XmlNode | None = None
    try:
        stream = io.BytesIO(raw)
        events: Iterator[tuple[str, _XmlNode]] = _xml_iterparse(stream, events=("start", "end"), forbid_dtd=True)
        for event, element in events:
            depth += 1 if event == "start" else -1
            if root is None:
                root = element
            elif event == "end" and depth == 1:
                yield element
                root.clear()
    except DefusedXmlException as exc:
        raise NativeCallError(f"{path}: XML resource carries a DTD or entity declaration; refused ({exc})") from exc
    except ParseError as exc:
        raise NativeCallError(f"{path}: malformed image XML: {exc}") from exc


def _text_of(element: _XmlNode) -> str:
    return (element.text or "").strip()


def _text(node: _XmlNode, path: str) -> str:
    found = node.find(path)
    return _text_of(found) if found is not None else ""


def _decimal(node: _XmlNode, path: str) -> str | None:
    """Return the field's ASCII decimal digits, or None when it holds anything else."""
    value = _text(node, path)
    return value if _DECIMAL.fullmatch(value) else None


def _int(node: _XmlNode, path: str) -> int:
    value = _decimal(node, path)
    return int(value) if value is not None else 0


def _index(node: _XmlNode, path: Path) -> int:
    value = node.get("INDEX", "")
    if not _DECIMAL.fullmatch(value):
        raise NativeCallError(f"{path}: an <IMAGE> has no numeric INDEX attribute ({value!r})")
    return int(value)


def _architecture(node: _XmlNode) -> ImageArchitecture:
    # Absent or non-numeric must stay UNKNOWN: reading it through _int would turn it into
    # 0, which is PROCESSOR_ARCHITECTURE_INTEL, and report a guessed x86.
    value = _decimal(node, "WINDOWS/ARCH")
    return _ARCH.get(int(value), ImageArchitecture.UNKNOWN) if value is not None else ImageArchitecture.UNKNOWN


def _version(node: _XmlNode) -> str:
    parts = [_decimal(node, f"WINDOWS/VERSION/{part}") for part in ("MAJOR", "MINOR", "BUILD", "SPBUILD")]
    numbers = [str(int(part)) for part in parts if part is not None]
    return ".".join(numbers) if len(numbers) == len(parts) else ""


def _image(node: _XmlNode, path: Path) -> WindowsImage:
    """Build a validated record: the XML is untrusted file content, not a trusted native value."""
    languages = (_text_of(lang) for lang in node.findall("WINDOWS/LANGUAGES/LANGUAGE"))
    return WindowsImage(
        index=_index(node, path),
        name=_text(node, "NAME"),
        description=_text(node, "DESCRIPTION"),
        edition_id=_text(node, "WINDOWS/EDITIONID"),
        installation_type=_text(node, "WINDOWS/INSTALLATIONTYPE"),
        architecture=_architecture(node),
        version=_version(node),
        build=_int(node, "WINDOWS/VERSION/BUILD"),
        sp_build=_int(node, "WINDOWS/VERSION/SPBUILD"),
        languages=tuple(lang for lang in languages if lang),
        default_language=_text(node, "WINDOWS/LANGUAGES/DEFAULT"),
        size_bytes=_int(node, "TOTALBYTES"),
    )


def iter_wim_images(path: Path) -> Iterator[WindowsImage]:
    """Yield each image of the WIM (or split ``.swm`` part) at ``path``, like ``Get-WindowsImage``.

    Lazy: nothing is opened until the first image is requested. The XML is parsed as a
    stream, so a malformed tail is reported after the images before it were yielded.

    Args:
        path: The ``.wim`` / ``.swm`` file to read.

    Yields:
        One :class:`WindowsImage` per ``<IMAGE>`` element, in file order.

    Raises:
        NativeCallError: The file is unreadable, not a WIM, truncated, holds a compressed
            (resource flag 0x04) or oversize XML resource, a DTD, malformed XML, or an image without a
            numeric ``INDEX``.

    Example:
        >>> import types
        >>> isinstance(iter_wim_images(Path("any.wim")), types.GeneratorType)
        True
    """
    for node in _top_level_elements(_read_xml_resource(path), path):
        if node.tag == "IMAGE":
            yield _image(node, path)


__all__ = ["MAX_XML_BYTES", "iter_wim_images"]
