"""Build minimal WIM files in memory: a 208-byte header and a UTF-16LE XML resource after it.

Synthesised on purpose: the XML of a real install image names internal editions, and this
is a public repository. The layout mirrors what real install.wim / install.swm / boot.wim
files carry (XML resource right after the header, stored size equal to original size, the
metadata flag 0x02 set on the XML resource header, a UTF-16LE BOM in front of the XML).
"""

from __future__ import annotations

import struct

HEADER_SIZE = 208
MAGIC = b"MSWIM\0\0\0"
METADATA_FLAG = 0x02
COMPRESSED_FLAG = 0x04
# Measured 2026-10-23 on Microsoft's install.esd, build 26100.4349 en-us, x64 and arm64 alike:
# header flags (u32 at 16) are COMPRESS_LZMS 0x00080000 | RP_FIX 0x80 | COMPRESSION 0x02.
MEASURED_ESD_HEADER_FLAGS = 0x00080082
# Same files: the XML resource flag byte (79) is metadata only, not compressed, and its stored
# size equals its original size, so the XML sits raw (UTF-16LE with a BOM) as in a .wim.
MEASURED_ESD_XML_FLAGS = 0x02


def image_xml(*images: str) -> str:
    """Wrap ``<IMAGE>`` fragments in the ``<WIM>`` root a real XML resource has."""
    return "<WIM><TOTALBYTES>1</TOTALBYTES>" + "".join(images) + "</WIM>"


def image(index: int = 1, *, name: str = "Pro", arch: int = 9, build: int = 26200, spbuild: int = 8973) -> str:
    """Return one ``<IMAGE>`` fragment shaped like a Windows client image."""
    return (
        f'<IMAGE INDEX="{index}"><TOTALBYTES>123</TOTALBYTES><NAME>{name}</NAME><DESCRIPTION>d</DESCRIPTION>'
        f"<WINDOWS><ARCH>{arch}</ARCH><EDITIONID>Professional</EDITIONID><INSTALLATIONTYPE>Client</INSTALLATIONTYPE>"
        '<LANGUAGES><LANGUAGE>de-DE</LANGUAGE><FALLBACK LANGUAGE="de-DE">en-US</FALLBACK><DEFAULT>de-DE</DEFAULT>'
        "</LANGUAGES>"
        f"<VERSION><MAJOR>10</MAJOR><MINOR>0</MINOR><BUILD>{build}</BUILD><SPBUILD>{spbuild}</SPBUILD></VERSION>"
        "</WINDOWS></IMAGE>"
    )


def wim_bytes(
    xml: str,
    *,
    magic: bytes = MAGIC,
    header_flags: int = 0,
    xml_flags: int = METADATA_FLAG,
    offset: int | None = None,
    size: int | None = None,
    part: int = 1,
    total_parts: int = 1,
    body: bytes | None = None,
) -> bytes:
    """Return a header plus XML resource.

    Header fields written: magic (0), cbSize (8), flags (16), part number and total parts
    (40, 42), the XML resource header at 72 (7-byte stored size + 1 flag byte, offset u64
    at 80, original size u64 at 88). ``body`` replaces the BOM-prefixed UTF-16LE encoding of ``xml``, for
    tests that need bytes no well-formed string encodes to.
    """
    payload = b"\xff\xfe" + xml.encode("utf-16-le") if body is None else body
    header = bytearray(HEADER_SIZE)
    header[0:8] = magic
    struct.pack_into("<I", header, 8, HEADER_SIZE)
    struct.pack_into("<I", header, 16, header_flags)
    struct.pack_into("<HH", header, 40, part, total_parts)
    length = len(payload) if size is None else size
    header[72:79] = length.to_bytes(7, "little")
    header[79] = xml_flags
    struct.pack_into("<Q", header, 80, HEADER_SIZE if offset is None else offset)
    struct.pack_into("<Q", header, 88, len(payload))
    return bytes(header) + payload
