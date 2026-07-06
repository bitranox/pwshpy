"""CLI story: ``pwshpy connections`` output modes and option handling."""

from __future__ import annotations

import json
import socket
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
    return cli_runner.invoke(cli_mod.cli, ["connections", *args], obj=production_factory)


@pytest.mark.os_agnostic
def test_connections_jsonl_streams_valid_records(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """--jsonl emits parseable JSON objects and exits 0, even if the set is empty."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        result = _invoke(cli_runner, production_factory, ["--jsonl"])
    assert result.exit_code == 0
    for line in result.output.splitlines():
        if line.startswith("{"):
            assert "local_port" in json.loads(line)


@pytest.mark.os_agnostic
def test_connections_json_and_jsonl_are_mutually_exclusive(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """Passing both --json and --jsonl is a usage error (exit 2)."""
    result = _invoke(cli_runner, production_factory, ["--json", "--jsonl"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


@pytest.mark.os_agnostic
def test_connections_human_default_exits_zero(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """Default (human) output exits 0."""
    result = _invoke(cli_runner, production_factory, ["--limit", "5"])
    assert result.exit_code == 0
