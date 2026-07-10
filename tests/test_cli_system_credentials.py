"""CLI body coverage for system / credentials / .NET / reboot wrappers.

Portable commands (get_computer_info, stop/wait_process, exec) run against the REAL
facade. Environment-gated ones (get_hotfix -> WMI, credentials -> OS vault, run/cmdlet/
get_command -> [full], restart/stop_computer -> a real reboot) are covered by patching
the ``Ps`` method, so the CLI wrapper body runs without the environment - and no reboot,
Windows, or .NET is ever needed.
"""
# subprocess spawns a disposable child under test; the argv is fixed.

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from pwshpy.adapters.cli.root import cli
from pwshpy.composition import Ps
from pwshpy.domain.records import CommandInfo, Credential, Hotfix, PSInvocationResult

if TYPE_CHECKING:
    from pwshpy.composition import AppServices

_SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


# --- portable commands: real facade ------------------------------------------------------------


@pytest.mark.os_agnostic
def test_get_computer_info_cli(cli_runner: CliRunner, production_factory: Callable[[], AppServices]) -> None:
    """get_computer_info --jsonl emits a populated record."""
    result = cli_runner.invoke(cli, ["get_computer_info", "--jsonl"], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert '"hostname"' in result.stdout


@pytest.mark.os_agnostic
def test_stop_and_wait_process_cli(cli_runner: CliRunner, production_factory: Callable[[], AppServices]) -> None:
    """stop_process kills a spawned child; wait_process reports its exit."""
    child = subprocess.Popen(_SLEEP)  # noqa: S603 - fixed argv, disposable child
    try:
        stopped = cli_runner.invoke(cli, ["stop_process", str(child.pid), "-f"], obj=production_factory)
        assert stopped.exit_code == 0, stopped.output
        assert "stopped" in stopped.output
        waited = cli_runner.invoke(cli, ["wait_process", str(child.pid)], obj=production_factory)
        assert waited.exit_code == 0, waited.output
        assert "exited" in waited.output
    finally:
        child.wait()


@pytest.mark.os_agnostic
def test_exec_json_and_stderr_paths(cli_runner: CliRunner, production_factory: Callable[[], AppServices]) -> None:
    """exec --json emits the ProcessResult; the plain path echoes a child's stderr."""
    as_json = cli_runner.invoke(cli, ["exec", "--json", "--", sys.executable, "-c", "print(1)"], obj=production_factory)
    assert as_json.exit_code == 0, as_json.output
    assert '"exit_code":0' in as_json.stdout.replace(" ", "")

    stderr_run = cli_runner.invoke(
        cli, ["exec", "--", sys.executable, "-c", "import sys; sys.stderr.write('boom')"], obj=production_factory
    )
    assert stderr_run.exit_code == 0
    assert "boom" in (stderr_run.stderr or stderr_run.output)


# --- environment-gated commands: patch the Ps method so the CLI body still runs ----------------


@pytest.mark.os_agnostic
def test_get_hotfix_cli(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_hotfix CLI emits the records the facade yields (WMI patched out)."""

    def fake_get_hotfix(self: Ps) -> list[Hotfix]:
        return [Hotfix(hotfix_id="KB5001")]

    monkeypatch.setattr(Ps, "get_hotfix", fake_get_hotfix)
    result = cli_runner.invoke(cli, ["get_hotfix", "--jsonl"], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert "KB5001" in result.stdout


@pytest.mark.os_agnostic
def test_credential_roundtrip_cli(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], monkeypatch: pytest.MonkeyPatch
) -> None:
    """save (hidden prompt) / load (masked) / delete credential CLI bodies run; missing load fails."""
    vault: dict[str, Credential] = {}

    def fake_save(self: Ps, target: str, username: str, secret: str) -> Credential:
        cred = Credential(target=target, username=username, secret=SecretStr(secret))
        vault[target] = cred
        return cred

    def fake_load(self: Ps, target: str) -> Credential | None:
        return vault.get(target)

    def fake_delete(self: Ps, target: str) -> None:
        vault.pop(target, None)

    monkeypatch.setattr(Ps, "save_credential", fake_save)
    monkeypatch.setattr(Ps, "load_credential", fake_load)
    monkeypatch.setattr(Ps, "delete_credential", fake_delete)

    saved = cli_runner.invoke(cli, ["save_credential", "db", "svc"], input="pw\npw\n", obj=production_factory)
    assert saved.exit_code == 0, saved.output
    assert "saved credential for db" in saved.output

    loaded = cli_runner.invoke(cli, ["load_credential", "db", "--jsonl"], obj=production_factory)
    assert loaded.exit_code == 0
    assert '"svc"' in loaded.stdout
    assert "pw" not in loaded.stdout  # secret stays masked

    missing = cli_runner.invoke(cli, ["load_credential", "nope"], obj=production_factory)
    assert missing.exit_code != 0
    assert "no credential" in missing.output

    deleted = cli_runner.invoke(cli, ["delete_credential", "db"], obj=production_factory)
    assert deleted.exit_code == 0
    assert "deleted credential for db" in deleted.output


@pytest.mark.os_agnostic
def test_tier_b_command_bodies(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], monkeypatch: pytest.MonkeyPatch
) -> None:
    """run / cmdlet / get_command CLI bodies marshal to JSON (the [full] engine patched out)."""

    def fake_run(self: Ps, script: str, *, timeout: float | None = None) -> list[object]:
        return [{"echo": script}]

    def fake_cmdlet(
        self: Ps, name: str, *args: object, timeout: float | None = None, **params: object
    ) -> PSInvocationResult:
        return PSInvocationResult(output=[name, params])

    def fake_get_command(self: Ps, name: str) -> CommandInfo:
        return CommandInfo(name=name, command_type="Cmdlet")

    monkeypatch.setattr(Ps, "run", fake_run)
    monkeypatch.setattr(Ps, "cmdlet", fake_cmdlet)
    monkeypatch.setattr(Ps, "get_command", fake_get_command)

    run_result = cli_runner.invoke(cli, ["run", "Get-Date"], obj=production_factory)
    assert run_result.exit_code == 0, run_result.output
    assert "Get-Date" in run_result.stdout

    cmdlet_result = cli_runner.invoke(cli, ["cmdlet", "Get-Item", "-p", "Path=C:/"], obj=production_factory)
    assert cmdlet_result.exit_code == 0, cmdlet_result.output
    assert "Get-Item" in cmdlet_result.stdout
    assert "C:/" in cmdlet_result.stdout

    command_result = cli_runner.invoke(cli, ["get_command", "Get-Item", "--jsonl"], obj=production_factory)
    assert command_result.exit_code == 0
    assert '"Get-Item"' in command_result.stdout


@pytest.mark.os_agnostic
@pytest.mark.parametrize("command", ["restart_computer", "stop_computer"])
def test_reboot_commands_run_with_yes(
    cli_runner: CliRunner,
    production_factory: Callable[[], AppServices],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """With --yes the reboot CLI body calls the facade (patched - it NEVER actually reboots)."""
    calls: list[str] = []

    def fake_restart(self: Ps, *, delay_seconds: int = 0, force: bool = True) -> None:
        calls.append("restart")

    def fake_stop(self: Ps, *, delay_seconds: int = 0, force: bool = True) -> None:
        calls.append("stop")

    monkeypatch.setattr(Ps, "restart_computer", fake_restart)
    monkeypatch.setattr(Ps, "stop_computer", fake_stop)
    result = cli_runner.invoke(cli, [command, "--yes"], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert "scheduled" in result.output
    assert calls  # the (patched) facade method was reached
