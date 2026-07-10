"""Lock the structural wins pwshpy has over PowerShell (see the README "Power Tools").

These guard behaviors PowerShell gets wrong and pwshpy gets right by construction:
single-item collection collapse (a one-element pipeline becomes a scalar in PS), the
``$null``/comparison operand-order trap (``$x -eq $null`` filters a collection instead
of testing it), and JSON output shape for 0/1 rows. They are cheap and encode a
promise the docs make, so they earn their keep.
"""

from __future__ import annotations

import json

import pytest

from pwshpy.adapters.cli.output import RecordFormat, emit
from pwshpy.domain.pipeline import Pipeline
from pwshpy.domain.records import EnvVar, ProcessInfo, ProcessResult


@pytest.mark.os_agnostic
def test_to_list_is_a_real_list_for_zero_one_and_many() -> None:
    """to_list() is always a real list of the right length - never a scalar for one item.

    PowerShell unwraps a single-element pipeline to a bare object (``.Count`` == 1,
    ``foreach`` iterates a string's characters); pwshpy never collapses.
    """
    assert Pipeline(iter([])).to_list() == []
    one = Pipeline(iter([EnvVar(name="A", value="1")])).to_list()
    assert isinstance(one, list)
    assert len(one) == 1
    many = Pipeline(iter([EnvVar(name="A", value="1"), EnvVar(name="B", value="2")])).to_list()
    assert isinstance(many, list)
    assert len(many) == 2


@pytest.mark.os_agnostic
def test_where_is_boolean_strict_over_collection_fields() -> None:
    """where() uses a plain Python predicate: a collection-valued field is no trap.

    In PowerShell a comparison against a collection LHS *filters* rather than returns a
    bool, and operand order matters. Here the predicate is ordinary Python truthiness.
    """
    records = [
        ProcessResult(argv=["a"], exit_code=0),
        ProcessResult(argv=["a", "b", "c"], exit_code=1),
    ]
    multi_arg = Pipeline(iter(records)).where(lambda r: len(r.argv) > 1).to_list()
    assert [r.exit_code for r in multi_arg] == [1]


@pytest.mark.os_agnostic
def test_is_none_predicate_is_a_real_test_not_a_filter() -> None:
    """A ``field is None`` predicate is an honest boolean, never the $null operand-order trap."""
    procs = [ProcessInfo(pid=1, name="x"), ProcessInfo(pid=2, name="y", username="alice")]
    no_user = Pipeline(iter(procs)).where(lambda p: p.username is None).to_list()
    assert [p.pid for p in no_user] == [1]


@pytest.mark.os_agnostic
def test_json_output_is_an_array_even_for_zero_and_one_rows(capsys: pytest.CaptureFixture[str]) -> None:
    """--json emits a real JSON array for 0 and 1 rows, not nothing / a bare object."""
    emit([], RecordFormat.JSON)
    assert capsys.readouterr().out.strip() == "[]"

    emit([EnvVar(name="A", value="1")], RecordFormat.JSON)
    out = capsys.readouterr().out.strip()
    assert out.startswith("[")
    assert out.endswith("]")
    assert json.loads(out) == [{"name": "A", "value": "1"}]


@pytest.mark.os_agnostic
def test_jsonl_single_row_is_one_line_object(capsys: pytest.CaptureFixture[str]) -> None:
    """--jsonl of one record is exactly one JSON-object line (no scalar collapse)."""
    emit([EnvVar(name="A", value="1")], RecordFormat.JSONL)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"name": "A", "value": "1"}
