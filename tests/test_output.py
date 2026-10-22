"""CLI record output: JSON, JSONL, and human-table rendering."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from pwshpy.adapters import cli as cli_mod
from pwshpy.adapters.cli import output as output_mod
from pwshpy.adapters.cli.output import (
    _MAX_PIPED_WIDTH,  # pyright: ignore[reportPrivateUsage] - the ceiling these tests measure against
    RecordFormat,
    emit,
)
from pwshpy.domain.enums import ProcessStatus
from pwshpy.domain.records import EnvVar, ProcessInfo

if TYPE_CHECKING:
    from pwshpy.composition import AppServices

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
    monkeypatch.setattr(output_mod, "_MAX_TABLE_ROWS", 5)
    records = [ProcessInfo(pid=n, name=f"p{n}") for n in range(3)]
    emit(records, RecordFormat.HUMAN)
    assert "showing the first" not in capsys.readouterr().out


class _SourceFailedError(Exception):
    """Test-local stand-in for a record source failing mid-stream."""


def _raises_before_first_record() -> Iterator[ProcessInfo]:
    raise _SourceFailedError("source refused before its first record")
    yield ProcessInfo(pid=0, name="unreachable")  # pragma: no cover - makes this a generator


def _raises_after_one_record() -> Iterator[ProcessInfo]:
    yield ProcessInfo(pid=1, name="init")
    raise _SourceFailedError("source failed after one record")


@pytest.mark.os_agnostic
def test_json_writes_nothing_when_the_source_fails_before_its_first_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A source failing before its first record leaves stdout empty, never a bare ``[``."""
    with pytest.raises(_SourceFailedError):
        emit(_raises_before_first_record(), RecordFormat.JSON)
    assert capsys.readouterr().out == ""


@pytest.mark.os_agnostic
def test_json_leaves_a_partial_array_when_the_source_fails_after_records_streamed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The documented streaming trade-off: records already written stay, the array stays open."""
    with pytest.raises(_SourceFailedError):
        emit(_raises_after_one_record(), RecordFormat.JSON)
    out = capsys.readouterr().out
    assert out.startswith('[{"pid":1,')
    assert not out.rstrip().endswith("]")


@pytest.mark.os_agnostic
def test_json_of_an_empty_source_is_still_an_empty_array(capsys: pytest.CaptureFixture[str]) -> None:
    """Pulling the first record ahead keeps an empty source rendering as ``[]``."""
    emit(iter(()), RecordFormat.JSON)
    assert capsys.readouterr().out == "[]\n"


# --- piped human table: no cell is cut off a terminal ([84]) ---------------------------------------

#: Longer than the 80 columns rich falls back to off a terminal, and free of spaces, so a wrap
#: inside the cell or a cut with an ellipsis both break it as one contiguous substring.
_LONG_VALUE = "S-1-5-21-" + "-".join(str(n) * 10 for n in range(1, 9))
_LONG_SPACED_VALUE = " ".join(f"word{n:02d}" for n in range(16))


def _invoke_env_table(cli_runner: CliRunner, production_factory: Callable[[], AppServices]) -> str:
    result = cli_runner.invoke(cli_mod.cli, ["env"], obj=production_factory)
    assert result.exit_code == 0, result.output
    return result.stdout


@pytest.mark.os_agnostic
def test_piped_table_keeps_a_long_value_whole(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off a terminal with no COLUMNS, a value wider than 80 columns appears whole, not cut."""
    assert len(_LONG_VALUE) > 80
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setenv("ZZPWSHPY_X15_PROBE", _LONG_VALUE)
    assert _LONG_VALUE in _invoke_env_table(cli_runner, production_factory)


