"""Text read/write failures are PwshPyErrors: every fallible native call maps to NativeCallError.

``get_content`` / ``get_content_lines`` and the ``fileio`` writers let ``LookupError`` (an
unknown encoding), ``UnicodeDecodeError`` / ``UnicodeEncodeError`` and ``OSError`` through
raw, breaking the invariant the module docstrings state. A ``ValueError`` that is a usage
error (a NUL in a path) stays a ``ValueError``.
"""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from pwshpy.adapters.native.fileio import write_records, write_text, write_text_stream
from pwshpy.adapters.native.filesystem import NativeFileSystem
from pwshpy.domain.errors import NativeAccessDeniedError, NativeCallError
from pwshpy.domain.records import EnvVar

_FS = NativeFileSystem()
#: A character ASCII cannot encode (built by code point so this file stays ASCII).
_EURO = chr(0x20AC)


def _assert_wrapped(error: BaseException, cause_type: type[BaseException]) -> None:
    """The error is a NativeCallError raised from a native cause of ``cause_type``."""
    assert isinstance(error, NativeCallError)
    assert isinstance(error.__cause__, cause_type)


@pytest.fixture
def latin1_file(tmp_path: Path) -> Path:
    target = tmp_path / "latin1.txt"
    target.write_bytes(b"caf\xe9\n")  # 0xE9 alone is not valid UTF-8
    return target


# --- get_content ---------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_get_content_unknown_encoding_is_native_call_error(latin1_file: Path) -> None:
    with pytest.raises(NativeCallError) as caught:
        _FS.get_content(str(latin1_file), encoding="no-such-codec")
    _assert_wrapped(caught.value, LookupError)


@pytest.mark.os_agnostic
def test_get_content_undecodable_bytes_is_native_call_error(latin1_file: Path) -> None:
    with pytest.raises(NativeCallError) as caught:
        _FS.get_content(str(latin1_file))
    _assert_wrapped(caught.value, UnicodeDecodeError)
    assert "latin1.txt" in str(caught.value)


@pytest.mark.os_agnostic
def test_get_content_reads_with_the_right_encoding(latin1_file: Path) -> None:
    """Negative control: the same file reads cleanly when the encoding is right."""
    assert _FS.get_content(str(latin1_file), encoding="latin-1") == "caf\xe9\n"


@pytest.mark.os_agnostic
def test_get_content_nul_in_path_stays_a_usage_value_error(tmp_path: Path) -> None:
    """A NUL in the path is a caller error, not a native failure: it stays ValueError."""
    with pytest.raises(ValueError, match="null") as caught:
        _FS.get_content(str(tmp_path / "a\x00b"))
    assert not isinstance(caught.value, NativeCallError)


# --- get_content_lines ---------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_get_content_lines_unknown_encoding_raises_at_the_call(latin1_file: Path) -> None:
    with pytest.raises(NativeCallError) as caught:
        _FS.get_content_lines(str(latin1_file), encoding="no-such-codec")
    _assert_wrapped(caught.value, LookupError)


@pytest.mark.os_agnostic
def test_get_content_lines_undecodable_line_raises_on_iteration(latin1_file: Path) -> None:
    lines = _FS.get_content_lines(str(latin1_file))
    with pytest.raises(NativeCallError) as caught:
        list(lines)
    _assert_wrapped(caught.value, UnicodeDecodeError)


# --- write_text / write_text_stream ----------------------------------------------------------


@pytest.mark.os_agnostic
def test_write_text_unknown_encoding_is_native_call_error(tmp_path: Path) -> None:
    with pytest.raises(NativeCallError) as caught:
        write_text(tmp_path / "x.txt", "hi", encoding="no-such-codec")
    _assert_wrapped(caught.value, LookupError)


