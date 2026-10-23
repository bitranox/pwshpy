"""``pwshpy`` external-program command - run an argv and stream its output.

``exec`` runs a program with no shell (no quoting hell); it streams stdout, sends the
child's stderr to stderr, and exits with the child's exit code (or emits the typed
``ProcessResult`` as JSON with ``--json``).  Piped or redirected, the child's output is
written as UTF-8 bytes whatever the console code page (see
:func:`~pwshpy.adapters.cli.output.emit_text`).  ``--wait-tree`` also waits for every process
the program started, like ``Start-Process -Wait``.  Put the program after ``--`` so its own
flags are not parsed by pwshpy: ``pwshpy exec -- git status``.

Contents:
    * :func:`cli_exec`, exported as ``PROC_COMMANDS``.
"""

from __future__ import annotations

import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit, emit_text
from ..typed_click import argument, option
from ._common import TIMEOUT_RANGE, resolve_format


@click.command("exec", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("argv", nargs=-1, required=True)
@option("--cwd", default=None, help="Working directory to run in.")
@option(
    "--timeout",
    type=TIMEOUT_RANGE,
    default=None,
    help="Seconds before the process (or its tree) is killed, in (0, 2147483].",
)
@option(
    "--wait-tree",
    is_flag=True,
    default=False,
    help="Wait until every process the program started has exited too (like Start-Process -Wait).",
)
@option("--json", "as_json", is_flag=True, default=False, help="Emit the ProcessResult record as JSON.")
@click.pass_context
def cli_exec(
    ctx: click.Context, argv: tuple[str, ...], cwd: str | None, timeout: float | None, wait_tree: bool, as_json: bool
) -> None:
    """Run an external program (put it after --) and exit with its code (like & / Start-Process).

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_exec, ["--help"]).exit_code
        0
    """
    result = get_cli_context(ctx).services.ps.exec(list(argv), cwd=cwd, timeout=timeout, wait_tree=wait_tree)
    if as_json:
        emit([result], resolve_format(as_json=True, as_jsonl=False))
    else:
        emit_text(result.stdout)
        if result.stderr:
            emit_text(result.stderr, err=True)
    ctx.exit(result.exit_code)


PROC_COMMANDS = (cli_exec,)

__all__ = ["PROC_COMMANDS"]
