"""Shared helpers for record-listing CLI commands (processes, connections, ...).

Contents:
    * :func:`resolve_format` — map the mutually-exclusive output flags to a format.
"""

from __future__ import annotations

import rich_click as click

from ..output import RecordFormat


def resolve_format(as_json: bool, as_jsonl: bool) -> RecordFormat:
    """Pick the output format from mutually-exclusive flags (default: human table).

    Raises:
        click.UsageError: If both ``--json`` and ``--jsonl`` are given.

    Example:
        >>> resolve_format(False, True) is RecordFormat.JSONL
        True
        >>> resolve_format(False, False) is RecordFormat.HUMAN
        True
    """
    if as_json and as_jsonl:
        raise click.UsageError("--json and --jsonl are mutually exclusive.")
    if as_jsonl:
        return RecordFormat.JSONL
    if as_json:
        return RecordFormat.JSON
    return RecordFormat.HUMAN


__all__ = ["resolve_format"]
