"""CLI story: ``pwshpy env`` output modes."""

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
    return cli_runner.invoke(cli_mod.cli, ["env", *args], obj=production_factory)


@pytest.mark.os_agnostic
def test_env_jsonl_streams_records(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--jsonl emits name/value records and exits 0."""
    monkeypatch.setenv("PWSHPY_CLI_ENV_TEST", "x")
    result = _invoke(cli_runner, production_factory, ["--jsonl"])
    assert result.exit_code == 0
    names = {json.loads(line)["name"] for line in result.output.splitlines() if line.startswith("{")}
    assert "PWSHPY_CLI_ENV_TEST" in names


@pytest.mark.os_agnostic
def test_env_json_and_jsonl_are_mutually_exclusive(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """Passing both --json and --jsonl is a usage error (exit 2)."""
    result = _invoke(cli_runner, production_factory, ["--json", "--jsonl"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
