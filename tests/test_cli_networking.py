"""CLI tests for the networking commands (real facade, os_agnostic)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli

if TYPE_CHECKING:
    from pwshpy.composition import AppServices


@pytest.mark.os_agnostic
def test_get_net_adapter_jsonl(cli_runner: CliRunner, production_factory: Callable[[], AppServices]) -> None:
    """get_net_adapter --jsonl streams a NetAdapter per interface."""
    result = cli_runner.invoke(cli, ["get_net_adapter", "--jsonl"], obj=production_factory)
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert rows
    assert all("name" in row and "is_up" in row for row in rows)


@pytest.mark.os_agnostic
def test_get_net_ip_address_jsonl(cli_runner: CliRunner, production_factory: Callable[[], AppServices]) -> None:
    """get_net_ip_address --jsonl includes the loopback address."""
    result = cli_runner.invoke(cli, ["get_net_ip_address", "--jsonl"], obj=production_factory)
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert any(row["address"] in ("127.0.0.1", "::1") for row in rows)
