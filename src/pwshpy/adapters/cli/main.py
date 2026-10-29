"""CLI entry point and execution wrapper.

Provides the main entry point used by console scripts and ``python -m``
execution, ensuring consistent error handling and traceback restoration.

Contents:
    * :func:`main` - Primary entry point for CLI execution.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import lib_cli_exit_tools
import lib_log_rich.runtime

from pwshpy import __init__conf__

from ...domain.errors import NativeAccessDeniedError, OperationTimeoutError
from .constants import TRACEBACK_SUMMARY_LIMIT, TRACEBACK_VERBOSE_LIMIT
from .context import (
    apply_traceback_preferences,
    restore_traceback_state,
    snapshot_traceback_state,
)
from .exit_codes import ExitCode

if TYPE_CHECKING:
    from pwshpy.composition import AppServices


def _run_cli(argv: Sequence[str] | None, *, services_factory: Callable[[], AppServices]) -> int:
    """Execute the CLI with exception handling.

    Args:
        argv: Optional sequence of CLI arguments. None uses sys.argv.
        services_factory: Factory function that returns AppServices. Passed via ctx.obj.

    Returns:
        Exit code produced by the command.
    """
    import sys

    import click

    from .root import cli

    # Use Click's native invocation with obj parameter since lib_cli_exit_tools.run_cli
    # doesn't support passing obj. We replicate its behavior while adding obj support.
    args = list(argv) if argv is not None else sys.argv[1:]

    try:
        # cli is a rich_click group whose own main() reimplements click's Command.main().
        # Under standalone_mode=False it catches the click.exceptions.Exit that ctx.exit()
        # raises and RETURNS its exit code, and it returns a callback's plain return value the
        # same way, so the exit code arrives here as a return value and never as an Exit.
        # Commands therefore exit through ctx.exit() and never raise a bare SystemExit, which
        # would fall into the catch-all branch below and print as "SystemExit: N".
        exit_code = cli.main(
            args=args,
            prog_name=__init__conf__.shell_command,
            obj=services_factory,
            standalone_mode=False,
        )
        return exit_code if isinstance(exit_code, int) else 0
    except click.ClickException as exc:
        _drain_log_queue()
        exc.show()
        return exc.exit_code
    except BaseException as exc:
        # Catch BaseException (not just Exception) to handle SystemExit, KeyboardInterrupt,
        # and all errors at the CLI boundary. This ensures consistent error formatting via
        # lib_cli_exit_tools regardless of exception type. Intentional, not a bug.
        _drain_log_queue()
        tracebacks_enabled = bool(getattr(lib_cli_exit_tools.config, "traceback", False))
        apply_traceback_preferences(tracebacks_enabled)
        length_limit = TRACEBACK_VERBOSE_LIMIT if tracebacks_enabled else TRACEBACK_SUMMARY_LIMIT
        lib_cli_exit_tools.print_exception_message(trace_back=tracebacks_enabled, length_limit=length_limit)
        return _exit_code_for(exc)


#: Upper bound on waiting for queued log lines before the error is printed; the error prints anyway.
_LOG_DRAIN_TIMEOUT_SECONDS = 5.0


def _drain_log_queue() -> None:
    """Render every log line still queued before the CLI prints the error that ends the command.

    lib_log_rich renders console lines on a queue worker thread, and the error is printed on this
    one. Printed with a line still queued, both threads write to stderr at once and the two lines
    interleave into one (measured on Windows). Draining first keeps each line whole and in the
    order it was logged. A drain that times out or cannot run is not reported: the command's own
    error is the one the user needs, and it still prints.
    """
    if not lib_log_rich.runtime.is_initialised():
        return
    try:
        lib_log_rich.runtime.flush(timeout=_LOG_DRAIN_TIMEOUT_SECONDS)
    except (TimeoutError, RuntimeError):
        return


def _exit_code_for(exc: BaseException) -> int:
    """The process exit code for an exception that reached the CLI boundary.

    An :class:`OperationTimeoutError` exits ETIMEDOUT (110) on every OS. lib_cli_exit_tools
    cannot give that: the error is a ``NativeCallError``, not a ``TimeoutError``, so it falls
    through to the generic 1 - and its ``TimeoutError`` row is 1460 on Windows anyway.

    A :class:`NativeAccessDeniedError` exits as a plain ``PermissionError``: 13 on POSIX, 5 on
    Windows. lib_cli_exit_tools reads an exception's ``errno`` before its platform table, and the
    errno is whatever the refused OS call carried, so passing the error itself would give one
    refusal three codes: 13 for EACCES, 1 for EPERM (the generic failure code), and 13 or 5 for a
    COM ``E_ACCESSDENIED``, which has no errno. A fresh ``PermissionError`` carries no errno, so the
    platform table decides. Everything else keeps lib_cli_exit_tools' mapping.

    Args:
        exc: The exception the command raised.

    Returns:
        The exit code for ``exc``.

    Example:
        >>> _exit_code_for(OperationTimeoutError("slow", timeout=1.0))
        110
        >>> import errno
        >>> denied = NativeAccessDeniedError("refused")
        >>> denied.errno = errno.EPERM
        >>> _exit_code_for(denied) == lib_cli_exit_tools.get_system_exit_code(PermissionError())
        True
    """
    if isinstance(exc, OperationTimeoutError):
        return int(ExitCode.TIMEOUT)
    if isinstance(exc, NativeAccessDeniedError):
        return lib_cli_exit_tools.get_system_exit_code(PermissionError())
    return lib_cli_exit_tools.get_system_exit_code(exc)


def main(
    argv: Sequence[str] | None = None,
    *,
    restore_traceback: bool = True,
    services_factory: Callable[[], AppServices] | None = None,
) -> int:
    """Execute the CLI with error handling and return the exit code.

    Provides the single entry point used by console scripts and
    ``python -m`` execution so that behaviour stays identical across transports.

    Args:
        argv: Optional sequence of CLI arguments. None uses sys.argv.
        restore_traceback: Whether to restore prior traceback configuration after execution.
        services_factory: Factory function returning AppServices. Required.
            Callers outside the adapters layer should pass ``build_production``.

    Returns:
        Exit code reported by the CLI run.

    Raises:
        ValueError: If services_factory is not provided.

    Example:
        >>> from pwshpy.composition import build_production
        >>> exit_code = main(["--help"], services_factory=build_production)  # doctest: +SKIP
        >>> exit_code == 0  # doctest: +SKIP
        True
    """
    if services_factory is None:
        raise ValueError("services_factory is required. Pass build_production from composition layer.")

    previous_state = snapshot_traceback_state()
    try:
        return _run_cli(argv, services_factory=services_factory)
    finally:
        if restore_traceback:
            restore_traceback_state(previous_state)
        # Only shutdown logging from main thread to avoid killing logging for other threads.
        is_main_thread = threading.current_thread() is threading.main_thread()
        if is_main_thread and lib_log_rich.runtime.is_initialised():
            lib_log_rich.runtime.shutdown()


__all__ = ["main"]
