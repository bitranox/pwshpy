"""Shared helpers for record-listing CLI commands (processes, connections, ...).

Contents:
    * :func:`resolve_format` - map the mutually-exclusive output flags to a format.
    * :class:`PairSyntax` - the two option-pair syntaxes the CLI parses.
    * :func:`parse_pairs` - parse repeatable ``KEY=VALUE`` / ``Name: value`` options.
    * :data:`TIMEOUT_RANGE` - the ``--timeout`` range the facade accepts.
    * :func:`enum_by_value` - map a case-insensitive ``click.Choice`` answer to its enum member.
    * :class:`CaseInsensitiveChoice` - a choice matched in any case but shown as written.
    * :func:`bind_command` - bind the log context to the command's registered name.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from enum import Enum
from gettext import gettext as _
from gettext import ngettext
from typing import NamedTuple, TypeVar

import lib_log_rich.runtime
import rich_click as click
from click.shell_completion import CompletionItem
from lib_log_rich.domain import LogContext

from ..output import RecordFormat

_E = TypeVar("_E", bound=Enum)


def bind_command(
    ctx: click.Context, *, extra: Mapping[str, object] | None = None
) -> AbstractContextManager[LogContext]:
    """Bind the log context to the command's registered name, so log lines name a command that exists.

    The name comes from the click context (the name the command is registered and typed under), so
    a command cannot log under a short internal name nobody can search for: ``pwshpy get_volume``
    logs with job id ``cli-get_volume`` and ``extra["command"] == "get_volume"``.

    Args:
        ctx: The running command's click context.
        extra: Further fields for the bound context; ``command`` is always the registered name.

    Returns:
        The context manager ``lib_log_rich.runtime.bind`` returns; enter it around the command body.

    Example:
        >>> ctx = click.Context(click.Command("get_volume"))
        >>> isinstance(bind_command(ctx), AbstractContextManager)
        True
    """
    name = ctx.command.name or ctx.info_name or "pwshpy"
    return lib_log_rich.runtime.bind(job_id=f"cli-{name}", extra={**(extra or {}), "command": name})


#: Mirrors the facade's accepted timeout range, ``(0, 2147483]`` seconds, so a bad value is a usage
#: error (exit 2) before any child starts; NaN slips past a range check, and the facade refuses it.
TIMEOUT_RANGE = click.FloatRange(min=0, min_open=True, max=2147483)


class PairSyntax(str, Enum):
    """The option-pair syntaxes :func:`parse_pairs` reads; the value is the form a refusal names.

    One member per syntax, so the separator, the label and the whitespace rule always travel
    together: a caller cannot pair ``=`` with the header label or strip a ``KEY=VALUE`` value.

    Attributes:
        KEY_VALUE: ``KEY=VALUE`` (``cmdlet -p``); key and value kept exactly as typed.
        HTTP_HEADER: ``Name: value`` (``invoke_web_request -H``); whitespace around both stripped.
    """

    KEY_VALUE = "KEY=VALUE"
    HTTP_HEADER = "'Name: value'"


class _PairRule(NamedTuple):
    """How one :class:`PairSyntax` splits a pair.

    Attributes:
        separator: What separates the key from the value (the first occurrence splits).
        strip: Whether whitespace around the key and the value is removed.
    """

    separator: str
    strip: bool


_PAIR_RULES: dict[PairSyntax, _PairRule] = {
    PairSyntax.KEY_VALUE: _PairRule(separator="=", strip=False),
    PairSyntax.HTTP_HEADER: _PairRule(separator=":", strip=True),
}


def parse_pairs(pairs: Iterable[str], syntax: PairSyntax) -> dict[str, str]:
    """Parse repeatable ``KEY<sep>VALUE`` CLI options into a dict (last wins on a repeated key).

    Used for ``cmdlet -p KEY=VALUE`` and ``invoke_web_request -H 'Name: value'``. A pair with
    no separator or an empty key raises :class:`click.BadParameter` naming the syntax.

    Args:
        pairs: The option values as typed.
        syntax: Which pair syntax they are written in.

    Returns:
        The pairs as a dict, a repeated key keeping its last value.

    Raises:
        click.BadParameter: A pair has no separator or an empty key.

    Example:
        >>> parse_pairs(["Path=C:/", "Recurse=true"], PairSyntax.KEY_VALUE)
        {'Path': 'C:/', 'Recurse': 'true'}
        >>> parse_pairs(["Accept: application/json"], PairSyntax.HTTP_HEADER)
        {'Accept': 'application/json'}
        >>> parse_pairs(["oops"], PairSyntax.KEY_VALUE)
        Traceback (most recent call last):
        click.exceptions.BadParameter: expected KEY=VALUE, got 'oops'
    """
    rule = _PAIR_RULES[syntax]
    result: dict[str, str] = {}
    for pair in pairs:
        key, separator_found, value = pair.partition(rule.separator)
        clean_key = key.strip() if rule.strip else key
        if not separator_found or not clean_key:
            raise click.BadParameter(f"expected {syntax.value}, got {pair!r}")
        result[clean_key] = value.strip() if rule.strip else value
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


def enum_by_value(enum_cls: type[_E], value: str) -> _E:
    """Return the member of ``enum_cls`` whose value matches ``value`` case-insensitively.

    ``click.Choice(case_sensitive=False)`` hands back the canonical spelling in current click,
    but this does not depend on it.

    Args:
        enum_cls: The enum whose members' values the choice listed.
        value: The spelling the user typed (or click handed back).

    Returns:
        The matching member.

    Raises:
        ValueError: No member's value matches (unreachable behind a ``click.Choice`` of the values).

    Example:
        >>> from pwshpy.domain.enums import KnownFolder
        >>> enum_by_value(KnownFolder, "mydocuments") is KnownFolder.DOCUMENTS
        True
    """
    folded = value.casefold()
    match = next((member for member in enum_cls if str(member.value).casefold() == folded), None)
    if match is None:  # unreachable behind click.Choice; a ValueError reaches the user as a usage error
        raise ValueError(f"{value!r} is not one of {', '.join(str(member.value) for member in enum_cls)}")
    return match


class CaseInsensitiveChoice(click.Choice[str]):
    """A ``click.Choice`` matched in any case but shown in the spelling it was given.

    click 8.5's ``Choice(case_sensitive=False)`` casefolds every choice it SHOWS (the usage
    metavar, the bad-value and missing-value messages, shell completion), so a documented .NET
    name such as ``MyDocuments`` reaches the user as ``mydocuments``.  This keeps click's matching
    (:meth:`normalize_choice` and :meth:`convert` are inherited untouched; ``convert`` already
    returns the original choice) and overrides only the four display paths, with click's own
    formatting.  It is named for what it accepts, not for .NET: any mixed-case choice list needs it.

    Args:
        choices: The accepted values in their canonical spelling and display order.

    Example:
        >>> choice = CaseInsensitiveChoice(["Desktop", "MyDocuments"])
        >>> choice.convert("mydocuments", None, None)
        'MyDocuments'
        >>> choice.get_invalid_choice_message("bogus", None)
        "'bogus' is not one of 'Desktop', 'MyDocuments'."
    """

    def __init__(self, choices: Iterable[str]) -> None:
        super().__init__(choices, case_sensitive=False)

    def _shown(self) -> list[str]:
        return [str(choice) for choice in self.choices]

    def get_metavar(self, param: click.Parameter, ctx: click.Context) -> str | None:
        """Return ``{A|B}`` for a required argument, else ``[A|B]``, in the original spelling.

        An option with ``show_choices=False`` keeps click's type-name metavar (``[TEXT]``).
        """
        if isinstance(param, click.Option) and not param.show_choices:
            return super().get_metavar(param, ctx)
        choices_str = "|".join(self._shown())
        if param.required and param.param_type_name == "argument":
            return f"{{{choices_str}}}"
        return f"[{choices_str}]"

    def get_missing_message(self, param: click.Parameter, ctx: click.Context | None) -> str:
        """Return click's "Choose from" list in the original spelling."""
        return _("Choose from:\n\t{choices}").format(choices=",\n\t".join(self._shown()))

    def get_invalid_choice_message(self, value: object, ctx: click.Context | None) -> str:
        """Return click's bad-value message listing the choices in the original spelling."""
        choices_str = ", ".join(map(repr, self._shown()))
        return ngettext(
            "{value!r} is not {choice}.",
            "{value!r} is not one of {choices}.",
            len(self.choices),
        ).format(value=value, choice=choices_str, choices=choices_str)

    def shell_complete(self, ctx: click.Context, param: click.Parameter, incomplete: str) -> list[CompletionItem]:
        """Complete the choices starting with ``incomplete`` in any case, in the original spelling."""
        # Folding here instead of through normalize_choice skips ctx.token_normalize_func; pwshpy
        # sets none, and going through normalize_choice would hand back the folded spelling.
        folded = incomplete.casefold()
        return [CompletionItem(choice) for choice in self._shown() if choice.casefold().startswith(folded)]


__all__ = ["CaseInsensitiveChoice", "PairSyntax", "bind_command", "enum_by_value", "parse_pairs", "resolve_format"]
