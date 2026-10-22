"""JSON output is UTF-8 bytes whatever the console code page or PYTHONIOENCODING says.

JSON is UTF-8 by spec (RFC 8259), so a consumer reading ``pwshpy ... --jsonl`` from a pipe
decodes it as UTF-8. On a German Windows box the text-mode stdout encodes in cp1252, which
turned every non-ASCII field into mojibake (or a ``UnicodeEncodeError`` for a character
cp1252 cannot represent). These tests force a non-UTF-8 stdout and read back the BYTES.

Non-ASCII values are built with chr() so the source stays ASCII.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

import orjson
import pytest

from pwshpy.adapters.cli.output import RecordFormat, emit, emit_json
from pwshpy.domain.records import EnvVar

#: Representable in cp1252 (so the old code produced mojibake rather than an error).
_GERMAN = "Ger" + chr(0xE4) + "t " + chr(0xFC) + " " + chr(0xDF)
#: Not representable in cp1252 (the old code raised UnicodeEncodeError mid-stream).
_BEYOND_CP1252 = chr(0x4E2D) + chr(0x6587) + " " + chr(0x2603)


def _cp1252_stdout(monkeypatch: pytest.MonkeyPatch) -> io.BytesIO:
    """Replace sys.stdout with a strict cp1252 text stream over a byte sink; return the sink."""
    sink = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(sink, encoding="cp1252", errors="strict", newline="\n"))
    return sink


@pytest.mark.os_agnostic
@pytest.mark.parametrize("value", [_GERMAN, _BEYOND_CP1252], ids=["cp1252-representable", "beyond-cp1252"])
def test_jsonl_is_utf8_on_a_cp1252_stdout(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Each JSONL line decodes as UTF-8 to the original non-ASCII value."""
    sink = _cp1252_stdout(monkeypatch)
    emit([EnvVar(name="A", value=value), EnvVar(name="B", value="plain")], RecordFormat.JSONL)
    sys.stdout.flush()
    lines = sink.getvalue().decode("utf-8").splitlines()
    assert [orjson.loads(line)["value"] for line in lines] == [value, "plain"]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("value", [_GERMAN, _BEYOND_CP1252], ids=["cp1252-representable", "beyond-cp1252"])
def test_json_array_is_utf8_on_a_cp1252_stdout(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """The streamed JSON array decodes as UTF-8 to the original non-ASCII value."""
    sink = _cp1252_stdout(monkeypatch)
    emit([EnvVar(name="A", value=value), EnvVar(name="B", value="plain")], RecordFormat.JSON)
    sys.stdout.flush()
    parsed = orjson.loads(sink.getvalue().decode("utf-8"))
    assert [row["value"] for row in parsed] == [value, "plain"]


@pytest.mark.os_agnostic
def test_emit_json_is_utf8_on_a_cp1252_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-record JSON output (.NET / web commands) is UTF-8 too."""
    sink = _cp1252_stdout(monkeypatch)
    emit_json({"name": _GERMAN, "other": _BEYOND_CP1252})
    sys.stdout.flush()
    assert orjson.loads(sink.getvalue().decode("utf-8")) == {"name": _GERMAN, "other": _BEYOND_CP1252}


@pytest.mark.os_agnostic
def test_empty_json_array_stays_utf8_and_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty source still yields exactly ``[]`` and a newline."""
    sink = _cp1252_stdout(monkeypatch)
    emit([], RecordFormat.JSON)
    sys.stdout.flush()
    assert sink.getvalue() == b"[]\n"


@pytest.mark.os_agnostic
def test_text_written_earlier_stays_ahead_of_the_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unflushed text already on stdout is written before the JSON bytes, not after."""
    sink = _cp1252_stdout(monkeypatch)
    sys.stdout.write("before\n")
    emit([EnvVar(name="A", value=_GERMAN)], RecordFormat.JSONL)
    sys.stdout.flush()
    first, second = sink.getvalue().decode("utf-8").splitlines()
    assert first == "before"
    assert orjson.loads(second)["value"] == _GERMAN


@pytest.mark.os_agnostic
def test_ascii_jsonl_bytes_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ASCII-only record is written byte for byte as orjson renders it, LF-terminated."""
    sink = _cp1252_stdout(monkeypatch)
    record = EnvVar(name="PATH", value="/usr/bin")
    emit([record], RecordFormat.JSONL)
    sys.stdout.flush()
    assert sink.getvalue() == orjson.dumps(record.to_dict()) + b"\n"


@pytest.mark.os_agnostic
@pytest.mark.parametrize("flag", ["--jsonl", "--json"])
def test_cli_pipe_is_utf8_under_a_cp1252_io_encoding(flag: str) -> None:
    """End to end: ``pwshpy env`` piped with PYTHONIOENCODING=cp1252 still emits UTF-8."""
    # env= REPLACES the environment, so start from the full one (Windows needs SystemRoot).
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0", "PWSHPY_X8_PROBE": _GERMAN}
    completed = subprocess.run(  # noqa: S603 - fixed argv: this interpreter running pwshpy itself
        [sys.executable, "-m", "pwshpy", "env", flag],
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    text = completed.stdout.decode("utf-8")
    rows = [orjson.loads(line) for line in text.splitlines()] if flag == "--jsonl" else orjson.loads(text)
    assert {row["name"]: row["value"] for row in rows}["PWSHPY_X8_PROBE"] == _GERMAN
