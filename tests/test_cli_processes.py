"""CLI story: ``pwshpy processes`` output modes and option handling."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner, Result

from pwshpy.adapters import cli as cli_mod

if TYPE_CHECKING:
    from pwshpy.composition import AppServices


def _invoke(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
    args: list[str],
) -> Result:
    return cli_runner.invoke(cli_mod.cli, ["get_process", *args], obj=production_factory)


@pytest.mark.os_agnostic
def test_processes_jsonl_streams_records(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """--jsonl emits one JSON object per line and exits 0."""
    result = _invoke(cli_runner, production_factory, ["--jsonl", "--limit", "3"])
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().splitlines() if line.startswith("{")]
    assert lines
    for line in lines:
        assert "pid" in json.loads(line)


@pytest.mark.os_agnostic
def test_processes_json_emits_array(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """--json emits a parseable JSON array."""
    result = _invoke(cli_runner, production_factory, ["--json", "--limit", "2"])
    assert result.exit_code == 0
    # CliRunner mixes the stderr log line into the output; the JSON array is the
    # line beginning with "[{" (in real runs logs go to stderr, stdout is clean).
    payload = next(line for line in result.output.splitlines() if line.startswith("[{"))
    parsed: list[object] = json.loads(payload)
    assert isinstance(parsed, list)
    assert len(parsed) <= 2


@pytest.mark.os_agnostic
def test_processes_human_default(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """Default output is a human table with a pid column."""
    result = _invoke(cli_runner, production_factory, ["--limit", "2"])
    assert result.exit_code == 0
    assert "pid" in result.output


@pytest.mark.os_agnostic
def test_processes_json_and_jsonl_are_mutually_exclusive(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """Passing both --json and --jsonl is a usage error (exit 2)."""
    result = _invoke(cli_runner, production_factory, ["--json", "--jsonl"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


@pytest.mark.os_agnostic
def test_processes_limit_bounds_count(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """--limit caps the number of emitted records."""
    result = _invoke(cli_runner, production_factory, ["--jsonl", "--limit", "1"])
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().splitlines() if line.startswith("{")]
    assert len(lines) == 1
