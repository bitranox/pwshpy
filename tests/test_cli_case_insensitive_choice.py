"""Unit tests for :class:`CaseInsensitiveChoice`: any-case matching, original spelling on display.

click 8.5's ``Choice(case_sensitive=False)`` casefolds every choice it shows (usage metavar, error
and missing messages, completion). These pin that the subclass shows the choices exactly as they
were given, in their order, while matching stays click's own case-insensitive logic.
"""

from __future__ import annotations

import click
import pytest

from pwshpy.adapters.cli.commands._common import CaseInsensitiveChoice

_LEVELS = ("Limited", "Highest")


def _ctx() -> click.Context:
    return click.Context(click.Command("x"))


@pytest.mark.os_agnostic
def test_it_is_always_case_insensitive() -> None:
    assert CaseInsensitiveChoice(_LEVELS).case_sensitive is False


@pytest.mark.os_agnostic
@pytest.mark.parametrize("typed", ["Highest", "highest", "HIGHEST", "hIgHeSt"])
def test_convert_returns_the_original_spelling_for_any_case(typed: str) -> None:
    assert CaseInsensitiveChoice(_LEVELS).convert(typed, None, _ctx()) == "Highest"


@pytest.mark.os_agnostic
def test_required_argument_metavar_keeps_the_spelling_in_braces() -> None:
    choice = CaseInsensitiveChoice(_LEVELS)
    param = click.Argument(["level"], type=choice)
    assert choice.get_metavar(param, _ctx()) == "{Limited|Highest}"


@pytest.mark.os_agnostic
def test_optional_argument_metavar_keeps_the_spelling_in_brackets() -> None:
    choice = CaseInsensitiveChoice(_LEVELS)
    param = click.Argument(["level"], type=choice, required=False)
    assert choice.get_metavar(param, _ctx()) == "[Limited|Highest]"


@pytest.mark.os_agnostic
def test_option_metavar_keeps_the_spelling_in_brackets() -> None:
    choice = CaseInsensitiveChoice(_LEVELS)
    param = click.Option(["--level"], type=choice)
    assert choice.get_metavar(param, _ctx()) == "[Limited|Highest]"


@pytest.mark.os_agnostic
def test_option_without_show_choices_still_names_the_type() -> None:
    choice = CaseInsensitiveChoice(_LEVELS)
    param = click.Option(["--level"], type=choice, show_choices=False)
    assert choice.get_metavar(param, _ctx()) == "[TEXT]"


@pytest.mark.os_agnostic
def test_invalid_choice_message_lists_the_original_spellings() -> None:
    choice = CaseInsensitiveChoice(_LEVELS)
    with pytest.raises(click.BadParameter) as caught:
        choice.convert("bogus", None, _ctx())
    assert caught.value.message == "'bogus' is not one of 'Limited', 'Highest'."


@pytest.mark.os_agnostic
def test_invalid_choice_message_for_a_single_choice_uses_the_singular() -> None:
    choice = CaseInsensitiveChoice(["OnlyOne"])
    assert choice.get_invalid_choice_message("bogus", _ctx()) == "'bogus' is not 'OnlyOne'."


@pytest.mark.os_agnostic
def test_missing_message_lists_the_original_spellings() -> None:
    choice = CaseInsensitiveChoice(_LEVELS)
    param = click.Argument(["level"], type=choice)
    assert choice.get_missing_message(param, _ctx()) == "Choose from:\n\tLimited,\n\tHighest"


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("incomplete", "expected"),
    [("", ["Limited", "Highest"]), ("hi", ["Highest"]), ("LIM", ["Limited"]), ("x", [])],
)
def test_shell_complete_offers_the_original_spellings_matched_in_any_case(incomplete: str, expected: list[str]) -> None:
    choice = CaseInsensitiveChoice(_LEVELS)
    param = click.Option(["--level"], type=choice)
    assert [item.value for item in choice.shell_complete(_ctx(), param, incomplete)] == expected
