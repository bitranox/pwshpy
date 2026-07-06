"""CLI record output: JSON, JSONL, and human-table rendering."""

from __future__ import annotations

import json

import pytest

from pwshpy.adapters.cli.output import RecordFormat, emit
from pwshpy.domain.enums import ProcessStatus
from pwshpy.domain.records import ProcessInfo

_RECORDS = [
    ProcessInfo(pid=1, name="init", status=ProcessStatus.SLEEPING),
    ProcessInfo(pid=2, name="svc", status=ProcessStatus.RUNNING),
]


@pytest.mark.os_agnostic
def test_jsonl_emits_one_object_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    """JSONL writes exactly one JSON object per line."""
    emit(_RECORDS, RecordFormat.JSONL)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["pid"] == 1
    assert json.loads(lines[1])["status"] == "running"


@pytest.mark.os_agnostic
def test_json_emits_a_single_array(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON writes one parseable array of records."""
    emit(_RECORDS, RecordFormat.JSON)
    parsed = json.loads(capsys.readouterr().out)
    assert [row["pid"] for row in parsed] == [1, 2]


@pytest.mark.os_agnostic
def test_human_table_contains_headers_and_values(capsys: pytest.CaptureFixture[str]) -> None:
    """The human table renders column headers and row values."""
    emit(_RECORDS, RecordFormat.HUMAN)
    out = capsys.readouterr().out
    assert "pid" in out
    assert "init" in out


@pytest.mark.os_agnostic
def test_human_table_handles_empty_input(capsys: pytest.CaptureFixture[str]) -> None:
    """An empty record stream renders a friendly notice, not a crash."""
    emit([], RecordFormat.HUMAN)
    assert "No records" in capsys.readouterr().out


@pytest.mark.os_agnostic
def test_human_table_soft_caps_and_hints(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beyond the row cap the table truncates and points at the streaming path."""
    import pwshpy.adapters.cli.output as output_mod

    monkeypatch.setattr(output_mod, "_MAX_TABLE_ROWS", 2)
    records = [ProcessInfo(pid=n, name=f"p{n}") for n in range(5)]
    emit(records, RecordFormat.HUMAN)
    out = capsys.readouterr().out
    assert "showing the first 2 rows" in out
    assert "--jsonl" in out


@pytest.mark.os_agnostic
def test_human_table_no_hint_when_within_cap(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At or below the cap, no truncation hint is printed."""
    import pwshpy.adapters.cli.output as output_mod

    monkeypatch.setattr(output_mod, "_MAX_TABLE_ROWS", 5)
    records = [ProcessInfo(pid=n, name=f"p{n}") for n in range(3)]
    emit(records, RecordFormat.HUMAN)
    assert "showing the first" not in capsys.readouterr().out
