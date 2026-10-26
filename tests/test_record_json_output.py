"""CLI and write_records output for text that is not valid UTF-8: each undecodable byte becomes U+FFFD, plus a warning.

A POSIX environment variable, file name, process name or account name holding a byte that is not
valid UTF-8 reaches Python as a lone surrogate (``os`` decodes with ``surrogateescape``), which
``orjson`` refuses. ``--json`` / ``--jsonl`` and ``ps.write_records`` write each such code point as
U+FFFD instead, keep streaming, and warn once per record naming the record type, its 1-based position
and the field paths - never the value itself.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fake_services import services_with_ps

from pwshpy.adapters.cli.root import cli
from pwshpy.adapters.native.fileio import write_records
from pwshpy.composition import build_production
from pwshpy.domain.pipeline import Pipeline
from pwshpy.domain.records import EnvVar, PSRecord

#: The code point os.environ / os.fsdecode hold for the undecodable byte 0xFF.
_BAD = chr(0xDCFF)
#: What each undecodable byte is written as.
_FFFD = chr(0xFFFD)


class _FakePs:
    """A facade double whose ``environment()`` yields the given records."""

    def __init__(self, records: list[EnvVar]) -> None:
        self._records = records

    def environment(self) -> Pipeline[EnvVar]:
        return Pipeline(iter(self._records))


def _invoke_env(records: list[EnvVar], fmt: str) -> Any:
    services = services_with_ps(_FakePs(records))
    return CliRunner().invoke(cli, ["env", fmt], obj=lambda: services)


def _three_vars() -> list[EnvVar]:
    return [
        EnvVar(name="A", value="1"),
        EnvVar(name="BAD", value="a" + _BAD + "b"),
        EnvVar(name="C", value="3"),
    ]


def _warnings(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("warning:")]


# --- the CLI: --jsonl / --json -------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_cli_jsonl_writes_the_bad_record_as_fffd_and_warns_once() -> None:
    """The stream does not abort: every record prints, exit 0, one warning naming record 2."""
    result = _invoke_env(_three_vars(), "--jsonl")
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows == [
        {"name": "A", "value": "1"},
        {"name": "BAD", "value": "a" + _FFFD + "b"},
        {"name": "C", "value": "3"},
    ]
    assert _warnings(result.stderr) == [
        "warning: EnvVar record 2, field value: not valid UTF-8; each undecodable byte written as U+FFFD"
    ]


@pytest.mark.os_agnostic
def test_cli_json_array_stays_valid_and_warns_once() -> None:
    """--json still prints one parseable array holding every record."""
    result = _invoke_env(_three_vars(), "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[1] == {"name": "BAD", "value": "a" + _FFFD + "b"}
    assert len(json.loads(result.stdout)) == 3
    assert _warnings(result.stderr) == [
        "warning: EnvVar record 2, field value: not valid UTF-8; each undecodable byte written as U+FFFD"
    ]


@pytest.mark.os_agnostic
def test_cli_json_names_the_first_record_as_record_1() -> None:
    """--json pulls the first record separately; it is still counted as record 1."""
    result = _invoke_env([EnvVar(name="BAD", value=_BAD)], "--json")
    assert result.exit_code == 0, result.output
    assert _warnings(result.stderr) == [
        "warning: EnvVar record 1, field value: not valid UTF-8; each undecodable byte written as U+FFFD"
    ]


@pytest.mark.os_agnostic
def test_cli_clean_records_print_no_warning() -> None:
    """No replacement, no warning."""
    result = _invoke_env([EnvVar(name="A", value="1")], "--jsonl")
    assert result.exit_code == 0
    assert _warnings(result.stderr) == []


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="os.environ on Windows cannot hold a byte that is not valid UTF-8")
def test_real_env_with_a_non_utf8_byte_prints_and_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end over the real os.environ: PWSHPY_PROBE_BAD=a<0xFF>b no longer exits 22."""
    monkeypatch.setenv("PWSHPY_PROBE_BAD", "a" + _BAD + "b")
    services = build_production()
    result = CliRunner().invoke(cli, ["env", "--jsonl"], obj=lambda: services)
    assert result.exit_code == 0, result.output
    rows = {row["name"]: row["value"] for row in map(json.loads, result.stdout.splitlines())}
    assert rows["PWSHPY_PROBE_BAD"] == "a" + _FFFD + "b"
    assert any(
        line.endswith("field value: not valid UTF-8; each undecodable byte written as U+FFFD")
        for line in _warnings(result.stderr)
    )


# --- the library: write_records ------------------------------------------------------------------


def _records() -> Iterator[PSRecord]:
    yield EnvVar(name="A", value="1")
    yield EnvVar(name="BAD", value="a" + _BAD + "b")


@pytest.mark.os_agnostic
def test_write_records_jsonl_writes_every_record_and_logs_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The file is complete and valid UTF-8; one WARNING names the record and field."""
    target = tmp_path / "out.jsonl"
    with caplog.at_level(logging.WARNING):
        write_records(target, _records())
    rows = [json.loads(line) for line in target.read_bytes().decode("utf-8").splitlines()]
    assert rows == [{"name": "A", "value": "1"}, {"name": "BAD", "value": "a" + _FFFD + "b"}]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        "EnvVar record 2, field value: not valid UTF-8; each undecodable byte written as U+FFFD"
    ]


@pytest.mark.os_agnostic
def test_write_records_json_array_stays_valid(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """jsonl=False writes one parseable array with the repaired record in place."""
    target = tmp_path / "out.json"
    with caplog.at_level(logging.WARNING):
        write_records(target, _records(), jsonl=False)
    assert json.loads(target.read_bytes())[1] == {"name": "BAD", "value": "a" + _FFFD + "b"}
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
