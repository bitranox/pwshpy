"""CLI story: ``pwshpy uptime`` output modes."""

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
    return cli_runner.invoke(cli_mod.cli, ["uptime", *args], obj=production_factory)


@pytest.mark.os_agnostic
def test_uptime_jsonl_emits_single_record(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """--jsonl emits exactly one JSON record with boot_time and uptime_seconds."""
    result = _invoke(cli_runner, production_factory, ["--jsonl"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.startswith("{")]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "boot_time" in record
    assert record["uptime_seconds"] >= 0.0


@pytest.mark.os_agnostic
def test_uptime_human_default_exits_zero(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """Default (human) output exits 0."""
    result = _invoke(cli_runner, production_factory, [])
    assert result.exit_code == 0
    assert result.output.strip()
