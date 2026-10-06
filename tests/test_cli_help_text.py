"""``--help`` shows the command's description, never the doctest examples of its docstring.

Click turns a command's docstring into its help text and stops at a form feed (``\\f``). Every
command docstring here ends in an ``Example:`` section written for developers (``>>>`` lines),
so the form feed has to come before it, or ``pwshpy <command> --help`` prints Python code.
"""

from __future__ import annotations

from collections.abc import Iterator

import click
import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli
from pwshpy.composition import build_testing


def _command_paths(group: click.Group, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Every command path below ``group``, hidden commands included, nested groups walked."""
    for name, command in sorted(group.commands.items()):
        path = (*prefix, name)
        yield path
        if isinstance(command, click.Group):
            yield from _command_paths(command, path)


_PATHS = [(), *_command_paths(cli)]


@pytest.mark.os_agnostic
def test_the_walk_reaches_the_commands_it_is_meant_to_check() -> None:
    """Guards the parametrisation below against passing over an empty or partial command list."""
    assert ("config-deploy",) in _PATHS
    assert ("get_process",) in _PATHS
    assert len(_PATHS) > 30


@pytest.mark.os_agnostic
@pytest.mark.parametrize("path", _PATHS, ids=lambda path: " ".join(path) or "<root>")
def test_help_never_prints_a_doctest(cli_runner: CliRunner, path: tuple[str, ...]) -> None:
    result = cli_runner.invoke(cli, [*path, "--help"], obj=build_testing)

    assert result.exit_code == 0, result.output
    assert "Usage" in result.output
    assert ">>>" not in result.output, result.output
