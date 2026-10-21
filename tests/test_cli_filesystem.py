"""CLI tests for the filesystem commands (real facade, hermetic tmp paths, os_agnostic)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli
from pwshpy.domain.errors import NativeCallError

if TYPE_CHECKING:
    from pwshpy.composition import AppServices


@pytest.mark.os_agnostic
def test_get_child_item_jsonl(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """get_child_item --jsonl streams a FileSystemItem per entry."""
    (tmp_path / "a.txt").write_text("x")
    result = cli_runner.invoke(cli, ["get_child_item", str(tmp_path), "--jsonl"], obj=production_factory)
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert any(row["name"] == "a.txt" and row["is_directory"] is False for row in rows)


@pytest.mark.os_agnostic
def test_test_path_exit_codes(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """test_path exits 0 when the path exists, 1 when it does not."""
    ok = cli_runner.invoke(cli, ["test_path", str(tmp_path)], obj=production_factory)
    assert ok.exit_code == 0
    assert "True" in ok.output
    missing = cli_runner.invoke(cli, ["test_path", str(tmp_path / "nope")], obj=production_factory)
    assert missing.exit_code == 1


@pytest.mark.os_agnostic
def test_new_item_and_get_content(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """new_item creates a file; get_content reads it back."""
    target = tmp_path / "made.txt"
    target.write_text("body")
    created = cli_runner.invoke(cli, ["new_item", str(tmp_path / "empty.txt")], obj=production_factory)
    assert created.exit_code == 0
    assert (tmp_path / "empty.txt").is_file()
    content = cli_runner.invoke(cli, ["get_content", str(target)], obj=production_factory)
    assert content.exit_code == 0
    assert "body" in content.output


@pytest.mark.os_agnostic
def test_get_child_item_json_on_a_missing_directory_writes_no_partial_array(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], tmp_path: Path
) -> None:
    """A source that fails before its first record leaves --json stdout empty, not a bare ``[``.

    A caller piping ``--json`` into a JSON parser then sees only the stderr message and the
    exit code, never a parse error on top of it.
    """
    missing = tmp_path / "does-not-exist"
    result = cli_runner.invoke(cli, ["get_child_item", str(missing), "--json"], obj=production_factory)
    assert result.exit_code == 1
    assert isinstance(result.exception, NativeCallError)
    assert result.stdout == ""
