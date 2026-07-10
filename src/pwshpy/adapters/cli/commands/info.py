"""Metadata command plus an internal failure hook for error-path testing.

Contents:
    * :func:`cli_info` - Display package metadata.
    * :func:`_fail` - Internal helper that raises, used to exercise the CLI
      error-handling and traceback plumbing.
    * :func:`cli_fail` - Hidden command wrapping :func:`_fail` (not shown in
      ``--help``); the vehicle the error-path tests drive through the real CLI.
"""

from __future__ import annotations

import logging

import lib_log_rich.runtime
import rich_click as click

from pwshpy import __init__conf__

from ..constants import CLICK_CONTEXT_SETTINGS

logger = logging.getLogger(__name__)


@click.command("info", context_settings=CLICK_CONTEXT_SETTINGS)
def cli_info() -> None:
    """Print resolved metadata so users can inspect installation details.

    Example:
        >>> from click.testing import CliRunner
        >>> runner = CliRunner()
        >>> result = runner.invoke(cli_info)
        >>> result.exit_code == 0
        True
    """
    with lib_log_rich.runtime.bind(job_id="cli-info", extra={"command": "info"}):
        logger.info("Displaying package information")
        __init__conf__.print_info()


def _fail() -> None:
    """Raise the intentional failure used to exercise CLI error handling.

    Internal diagnostic hook, not a user-facing feature. Kept so the error-path
    and traceback plumbing tests can drive a real exception through the
    exit-code machinery.

    Example:
        >>> _fail()
        Traceback (most recent call last):
        ...
        RuntimeError: I should fail
    """
    raise RuntimeError("I should fail")


@click.command("fail", hidden=True, context_settings=CLICK_CONTEXT_SETTINGS)
def cli_fail() -> None:
    """Hidden diagnostic command that triggers :func:`_fail`.

    Not listed in ``--help``; exists only as the CLI-level failure vehicle for
    error-handling tests.

    Example:
        >>> from click.testing import CliRunner
        >>> runner = CliRunner()
        >>> result = runner.invoke(cli_fail)
        >>> result.exit_code != 0
        True
    """
    with lib_log_rich.runtime.bind(job_id="cli-fail", extra={"command": "fail"}):
        logger.warning("Executing intentional failure command")
        _fail()


__all__ = ["cli_fail", "cli_info"]
