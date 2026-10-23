"""Windows image (WIM, split .swm): the ``WindowsImage`` record, its enum, and the pure-Python WIM reader.

The reader tests run against synthesised WIM files under tmp_path (see ``wim_builder``), so
they are hermetic and portable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from wim_builder import (
    COMPRESSED_FLAG,
    MEASURED_ESD_HEADER_FLAGS,
    MEASURED_ESD_XML_FLAGS,
    METADATA_FLAG,
    image,
    image_xml,
    wim_bytes,
)

from pwshpy import ImageArchitecture, WindowsImage
from pwshpy.adapters.native import iter_wim_images
from pwshpy.adapters.native.wim import MAX_XML_BYTES
from pwshpy.domain.errors import NativeCallError


def _write(tmp_path: Path, data: bytes, name: str = "install.wim") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


@pytest.mark.os_agnostic
def test_windows_image_record_fields() -> None:
    """Fields the XML may omit default to empty, so a sparse image entry still builds."""
    image = WindowsImage(
        index=1, name="Pro", architecture=ImageArchitecture.X64, build=26200, sp_build=8973, languages=("de-DE",)
    )
    assert image.version == "" and image.default_language == "" and image.size_bytes == 0
    assert image.languages == ("de-DE",)
    assert image.to_dict()["architecture"] == "x64"


@pytest.mark.os_agnostic
def test_windows_image_architecture_defaults_to_unknown() -> None:
    """An image without an ``<ARCH>`` element reads as UNKNOWN, never as a guessed CPU."""
    assert WindowsImage(index=2, name="Core").architecture is ImageArchitecture.UNKNOWN


@pytest.mark.os_agnostic
def test_reads_each_image(tmp_path: Path) -> None:
    """Every <IMAGE> becomes one typed record with the fields Get-WindowsImage prints."""
    path = _write(tmp_path, wim_bytes(image_xml(image(1), image(2, name="Home", arch=12))))
    first, second = list(iter_wim_images(path))
    assert (first.index, first.name, first.architecture, first.build, first.sp_build) == (
        1,
        "Pro",
        ImageArchitecture.X64,
        26200,
        8973,
    )
    assert first.version == "10.0.26200.8973"
    assert first.languages == ("de-DE",)
    assert first.default_language == "de-DE"
    assert first.edition_id == "Professional"
    assert first.installation_type == "Client"
    assert first.description == "d"
    assert first.size_bytes == 123
    assert (second.index, second.name, second.architecture) == (2, "Home", ImageArchitecture.ARM64)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("arch", "expected"), [(0, "x86"), (5, "ARM"), (6, "ia64"), (9, "x64"), (12, "ARM64")])
def test_every_documented_architecture_number_maps(tmp_path: Path, arch: int, expected: str) -> None:
    """Each PROCESSOR_ARCHITECTURE_* number maps to the spelling Get-WindowsImage prints."""
    path = _write(tmp_path, wim_bytes(image_xml(image(arch=arch))))
    assert [img.architecture.value for img in iter_wim_images(path)] == [expected]


@pytest.mark.os_agnostic
def test_the_metadata_flag_real_wims_set_does_not_refuse(tmp_path: Path) -> None:
    """Real WIMs carry flag 0x02 on the XML resource; only 0x04 (compressed) is refused."""
    path = _write(tmp_path, wim_bytes(image_xml(image()), xml_flags=METADATA_FLAG))
    assert [img.index for img in iter_wim_images(path)] == [1]


@pytest.mark.os_agnostic
def test_a_flag_byte_of_zero_parses_too(tmp_path: Path) -> None:
    """A writer that leaves the flag byte clear still has a plain XML resource."""
    path = _write(tmp_path, wim_bytes(image_xml(image()), xml_flags=0))
    assert [img.index for img in iter_wim_images(path)] == [1]


@pytest.mark.os_agnostic
def test_a_real_esd_layout_reads_like_a_wim(tmp_path: Path) -> None:
    """Microsoft's install.esd (measured 2026-10-23): LZMS header flags, uncompressed XML.

    The header flags and the XML flag byte are the values measured on a real install.esd;
    only the XML resource is read, so the LZMS image data never matters. A failure means
    the reader refused a file because its header names LZMS: this guards against a future
    header-flag check (the XML flag 0x02 alone is covered by
    test_the_metadata_flag_real_wims_set_does_not_refuse).
    """
    path = _write(
        tmp_path,
        wim_bytes(image_xml(image()), header_flags=MEASURED_ESD_HEADER_FLAGS, xml_flags=MEASURED_ESD_XML_FLAGS),
        "install.esd",
    )
    assert [img.index for img in iter_wim_images(path)] == [1]


@pytest.mark.os_agnostic
def test_a_split_part_lists_every_image(tmp_path: Path) -> None:
    """Each .swm part carries the XML for all images, so a later part lists them like a .wim."""
    data = wim_bytes(image_xml(image(1), image(2)), part=2, total_parts=3)
    path = _write(tmp_path, data, "install2.swm")
    assert [img.index for img in iter_wim_images(path)] == [1, 2]


@pytest.mark.os_agnostic
def test_an_xml_resource_without_bom_is_read_as_utf16le(tmp_path: Path) -> None:
    """The WIM format fixes UTF-16LE; a missing BOM must not flip it to UTF-8 or native order."""
    body = image_xml(image(name="Bildung")).encode("utf-16-le")
    path = _write(tmp_path, wim_bytes("", body=body))
    assert [img.name for img in iter_wim_images(path)] == ["Bildung"]


@pytest.mark.os_agnostic
def test_non_ascii_text_survives(tmp_path: Path) -> None:
    """Names are UTF-16 text, not ASCII: a non-ASCII name round-trips unchanged."""
    name = "Windows " + chr(0x00C4) + chr(0x4E2D)
    path = _write(tmp_path, wim_bytes(image_xml(image(name=name))))
    assert [img.name for img in iter_wim_images(path)] == [name]


@pytest.mark.os_agnostic
def test_fields_the_xml_omits_stay_empty(tmp_path: Path) -> None:
    """An image without a <WINDOWS> block gets the record's empty defaults, never a guess."""
    path = _write(tmp_path, wim_bytes(image_xml('<IMAGE INDEX="3"><NAME>Setup</NAME></IMAGE>')))
    (only,) = iter_wim_images(path)
    assert (only.index, only.name, only.architecture) == (3, "Setup", ImageArchitecture.UNKNOWN)
    assert (only.version, only.build, only.sp_build, only.size_bytes) == ("", 0, 0, 0)
    assert only.languages == ()
    assert only.default_language == ""


