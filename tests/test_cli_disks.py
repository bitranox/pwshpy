"""CLI story: ``pwshpy disks`` output modes and option handling."""

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
    return cli_runner.invoke(cli_mod.cli, ["disks", *args], obj=production_factory)


@pytest.mark.os_agnostic
def test_disks_jsonl_streams_records(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """--jsonl emits one JSON object per filesystem and exits 0."""
    result = _invoke(cli_runner, production_factory, ["--jsonl"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.startswith("{")]
    assert lines
    assert "mountpoint" in json.loads(lines[0])


@pytest.mark.os_agnostic
def test_disks_human_default(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """Default (human table) output renders and exits 0.

    Header text is not asserted: rich truncates long headers to fit an 80-column
    non-tty. Content correctness is covered by the JSONL test.
    """
    result = _invoke(cli_runner, production_factory, ["--limit", "3"])
    assert result.exit_code == 0
    assert result.output.strip()


@pytest.mark.os_agnostic
def test_disks_json_and_jsonl_are_mutually_exclusive(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """Passing both --json and --jsonl is a usage error (exit 2)."""
    result = _invoke(cli_runner, production_factory, ["--json", "--jsonl"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
