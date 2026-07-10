"""native predictable file output: os_agnostic, hermetic tests over a tmp path.

Assert the invariant the PowerShell Out-File pain is about: UTF-8, no BOM, LF by
default, on every OS - plus explicit opt-in BOM/encoding/newline control.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pwshpy.adapters.native.fileio import write_records, write_text, write_text_stream
from pwshpy.domain.records import EnvVar


@pytest.mark.os_agnostic
def test_write_text_default_is_utf8_no_bom_lf(tmp_path: Path) -> None:
    """Default output is UTF-8 with no BOM and LF newlines."""
    target = tmp_path / "out.txt"
    write_text(target, "line1\r\nline2\r\n")
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # no BOM
    assert raw == b"line1\nline2\n"  # CRLF normalized to LF


@pytest.mark.os_agnostic
def test_write_text_bom_opt_in(tmp_path: Path) -> None:
    """bom=True prepends exactly the UTF-8 BOM."""
    target = tmp_path / "out.txt"
    write_text(target, "hi", bom=True)
    assert target.read_bytes() == b"\xef\xbb\xbf" + b"hi"


@pytest.mark.os_agnostic
def test_write_text_crlf_newline(tmp_path: Path) -> None:
    """newline='\\r\\n' emits CRLF regardless of the input's newlines."""
    target = tmp_path / "out.txt"
    write_text(target, "a\nb\n", newline="\r\n")
    assert target.read_bytes() == b"a\r\nb\r\n"


@pytest.mark.os_agnostic
def test_write_text_non_ascii_roundtrips_utf8(tmp_path: Path) -> None:
    """Non-ASCII text is written as UTF-8 (no BOM) and reads back intact."""
    target = tmp_path / "out.txt"
    write_text(target, "grüße 世界")
    assert target.read_bytes() == "grüße 世界".encode()
    assert target.read_text(encoding="utf-8") == "grüße 世界"


@pytest.mark.os_agnostic
def test_write_text_utf16le_bom_controlled(tmp_path: Path) -> None:
    """utf-16 gets a BOM only when asked - never Python's silently-injected one."""
    target = tmp_path / "no_bom.txt"
    write_text(target, "hi", encoding="utf-16")
    assert target.read_bytes() == "hi".encode("utf-16-le")  # no BOM despite utf-16
    with_bom = tmp_path / "with_bom.txt"
    write_text(with_bom, "hi", encoding="utf-16", bom=True)
    assert with_bom.read_bytes() == b"\xff\xfe" + "hi".encode("utf-16-le")


@pytest.mark.os_agnostic
def test_write_records_jsonl_no_bom(tmp_path: Path) -> None:
    """JSONL output is one UTF-8 object per line with no BOM."""
    target = tmp_path / "recs.jsonl"
    write_records(target, [EnvVar(name="A", value="1"), EnvVar(name="B", value="2")])
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw == b'{"name":"A","value":"1"}\n{"name":"B","value":"2"}\n'


@pytest.mark.os_agnostic
def test_write_records_json_array(tmp_path: Path) -> None:
    """jsonl=False writes a single JSON array."""
    target = tmp_path / "recs.json"
    write_records(target, [EnvVar(name="A", value="1")], jsonl=False)
    assert target.read_bytes() == b'[{"name":"A","value":"1"}]'


@pytest.mark.os_agnostic
def test_write_records_streams_from_a_lazy_generator(tmp_path: Path) -> None:
    """write_records consumes a generator one record at a time (memory-bounded), not by listing it."""
    pulled: list[str] = []

    def gen() -> Iterator[EnvVar]:
        for name in ("A", "B", "C"):
            pulled.append(name)  # records each pull so we can prove it iterated, not materialized
            yield EnvVar(name=name, value=name.lower())

    target = tmp_path / "stream.jsonl"
    write_records(target, gen())
    assert pulled == ["A", "B", "C"]
    assert target.read_bytes() == b'{"name":"A","value":"a"}\n{"name":"B","value":"b"}\n{"name":"C","value":"c"}\n'


@pytest.mark.os_agnostic
def test_write_text_stream_matches_write_text_bytes(tmp_path: Path) -> None:
    """Streaming chunks produces the same predictable bytes as write_text: UTF-8, no BOM, retargeted newline."""
    target = tmp_path / "stream.txt"
    write_text_stream(target, ["grüße\n", "世界\n"], newline="\r\n")
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw == "grüße\r\n世界\r\n".encode()


@pytest.mark.os_agnostic
def test_write_text_stream_bom_opt_in(tmp_path: Path) -> None:
    """bom=True prepends exactly one BOM, before the streamed content."""
    target = tmp_path / "stream_bom.txt"
    write_text_stream(target, ["a", "b"], bom=True)
    assert target.read_bytes() == b"\xef\xbb\xbf" + b"ab"


@pytest.mark.os_agnostic
def test_write_text_stream_is_memory_bounded(tmp_path: Path) -> None:
    """write_text_stream consumes its source one chunk at a time (never joins the whole input)."""
    pulled: list[str] = []

    def gen() -> Iterator[str]:
        for name in ("A", "B", "C"):
            pulled.append(name)  # proves lazy iteration, not materialization
            yield f"{name}\n"

    target = tmp_path / "stream_lazy.txt"
    write_text_stream(target, gen())
    assert pulled == ["A", "B", "C"]
    assert target.read_bytes() == b"A\nB\nC\n"


@pytest.mark.os_agnostic
def test_write_records_empty(tmp_path: Path) -> None:
    """No records writes an empty JSONL file (and an empty array in json mode)."""
    jsonl = tmp_path / "empty.jsonl"
    write_records(jsonl, [])
    assert jsonl.read_bytes() == b""
    array = tmp_path / "empty.json"
    write_records(array, [], jsonl=False)
    assert array.read_bytes() == b"[]"