@pytest.mark.os_agnostic
@pytest.mark.parametrize("arch", ["7", "abc", ""], ids=["unassigned-number", "letters", "empty"])
def test_an_unrecognised_architecture_is_unknown(tmp_path: Path, arch: str) -> None:
    """An ARCH outside PROCESSOR_ARCHITECTURE_* maps to UNKNOWN - never to x86, which is number 0."""
    xml = image_xml(image()).replace("<ARCH>9</ARCH>", f"<ARCH>{arch}</ARCH>")
    path = _write(tmp_path, wim_bytes(xml))
    assert [img.architecture for img in iter_wim_images(path)] == [ImageArchitecture.UNKNOWN]


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "value",
    ["abc", "-5", " ", chr(0x00B2), chr(0x0662), "1" * 5000],
    ids=["letters", "negative", "blank", "superscript-two", "arabic-indic-two", "huge"],
)
def test_a_non_decimal_number_reads_as_zero(tmp_path: Path, value: str) -> None:
    """A number field holding anything but ASCII decimal digits reads 0 and drops out of version."""
    xml = image_xml(image()).replace("<BUILD>26200</BUILD>", f"<BUILD>{value}</BUILD>")
    path = _write(tmp_path, wim_bytes(xml))
    (only,) = iter_wim_images(path)
    assert only.build == 0
    assert only.version == ""
    assert only.sp_build == 8973


@pytest.mark.os_agnostic
def test_whitespace_around_a_number_is_stripped(tmp_path: Path) -> None:
    """A pretty-printed XML puts newlines around numbers; they are layout, not part of the value."""
    xml = image_xml(image()).replace("<BUILD>26200</BUILD>", "<BUILD>\n  26200\n</BUILD>")
    path = _write(tmp_path, wim_bytes(xml))
    (only,) = iter_wim_images(path)
    assert (only.build, only.version) == (26200, "10.0.26200.8973")