@pytest.mark.os_agnostic
def test_piped_table_keeps_a_spaced_long_value_on_one_line(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long value with spaces is not word-wrapped inside its cell either, so a grep finds it."""
    assert len(_LONG_SPACED_VALUE) > 80
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setenv("ZZPWSHPY_X15_SPACED", _LONG_SPACED_VALUE)
    lines = [
        line for line in _invoke_env_table(cli_runner, production_factory).splitlines() if "ZZPWSHPY_X15_SPACED" in line
    ]
    assert len(lines) == 1
    assert _LONG_SPACED_VALUE in lines[0]


@pytest.mark.os_agnostic
def test_piped_table_honours_an_explicit_columns(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
    monkeypatch: pytest.MonkeyPatch,
    strip_ansi: Callable[[str], str],
) -> None:
    """A COLUMNS the user set still decides the width: no line is wider, the long value is cut."""
    monkeypatch.setenv("COLUMNS", "60")
    monkeypatch.setenv("ZZPWSHPY_X15_PROBE", _LONG_VALUE)
    out = strip_ansi(_invoke_env_table(cli_runner, production_factory))
    assert max(len(line) for line in out.splitlines()) <= 60
    assert _LONG_VALUE not in out


@pytest.mark.os_agnostic
def test_piped_table_hint_stays_on_one_line_for_a_narrow_table(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sizing to the content never narrows the console below its default, so the hint does not wrap."""
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(output_mod, "_MAX_TABLE_ROWS", 1)
    emit([EnvVar(name="A", value="1"), EnvVar(name="B", value="2")], RecordFormat.HUMAN)
    assert "use --jsonl for the full stream, or --limit N." in capsys.readouterr().out


#: Distinct, space-free and longer than the piped-width ceiling, so it must fold; the dashes keep a
#: lost or doubled character visible in the substring check.
_BEYOND_CEILING_VALUE = "".join(f"{n:07d}-" for n in range(2 * _MAX_PIPED_WIDTH // 8))


def _emit_piped_table(capsys: pytest.CaptureFixture[str], records: list[EnvVar]) -> list[str]:
    emit(records, RecordFormat.HUMAN)
    return capsys.readouterr().out.splitlines()


@pytest.mark.os_agnostic
def test_piped_table_is_never_wider_than_the_ceiling(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One value beyond the ceiling no longer widens every row: no line exceeds it."""
    monkeypatch.delenv("COLUMNS", raising=False)
    lines = _emit_piped_table(capsys, [EnvVar(name="BIG", value=_BEYOND_CEILING_VALUE), EnvVar(name="A", value="1")])
    assert max(len(line) for line in lines) <= _MAX_PIPED_WIDTH


@pytest.mark.os_agnostic
def test_piped_table_folds_a_value_beyond_the_ceiling_without_losing_a_character(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beyond the ceiling the value folds over several lines but keeps every character, never an ellipsis."""
    monkeypatch.delenv("COLUMNS", raising=False)
    lines = _emit_piped_table(capsys, [EnvVar(name="BIG", value=_BEYOND_CEILING_VALUE)])
    joined = "".join("".join(line.split()).replace("\u2502", "") for line in lines)
    assert _BEYOND_CEILING_VALUE in joined
    assert "\u2026" not in joined


@pytest.mark.os_agnostic
def test_piped_table_keeps_a_path_max_value_on_one_line(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A POSIX PATH_MAX (4096) value is below the ceiling with its row, so it stays whole on one line."""
    monkeypatch.delenv("COLUMNS", raising=False)
    path = "/" + "/".join(f"d{n:05d}" for n in range(4095 // 7))
    path += "x" * (4096 - len(path))
    lines = _emit_piped_table(capsys, [EnvVar(name="LONG_PATH", value=path), EnvVar(name="A", value="1")])
    assert sum(path in line for line in lines) == 1


@pytest.mark.os_agnostic
def test_piped_table_never_cuts_a_neighbour_of_a_folded_value(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value under the ceiling but wider than its narrowed column folds too, never an ellipsis."""
    monkeypatch.delenv("COLUMNS", raising=False)
    # Shorter than the ceiling, longer than the value column left once the table is clamped to it.
    neighbour = "".join(f"{n:07d}+" for n in range(_MAX_PIPED_WIDTH // 8))[: _MAX_PIPED_WIDTH - 4]
    lines = _emit_piped_table(
        capsys, [EnvVar(name="BIG", value=_BEYOND_CEILING_VALUE), EnvVar(name="NEAR", value=neighbour)]
    )
    joined = "".join("".join(line.split()).replace("\u2502", "") for line in lines)
    assert neighbour in joined
    assert "\u2026" not in joined


# --- human table renders record values literally, never as rich markup ([88]) ---------------------

#: Values rich would otherwise read as console markup, an emoji code or a markup escape. Each must
#: come out byte for byte: a closing tag with nothing to close crashed the table (exit 1), the
#: others silently printed something other than the value (a lost tag, an emoji, a lost backslash).
_MARKUP_LIKE_VALUES = [
    "a[/]b",
    "[bold]abc[/bold]",
    "[red]alert[/red]",
    "[link=https://example.invalid/x]click[/link]",
    ":thumbs_up: :smile:",
    "C:\\[x]\\y",
]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("value", _MARKUP_LIKE_VALUES)
def test_env_table_prints_a_markup_like_value_verbatim(
    value: str,
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pwshpy env`` exits 0 and shows a value that looks like rich markup exactly as it is."""
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setenv("ZZPWSHPY_X16_PROBE", value)
    lines = [line for line in _invoke_env_table(cli_runner, production_factory).splitlines() if "ZZPWSHPY_X16" in line]
    assert len(lines) == 1
    assert value in lines[0]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("value", _MARKUP_LIKE_VALUES)
def test_human_table_renders_a_markup_like_value_verbatim(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    """The renderer itself keeps every character of a markup-like cell value."""
    emit([EnvVar(name="PROBE", value=value)], RecordFormat.HUMAN)
    assert value in capsys.readouterr().out


@pytest.mark.os_agnostic
def test_piped_table_sizes_a_markup_like_value_by_its_literal_width(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured as literal text, a long markup-like value is whole on one line, never cut or wrapped."""
    monkeypatch.delenv("COLUMNS", raising=False)
    value = "[link=https://example.invalid/" + _LONG_VALUE + "]" + _LONG_VALUE + "[/link]"
    lines = _emit_piped_table(capsys, [EnvVar(name="LINKISH", value=value), EnvVar(name="A", value="1")])
    assert sum(value in line for line in lines) == 1
