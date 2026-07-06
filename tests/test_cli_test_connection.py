"""CLI story: ``pwshpy test-connection HOST`` output and options."""

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
    return cli_runner.invoke(cli_mod.cli, ["test-connection", *args], obj=production_factory)


@pytest.mark.os_agnostic
def test_reachable_socket_reports_true(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """Probing a listening port emits reachable=true and exits 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        result = _invoke(cli_runner, production_factory, ["127.0.0.1", "-p", str(port), "--timeout", "2", "--jsonl"])
    assert result.exit_code == 0
    line = next(line for line in result.output.splitlines() if line.startswith("{"))
    assert json.loads(line)["reachable"] is True


@pytest.mark.os_agnostic
def test_missing_host_is_usage_error(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """Omitting HOST is a usage error (exit 2)."""
    result = _invoke(cli_runner, production_factory, [])
    assert result.exit_code == 2
