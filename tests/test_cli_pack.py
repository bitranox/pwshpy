"""CLI tests for the pack / unpack commands (real facade, hermetic tmp paths, os_agnostic)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli

if TYPE_CHECKING:
    from pwshpy.composition import AppServices


def _project(root: Path) -> Path:
    """An entry importing a local submodule, the shape the packer is built for."""
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "helper.py").write_text("VALUE = 1\n")
    entry = root / "app.py"
    entry.write_text("from pkg import helper\nprint(helper.VALUE)\n")
    return entry


@pytest.mark.os_agnostic
def test_pack_writes_the_artefact_and_reports_it(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """pack produces the .ps1 and prints a manifest naming what went in."""
    entry = _project(tmp_path)
    result = cli_runner.invoke(cli, ["pack", str(entry), "--json"], obj=production_factory)
    assert result.exit_code == 0, result.output
    manifest = json.loads(result.stdout)[0]
    assert manifest["entry"] == "app.py"
    assert "pkg/helper.py" in manifest["files"]
    assert Path(manifest["output_path"]).is_file()


@pytest.mark.os_agnostic
def test_pack_honours_the_output_option(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """-o places the artefact where the caller asked."""
    entry = _project(tmp_path)
    target = tmp_path / "dist" / "tool.ps1"
    result = cli_runner.invoke(cli, ["pack", str(entry), "-o", str(target)], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert target.is_file()


@pytest.mark.os_agnostic
def test_pack_format_sh_emits_a_shell_runner(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """--format sh writes a POSIX .sh runner, and unpack reads it straight back."""
    entry = _project(tmp_path)
    target = tmp_path / "app.sh"
    packed = cli_runner.invoke(cli, ["pack", str(entry), "-o", str(target), "--format", "sh"], obj=production_factory)
    assert packed.exit_code == 0, packed.output
    assert target.read_text().startswith("#!/bin/sh")
    out = tmp_path / "restored"
    result = cli_runner.invoke(cli, ["unpack", str(target), "-o", str(out), "--json"], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["files"] == ["app.py", "pkg/__init__.py", "pkg/helper.py"]


@pytest.mark.os_agnostic
def test_pack_with_flag_reaches_the_manifest(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """--with is repeatable and lands in the recorded uv arguments."""
    entry = _project(tmp_path)
    result = cli_runner.invoke(
        cli, ["pack", str(entry), "--with", "rich", "--with", "httpx", "--json"], obj=production_factory
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["uv_args"] == ["--with", "rich", "--with", "httpx"]


@pytest.mark.os_agnostic
def test_pack_warns_when_dependencies_are_undeclared(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """A third-party import with nothing declared anywhere is worth saying out loud."""
    entry = tmp_path / "app.py"
    entry.write_text("import rich\nprint(rich)\n")
    result = cli_runner.invoke(cli, ["pack", str(entry)], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert "declares no dependencies" in result.output
    assert "rich" in result.output


@pytest.mark.os_agnostic
def test_pack_stays_quiet_when_dependencies_are_declared(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """A correctly declared script must not be nagged."""
    entry = tmp_path / "app.py"
    entry.write_text('# /// script\n# dependencies = ["rich"]\n# ///\nimport rich\nprint(rich)\n')
    result = cli_runner.invoke(cli, ["pack", str(entry)], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert "declares no dependencies" not in result.output


@pytest.mark.os_agnostic
def test_pack_refuses_to_overwrite_without_force(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """The second pack fails; --force makes it succeed."""
    entry = _project(tmp_path)
    assert cli_runner.invoke(cli, ["pack", str(entry)], obj=production_factory).exit_code == 0
    again = cli_runner.invoke(cli, ["pack", str(entry)], obj=production_factory)
    assert again.exit_code != 0
    forced = cli_runner.invoke(cli, ["pack", str(entry), "--force"], obj=production_factory)
    assert forced.exit_code == 0, forced.output


@pytest.mark.os_agnostic
def test_pack_reports_a_missing_entry(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """A missing entry is a clean failure, not a traceback."""
    result = cli_runner.invoke(cli, ["pack", str(tmp_path / "nope.py")], obj=production_factory)
    assert result.exit_code != 0


@pytest.mark.os_agnostic
def test_unpack_restores_the_sources(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """unpack -o writes the packed sources back out, shim excluded."""
    entry = _project(tmp_path)
    packed = cli_runner.invoke(cli, ["pack", str(entry), "-o", str(tmp_path / "t.ps1")], obj=production_factory)
    assert packed.exit_code == 0, packed.output
    out = tmp_path / "restored"
    result = cli_runner.invoke(
        cli, ["unpack", str(tmp_path / "t.ps1"), "-o", str(out), "--json"], obj=production_factory
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["files"] == ["app.py", "pkg/__init__.py", "pkg/helper.py"]
    assert (out / "pkg" / "helper.py").read_text() == "VALUE = 1\n"


@pytest.mark.os_agnostic
def test_unpack_rejects_a_file_that_is_not_a_pack(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """An ordinary .ps1 fails cleanly rather than producing an empty directory."""
    stray = tmp_path / "plain.ps1"
    stray.write_text("Write-Host 'hi'\n")
    result = cli_runner.invoke(cli, ["unpack", str(stray), "-o", str(tmp_path / "out")], obj=production_factory)
    assert result.exit_code != 0
