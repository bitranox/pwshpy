"""CLI tests for the newly-exposed power commands: exec, write_text, reboot guards, .NET guard."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli

if TYPE_CHECKING:
    from pwshpy.composition import AppServices


@pytest.mark.os_agnostic
def test_exec_runs_and_exits_with_child_code(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    """`exec -- <python> -c print(1)` streams stdout and exits 0."""
    result = cli_runner.invoke(cli, ["exec", "--", sys.executable, "-c", "print(1)"], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert "1" in result.output


@pytest.mark.os_agnostic
def test_write_text_writes_utf8_no_bom(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """write_text consumes stdin and writes UTF-8 with no BOM by default."""
    target = tmp_path / "out.txt"
    result = cli_runner.invoke(cli, ["write_text", str(target)], input="grüße", obj=production_factory)
    assert result.exit_code == 0, result.output
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # no BOM
    assert raw == "grüße".encode()


@pytest.mark.os_agnostic
@pytest.mark.parametrize("command", ["restart_computer", "stop_computer"])
def test_reboot_commands_refuse_without_yes(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], command: str
) -> None:
    """The destructive machine commands refuse (nonzero, no action) unless --yes is given."""
    result = cli_runner.invoke(cli, [command], obj=production_factory)
    assert result.exit_code != 0
    assert "refus" in result.output.lower()


@pytest.mark.os_agnostic
def test_run_without_full_extra_exits_nonzero(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    """Without the [full] extra, `run` surfaces the .NET guard as a nonzero exit."""
    from pwshpy.adapters.powershell import is_available

    if is_available():
        pytest.skip("[full] extra is installed; guard path not exercised")
    result = cli_runner.invoke(cli, ["run", "Get-Date"], obj=production_factory)
    assert result.exit_code != 0