@pytest.mark.os_agnostic
@pytest.mark.parametrize("index", ['INDEX="x"', 'INDEX=""', ""], ids=["letters", "empty", "absent"])
def test_an_image_without_a_numeric_index_is_refused(tmp_path: Path, index: str) -> None:
    """INDEX is how every DISM call addresses an image, so an unusable one is damage, not a default."""
    xml = image_xml(image()).replace('INDEX="1"', index)
    path = _write(tmp_path, wim_bytes(xml))
    with pytest.raises(NativeCallError, match="INDEX"):
        list(iter_wim_images(path))


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "mutate",
    [
        "truncated",
        "bad_magic",
        "offset_past_eof",
        "offset_beyond_signed_seek",
        "offset_u64_max",
        "compressed",
        "oversize",
        "doctype",
        "entity",
        "not_xml",
        "odd_length",
        "empty_resource",
        "missing",
        "directory",
    ],
)
def test_refuses_damaged_input(tmp_path: Path, mutate: str) -> None:
    """Every way a file can be damaged or hostile surfaces as NativeCallError, never a raw error."""
    path = tmp_path / "x.wim"
    good = wim_bytes(image_xml(image()))
    data = {
        "truncated": good[:-10],
        "bad_magic": wim_bytes(image_xml(image()), magic=b"NOTAWIM\0"),
        "offset_past_eof": wim_bytes(image_xml(image()), offset=10_000_000),
        "offset_beyond_signed_seek": wim_bytes(image_xml(image()), offset=2**63),
        "offset_u64_max": wim_bytes(image_xml(image()), offset=2**64 - 1),
        "compressed": wim_bytes(image_xml(image()), xml_flags=COMPRESSED_FLAG | METADATA_FLAG),
        "oversize": wim_bytes(image_xml(image()), size=MAX_XML_BYTES + 1),
        "doctype": wim_bytes('<!DOCTYPE x [<!ENTITY a "a">]>' + image_xml(image())),
        "entity": wim_bytes(image_xml(image(name="&a;"))),
        "not_xml": wim_bytes("not xml at all"),
        "odd_length": wim_bytes("", body=b"\xff\xfe" + image_xml(image()).encode("utf-16-le") + b"<"),
        "empty_resource": wim_bytes("", body=b""),
    }.get(mutate)
    if data is not None:
        path.write_bytes(data)
    if mutate == "directory":
        path.mkdir()
    with pytest.raises(NativeCallError):
        list(iter_wim_images(path))


@pytest.mark.os_agnostic
def test_a_short_header_is_refused(tmp_path: Path) -> None:
    """A file shorter than the 208-byte header is named as a header problem."""
    path = _write(tmp_path, b"MSWIM\0\0\0", "x.wim")
    with pytest.raises(NativeCallError, match="header"):
        list(iter_wim_images(path))


@pytest.mark.os_agnostic
def test_a_compressed_xml_resource_names_the_unsupported_format(tmp_path: Path) -> None:
    """A compressed XML resource (flag 0x04) is refused with a message naming the flag."""
    path = _write(tmp_path, wim_bytes(image_xml(image()), xml_flags=COMPRESSED_FLAG), "compressed.wim")
    with pytest.raises(NativeCallError, match=r"compressed XML resource \(resource flag 0x04\) is not supported"):
        list(iter_wim_images(path))


@pytest.mark.os_agnostic
def test_a_resource_past_the_end_of_file_names_the_truncation(tmp_path: Path) -> None:
    """A truncated copy is reported as truncated, not as a malformed or empty image list."""
    path = _write(tmp_path, wim_bytes(image_xml(image()))[:-10])
    with pytest.raises(NativeCallError, match="truncated"):
        list(iter_wim_images(path))


@pytest.mark.os_agnostic
def test_an_offset_inside_the_header_is_named(tmp_path: Path) -> None:
    """An offset pointing back into the header is damage; it is named, not parsed as garbage XML."""
    path = _write(tmp_path, wim_bytes(image_xml(image()), offset=16))
    with pytest.raises(NativeCallError, match="inside the header"):
        list(iter_wim_images(path))


@pytest.mark.os_agnostic
def test_an_oversize_resource_is_refused_before_reading(tmp_path: Path) -> None:
    """The size cap is checked against the header, so a hostile size never allocates."""
    path = _write(tmp_path, wim_bytes(image_xml(image()), size=MAX_XML_BYTES + 1))
    with pytest.raises(NativeCallError, match="limit"):
        list(iter_wim_images(path))


@pytest.mark.os_agnostic
def test_a_dtd_is_refused_by_name(tmp_path: Path) -> None:
    """No WIM writer emits a DTD; one is refused as such rather than expanded."""
    path = _write(tmp_path, wim_bytes('<!DOCTYPE x [<!ENTITY a "a">]>' + image_xml(image(name="&a;"))))
    with pytest.raises(NativeCallError, match="DTD"):
        list(iter_wim_images(path))


@pytest.mark.os_agnostic
def test_a_bare_doctype_is_refused_too(tmp_path: Path) -> None:
    """A DOCTYPE without any entity is still refused: the DTD itself is the refused construct."""
    path = _write(tmp_path, wim_bytes("<!DOCTYPE WIM>" + image_xml(image())))
    with pytest.raises(NativeCallError, match="DTD"):
        list(iter_wim_images(path))


@pytest.mark.os_agnostic
def test_images_are_yielded_lazily(tmp_path: Path) -> None:
    """The reader is a generator: nothing is opened until the first image is asked for."""
    images = iter_wim_images(tmp_path / "absent.wim")
    with pytest.raises(NativeCallError):
        next(images)
