"""CLI log context: every command binds its log context to the name it is registered under.

The log lines a command writes carry ``job_id`` and ``extra["command"]`` from the bind around its
body. Both must name the command a user can type (``get_volume``), never a short internal name
(``disks``) nobody can search for: the binds go through ``bind_command``, which reads the name off
the click context, and a guard refuses, in every command module but ``_common.py``, a direct
``lib_log_rich.runtime.bind`` and any ``lib_log_rich`` import except its ``domain`` types.
"""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import lib_log_rich.runtime
import pytest
import rich_click as click
from click.testing import CliRunner
from lib_layered_config import Config

from pwshpy.adapters.cli.commands._common import bind_command
from pwshpy.adapters.cli.root import cli
from pwshpy.adapters.memory.logging import init_logging_in_memory
from pwshpy.composition import AppServices, build_testing

_COMMANDS = Path(__file__).resolve().parents[1] / "src" / "pwshpy" / "adapters" / "cli" / "commands"


def _calls(tree: ast.AST) -> Iterator[ast.Call]:
    return (node for node in ast.walk(tree) if isinstance(node, ast.Call))


def _is_runtime_bind(call: ast.Call) -> bool:
    """``lib_log_rich.runtime.bind(...)`` or ``runtime.bind(...)``: the direct bind."""
    func = call.func
    return isinstance(func, ast.Attribute) and func.attr == "bind" and "runtime" in ast.unparse(func.value)


def _is_bind_command(call: ast.Call) -> bool:
    func = call.func
    return isinstance(func, ast.Name) and func.id == "bind_command"


def _is_lib_log_rich(module: str) -> bool:
    return module == "lib_log_rich" or module.startswith("lib_log_rich.")


def _imports_lib_log_rich(node: ast.Import | ast.ImportFrom) -> bool:
    """An import that can reach ``bind``: any ``lib_log_rich`` import but ``from lib_log_rich.domain``.

    Matching imports rather than calls catches a bind under any local name (``from ... import
    bind``, ``import lib_log_rich.runtime as rt``). ``import lib_log_rich.domain`` is refused too:
    it binds the top-level name, and ``lib_log_rich.bind`` hangs off that.
    """
    if isinstance(node, ast.Import):
        return any(_is_lib_log_rich(alias.name) for alias in node.names)
    if node.module is None or not _is_lib_log_rich(node.module):
        return False
    return not (node.module == "lib_log_rich.domain" or node.module.startswith("lib_log_rich.domain."))


def _violations(tree: ast.AST) -> list[str]:
    """The direct binds and the imports that could reach ``bind`` in ``tree``, each as ``<line> <source>``."""
    found: list[ast.stmt | ast.expr] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom)) and _imports_lib_log_rich(node)
    ]
    found += [call for call in _calls(tree) if _is_runtime_bind(call)]
    return [f"{node.lineno} {ast.unparse(node)}" for node in found]


def _scan() -> tuple[list[str], int, int]:
    """Return the direct binds outside ``_common.py``, the files scanned and the bind_command calls."""
    direct: list[str] = []
    scanned = 0
    helper_calls = 0
    for path in sorted(_COMMANDS.glob("*.py")):
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        helper_calls += sum(_is_bind_command(call) for call in _calls(tree))
        if path.name != "_common.py":
            direct.extend(f"{path.name}:{found}" for found in _violations(tree))
    return direct, scanned, helper_calls


@pytest.mark.os_agnostic
def test_no_command_module_binds_its_log_context_by_hand() -> None:
    """Every bind goes through bind_command, so no command can name itself by a literal."""
    direct, scanned, helper_calls = _scan()
    assert scanned > 15, f"scanned only {scanned} command modules - wrong root?"
    assert direct == [], "bind through _common.bind_command(ctx):\n" + "\n".join(direct)
    assert helper_calls >= 20, f"found only {helper_calls} bind_command calls - is the scan reading the binds?"


