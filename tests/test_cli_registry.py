"""CLI: ``pwshpy registry values/keys`` over the pipeline (structural, cross-backend).

Driven against whatever backend ``lib_registry`` uses (real registry on Windows,
``fake_winreg`` minimal registry on Linux/macOS), so the assertions are the
structural CLI contract, deterministic on every OS.  Exact live-Windows values
are pinned in ``test_registry_pwsh_oracle``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli

if TYPE_CHECKING:
    from pwshpy.composition import AppServices

_STABLE_KEY = "HKLM/SOFTWARE/Microsoft/Windows NT/CurrentVersion"
_STABLE_PARENT = "HKLM/SOFTWARE"


def _json_rows(output: str) -> list[dict[str, object]]:
    """Parse the JSONL record lines, ignoring any interleaved log lines."""
    return [json.loads(line) for line in output.splitlines() if line.strip().startswith("{")]


@pytest.mark.os_agnostic
def test_registry_commands_are_top_level(cli_runner: CliRunner, production_factory: Callable[[], AppServices]) -> None:
    """The registry read commands are top-level: ``get_item_property`` and ``registry_keys``."""
    result = cli_runner.invoke(cli, ["--help"], obj=production_factory)
    assert result.exit_code == 0
    assert "get_item_property" in result.output
    assert "registry_keys" in result.output


@pytest.mark.os_agnostic
def test_registry_values_jsonl_streams_records(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    """``registry values KEY --jsonl`` streams one JSON RegistryValue per line."""
    result = cli_runner.invoke(cli, ["get_item_property", _STABLE_KEY, "--jsonl"], obj=production_factory)
    assert result.exit_code == 0, result.output
    rows = _json_rows(result.stdout)
    assert rows, "the CurrentVersion key always has values"
    assert all(row["hive"] == "HKLM" for row in rows)
    assert all("type" in row and "name" in row and "data" in row for row in rows)


@pytest.mark.os_agnostic
def test_registry_keys_jsonl_streams_records(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    """``registry keys KEY --jsonl`` streams one JSON RegistryKey per subkey."""
    result = cli_runner.invoke(cli, ["registry_keys", _STABLE_PARENT, "--jsonl"], obj=production_factory)
    assert result.exit_code == 0, result.output
    rows = _json_rows(result.stdout)
    assert rows, "HKLM\\SOFTWARE always has subkeys"
    assert all(row["hive"] == "HKLM" and row["name"] for row in rows)


@pytest.mark.os_agnostic
def test_registry_values_unknown_hive_exits_nonzero(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    """A key with an unknown hive prefix fails cleanly with a non-zero exit code."""
    result = cli_runner.invoke(cli, ["get_item_property", "BOGUS/x", "--jsonl"], obj=production_factory)
    assert result.exit_code != 0
