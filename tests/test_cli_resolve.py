"""CLI story: ``pwshpy resolve NAME`` output and error handling."""

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
    return cli_runner.invoke(cli_mod.cli, ["resolve_dns_name", *args], obj=production_factory)


@pytest.mark.os_agnostic
def test_resolve_localhost_jsonl(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """resolve localhost --jsonl emits DnsRecord objects and exits 0."""
    result = _invoke(cli_runner, production_factory, ["localhost", "--jsonl"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.startswith("{")]
    assert lines
    assert "address" in json.loads(lines[0])


@pytest.mark.os_agnostic
def test_resolve_missing_argument_is_usage_error(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """Omitting NAME is a usage error (exit 2)."""
    result = _invoke(cli_runner, production_factory, [])
    assert result.exit_code == 2


@pytest.mark.os_agnostic
def test_resolve_unresolvable_name_exits_nonzero(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
) -> None:
    """An unresolvable name surfaces as a non-zero exit, not a raw traceback."""
    result = _invoke(cli_runner, production_factory, ["nonexistent.invalid.", "--jsonl"])
    assert result.exit_code != 0
