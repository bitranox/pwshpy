"""Shared helpers for record-listing CLI commands (processes, connections, ...).

Contents:
    * :func:`resolve_format` — map the mutually-exclusive output flags to a format.
    * :func:`parse_pairs` — parse repeatable ``KEY=VALUE`` / ``Name: value`` options.
"""

from __future__ import annotations

from collections.abc import Iterable

import rich_click as click

from ..output import RecordFormat


def parse_pairs(pairs: Iterable[str], separator: str, label: str, *, strip: bool = False) -> dict[str, str]:
    """Parse repeatable ``KEY<sep>VALUE`` CLI options into a dict (last wins on a repeated key).

    Used for ``cmdlet -p KEY=VALUE`` and ``invoke_web_request -H 'Name: value'``. A pair with
    no separator or an empty key raises :class:`click.BadParameter` naming ``label``.

    Example:
        >>> parse_pairs(["Path=C:/", "Recurse=true"], "=", "KEY=VALUE")
        {'Path': 'C:/', 'Recurse': 'true'}
        >>> parse_pairs(["Accept: application/json"], ":", "'Name: value'", strip=True)
        {'Accept': 'application/json'}
        >>> parse_pairs(["oops"], "=", "KEY=VALUE")
        Traceback (most recent call last):
        click.exceptions.BadParameter: expected KEY=VALUE, got 'oops'
    """
    result: dict[str, str] = {}
    for pair in pairs:
        key, separator_found, value = pair.partition(separator)
        clean_key = key.strip() if strip else key
        if not separator_found or not clean_key:
            raise click.BadParameter(f"expected {label}, got {pair!r}")
        result[clean_key] = value.strip() if strip else value
    return result


def resolve_format(as_json: bool, as_jsonl: bool) -> RecordFormat:
    """Pick the output format from mutually-exclusive flags (default: human table).

    Raises:
        click.UsageError: If both ``--json`` and ``--jsonl`` are given.

    Example:
        >>> resolve_format(False, True) is RecordFormat.JSONL
        True
        >>> resolve_format(False, False) is RecordFormat.HUMAN
        True
        >>> resolve_format(True, True)
        Traceback (most recent call last):
        click.exceptions.UsageError: --json and --jsonl are mutually exclusive.
    """
    if as_json and as_jsonl:
        raise click.UsageError("--json and --jsonl are mutually exclusive.")
    if as_jsonl:
        return RecordFormat.JSONL
    if as_json:
        return RecordFormat.JSON
    return RecordFormat.HUMAN


__all__ = ["parse_pairs", "resolve_format"]