@pytest.mark.os_agnostic
def test_write_text_binary_codec_is_native_call_error(tmp_path: Path) -> None:
    """base64 is a codec but not a text encoding: refused as a LookupError cause."""
    with pytest.raises(NativeCallError) as caught:
        write_text(tmp_path / "x.txt", "hi", encoding="base64")
    _assert_wrapped(caught.value, LookupError)


@pytest.mark.os_agnostic
def test_write_text_unencodable_text_is_native_call_error_and_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    with pytest.raises(NativeCallError) as caught:
        write_text(target, "price: " + _EURO, encoding="ascii")
    _assert_wrapped(caught.value, UnicodeEncodeError)
    assert not target.exists()


@pytest.mark.os_agnostic
def test_write_text_into_a_missing_directory_is_native_call_error(tmp_path: Path) -> None:
    with pytest.raises(NativeCallError) as caught:
        write_text(tmp_path / "missing" / "x.txt", "hi")
    _assert_wrapped(caught.value, FileNotFoundError)


@pytest.mark.os_agnostic
@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX mode bit makes the directory unwritable")
def test_write_text_denied_is_native_access_denied(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if _is_root():
            pytest.skip("root ignores the directory's write bit")
        with pytest.raises(NativeAccessDeniedError) as caught:
            write_text(locked / "x.txt", "hi")
        assert caught.value.errno == errno.EACCES
    finally:
        locked.chmod(0o700)


def _is_root() -> bool:
    if sys.platform == "win32":  # pyright narrows on sys.platform; os.geteuid is POSIX-only
        return False
    return os.geteuid() == 0


@pytest.mark.os_agnostic
def test_write_text_stream_unknown_encoding_is_native_call_error(tmp_path: Path) -> None:
    with pytest.raises(NativeCallError) as caught:
        write_text_stream(tmp_path / "x.txt", ["hi\n"], encoding="no-such-codec")
    _assert_wrapped(caught.value, LookupError)


@pytest.mark.os_agnostic
def test_write_text_stream_unencodable_chunk_is_native_call_error(tmp_path: Path) -> None:
    with pytest.raises(NativeCallError) as caught:
        write_text_stream(tmp_path / "x.txt", ["ok\n", _EURO + "\n"], encoding="ascii")
    _assert_wrapped(caught.value, UnicodeEncodeError)


@pytest.mark.os_agnostic
def test_write_text_stream_into_a_missing_directory_is_native_call_error(tmp_path: Path) -> None:
    with pytest.raises(NativeCallError) as caught:
        write_text_stream(tmp_path / "missing" / "x.txt", ["hi\n"])
    _assert_wrapped(caught.value, FileNotFoundError)


def _undecodable_source() -> Iterator[str]:
    yield "first\n"
    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


@pytest.mark.os_agnostic
def test_write_text_stream_lets_a_decode_error_from_the_source_through(tmp_path: Path) -> None:
    """A decode failure of the CALLER's chunks is not a write failure: the CLI maps it itself."""
    target = tmp_path / "x.txt"
    with pytest.raises(UnicodeDecodeError):
        write_text_stream(target, _undecodable_source())
    assert target.read_bytes() == b"first\n"


# --- write_records -----------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_write_records_into_a_missing_directory_is_native_call_error(tmp_path: Path) -> None:
    with pytest.raises(NativeCallError) as caught:
        write_records(tmp_path / "missing" / "r.jsonl", [EnvVar(name="A", value="1")])
    _assert_wrapped(caught.value, FileNotFoundError)


def _records_then_native_failure() -> Iterator[EnvVar]:
    yield EnvVar(name="A", value="1")
    raise NativeAccessDeniedError("source refused")


@pytest.mark.os_agnostic
def test_write_records_keeps_a_source_native_error_unchanged(tmp_path: Path) -> None:
    """A pwshpy error from the record source passes through as is, never re-wrapped."""
    with pytest.raises(NativeAccessDeniedError, match=r"^source refused$"):
        write_records(tmp_path / "r.jsonl", _records_then_native_failure())
