"""CLI: ``pwshpy invoke_cim_method`` - the read-only CIM method call, end to end through production wiring."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli
from pwshpy.domain.errors import CimMethodNotAllowedError

if TYPE_CHECKING:
    from pwshpy.composition import AppServices

_SECRET = "{SECRET-PROTECTOR-ID-0000}"


@pytest.mark.os_agnostic
def test_invoke_cim_method_is_a_top_level_command(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    result = cli_runner.invoke(cli, ["--help"], obj=production_factory)
    assert result.exit_code == 0
    assert "invoke_cim_method" in result.output


@pytest.mark.os_agnostic
def test_a_method_off_the_allowlist_fails_naming_it(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    """Refused on any OS, before WMI: the allowlist check needs no Windows."""
    result = cli_runner.invoke(
        cli, ["invoke_cim_method", "Win32_Process", "Terminate", "--jsonl"], obj=production_factory
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CimMethodNotAllowedError)
    assert "Win32_Process.Terminate" in str(result.exception)


@pytest.mark.os_agnostic
def test_a_malformed_arg_is_a_usage_error_that_does_not_repeat_it(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    result = cli_runner.invoke(
        cli,
        [
            "invoke_cim_method",
            "Win32_EncryptableVolume",
            "GetKeyProtectorType",
            "--namespace",
            "root/cimv2/Security/MicrosoftVolumeEncryption",
            "--arg",
            _SECRET,
        ],
        obj=production_factory,
    )
    assert result.exit_code == 2
    assert "NAME=VALUE" in result.output
    assert _SECRET not in result.output


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="WMI is Windows-only")
def test_the_owner_of_this_process_streams_as_jsonl(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    result = cli_runner.invoke(
        cli,
        ["invoke_cim_method", "Win32_Process", "GetOwner", "--where", f"ProcessId = {os.getpid()}", "--jsonl"],
        obj=production_factory,
    )
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert len(rows) == 1
    assert rows[0]["method_name"] == "GetOwner"
    assert rows[0]["return_value"] == 0
    assert str(rows[0]["out_parameters"]["User"]).lower() == os.environ["USERNAME"].lower()
