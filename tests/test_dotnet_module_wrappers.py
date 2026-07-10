"""CLI wrappers for the .NET module cmdlets (get_ad_user, get_az_vm, ...).

Each wrapper is a fixed-cmdlet ``cmdlet`` for the AD / Exchange / Azure long tail. The [full]
engine is patched out, so the test asserts the CLI binds ``-p KEY=VALUE`` and emits the result
as JSON without needing .NET or the PowerShell modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli
from pwshpy.composition import Ps
from pwshpy.domain.records import PSInvocationResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from pwshpy.composition import AppServices

#: (CLI command, facade method) - the six module-cmdlet wrappers, identical in shape.
_WRAPPERS = [
    "get_ad_user",
    "get_ad_group",
    "get_ad_computer",
    "get_mailbox",
    "get_az_vm",
    "get_az_resource_group",
]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("command", _WRAPPERS)
def test_module_wrapper_marshals_params(
    command: str,
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper binds -p KEY=VALUE and emits the cmdlet output as JSON (the .NET engine patched out)."""

    def fake(self: Ps, *, timeout: float | None = None, **params: object) -> PSInvocationResult:
        return PSInvocationResult(output=[{"cmdlet": command, "params": params}])

    monkeypatch.setattr(Ps, command, fake)
    result = cli_runner.invoke(cli, [command, "-p", "Filter=svc*"], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert command in result.stdout
    assert "svc*" in result.stdout


@pytest.mark.os_agnostic
@pytest.mark.parametrize("command", _WRAPPERS)
def test_module_wrapper_help(
    command: str, cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    """Each wrapper is registered and its --help lists the shared -p / --timeout / --streams options."""
    result = cli_runner.invoke(cli, [command, "--help"], obj=production_factory)
    assert result.exit_code == 0
    assert "--param" in result.stdout
    assert "--streams" in result.stdout
