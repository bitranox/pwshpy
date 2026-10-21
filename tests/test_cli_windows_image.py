"""``pwshpy get_windows_image`` through the REAL WIM reader, on synthesised WIM files.

The files come from ``wim_builder`` (header plus UTF-16LE XML resource), so these tests
are hermetic and portable: no DISM, no real image, no fake adapter behind the command.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner
from wim_builder import image, image_xml, wim_bytes

from pwshpy.adapters.cli.main import main
from pwshpy.adapters.cli.root import cli
from pwshpy.composition import AppServices, build_production


def _wim(tmp_path: Path, *images: str) -> Path:
    path = tmp_path / "install.wim"
    path.write_bytes(wim_bytes(image_xml(*images)))
    return path


@pytest.mark.os_agnostic
def test_jsonl_prints_one_line_per_image(
    tmp_path: Path, cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    """Each image of the file becomes one JSON line carrying the Get-WindowsImage fields."""
    path = _wim(tmp_path, image(1), image(2, name="Home", arch=12))
    result = cli_runner.invoke(cli, ["get_windows_image", str(path), "--jsonl"], obj=production_factory)
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(r["index"], r["name"], r["architecture"]) for r in rows] == [(1, "Pro", "x64"), (2, "Home", "ARM64")]
    assert rows[0]["version"] == "10.0.26200.8973"


@pytest.mark.os_agnostic
def test_index_option_keeps_only_that_image(
    tmp_path: Path, cli_runner: CliRunner, production_factory: Callable[[], AppServices]
) -> None:
    """``--index N`` narrows the list to image N, like ``Get-WindowsImage -Index``."""
    path = _wim(tmp_path, image(1), image(2, name="Home"))
    result = cli_runner.invoke(cli, ["get_windows_image", str(path), "--index", "2", "--json"], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert [(r["index"], r["name"]) for r in json.loads(result.stdout)] == [(2, "Home")]


@pytest.mark.os_agnostic
def test_damaged_tail_after_a_yielded_image_exits_nonzero_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reader streams, so image 1 is printed before the bad image 2 raises.

    The error must still take main()'s normal path: a one-line message on stderr and a
    non-zero exit, never a traceback, with the already-streamed line left intact.
    """
    path = _wim(tmp_path, image(1), '<IMAGE INDEX="two"><NAME>Bad</NAME></IMAGE>')
    exit_code = main(["get_windows_image", str(path), "--jsonl"], services_factory=build_production)
    captured = capsys.readouterr()
    assert exit_code != 0
    assert [json.loads(line)["index"] for line in captured.out.splitlines()] == [1]
    assert "numeric INDEX" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.os_agnostic
def test_a_missing_file_exits_nonzero_with_the_path_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A path that does not exist is a NativeCallError naming it, not a crash."""
    missing = tmp_path / "absent.wim"
    exit_code = main(["get_windows_image", str(missing)], services_factory=build_production)
    captured = capsys.readouterr()
    assert exit_code != 0
    assert "absent.wim" in captured.err
    assert "Traceback" not in captured.err