@pytest.mark.os_agnostic
def test_the_guard_flags_a_literal_bind() -> None:
    """The detector is not blind: a hand-written bind with a literal command name is reported."""
    sample = ast.parse('with lib_log_rich.runtime.bind(job_id="cli-disks", extra={"command": "disks"}):\n    pass\n')
    assert _violations(sample)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "source",
    [
        "from lib_log_rich.runtime import bind\nwith bind(job_id='cli-disks'):\n    pass\n",
        "import lib_log_rich.runtime as rt\nwith rt.bind(job_id='cli-disks'):\n    pass\n",
        "import lib_log_rich\nwith lib_log_rich.bind(job_id='cli-disks'):\n    pass\n",
        "from lib_log_rich import runtime as r\n",
    ],
)
def test_the_guard_flags_every_route_to_bind(source: str) -> None:
    """A bind reached through an imported name or an alias is reported too, not only the dotted call."""
    assert _violations(ast.parse(source))


@pytest.mark.os_agnostic
def test_the_guard_lets_a_command_import_lib_log_rich_types() -> None:
    """``lib_log_rich.domain`` holds types only (``logdemo`` needs ``ConsoleTheme``), and no bind."""
    assert _violations(ast.parse("from lib_log_rich.domain import ConsoleTheme\n")) == []


def _logged_events(cli_runner: CliRunner, args: list[str]) -> list[dict[str, Any]]:
    """Run ``pwshpy ARGS`` under the in-memory composition and return the runtime's recorded events.

    The in-memory runtime is started here with the stdlib bridge attached at DEBUG, through
    lib_log_rich's public API, so a command's INFO line reaches the ring buffer with the context
    bound around it; the command's own ``init_logging`` then finds the runtime live.
    """
    init_logging_in_memory(Config({}, {}))
    lib_log_rich.runtime.attach_std_logging(logger_level=logging.DEBUG, handler_level=logging.DEBUG)
    factory: Callable[[], AppServices] = build_testing
    result = cli_runner.invoke(cli, args, obj=factory)
    assert result.exit_code == 0, result.output
    events: list[dict[str, Any]] = json.loads(lib_log_rich.runtime.dump(dump_format="json"))
    return events


def _context_of(events: list[dict[str, Any]], message: str) -> dict[str, Any]:
    matching = [event["context"] for event in events if event["message"] == message]
    assert len(matching) == 1, f"expected one {message!r} event, got {len(matching)}"
    return matching[0]


@pytest.mark.os_agnostic
def test_a_command_logs_under_its_registered_name(cli_runner: CliRunner) -> None:
    """``pwshpy get_volume`` logs with job id ``cli-get_volume`` and command ``get_volume``."""
    context = _context_of(_logged_events(cli_runner, ["get_volume", "--jsonl"]), "Listing mounted filesystems")
    assert context["job_id"] == "cli-get_volume"
    assert context["extra"]["command"] == "get_volume"


@pytest.mark.os_agnostic
def test_a_caller_field_named_command_cannot_rename_the_command() -> None:
    """``extra`` adds fields, but ``command`` stays the registered name whatever the caller passes."""
    init_logging_in_memory(Config({}, {}))
    ctx = click.Context(click.Command("get_volume"))
    with bind_command(ctx, extra={"command": "disks", "format": "json"}) as bound:
        assert bound.job_id == "cli-get_volume"
        assert dict(bound.extra) == {"command": "get_volume", "format": "json"}


@pytest.mark.os_agnostic
def test_a_command_with_its_own_log_fields_keeps_them_beside_the_name(cli_runner: CliRunner) -> None:
    """``config`` binds its format and profile too; the command name is still the registered one."""
    events = _logged_events(cli_runner, ["config", "--format", "json"])
    context = _context_of(events, "Displaying configuration")
    assert context["job_id"] == "cli-config"
    assert context["extra"]["command"] == "config"
    assert context["extra"]["format"] == "json"
