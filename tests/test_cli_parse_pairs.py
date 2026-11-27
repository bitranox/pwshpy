"""The two CLI option-pair syntaxes: each one's separator, whitespace rule and refusal text."""

from __future__ import annotations

import pytest
import rich_click as click

from pwshpy.adapters.cli.commands._common import PairSyntax, parse_pairs


@pytest.mark.os_agnostic
def test_key_value_pairs_keep_whitespace_and_split_on_the_first_equals() -> None:
    assert parse_pairs([" a = b=c ", "a2="], PairSyntax.KEY_VALUE) == {" a ": " b=c ", "a2": ""}


@pytest.mark.os_agnostic
def test_header_pairs_strip_whitespace_and_split_on_the_first_colon() -> None:
    assert parse_pairs([" Host : example:8080 "], PairSyntax.HTTP_HEADER) == {"Host": "example:8080"}


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("syntax", "pair", "message"),
    [
        (PairSyntax.KEY_VALUE, "no-separator", "expected KEY=VALUE, got 'no-separator'"),
        (PairSyntax.KEY_VALUE, "=value", "expected KEY=VALUE, got '=value'"),
        (PairSyntax.HTTP_HEADER, "Accept=json", "expected 'Name: value', got 'Accept=json'"),
        (PairSyntax.HTTP_HEADER, "  : v", "expected 'Name: value', got '  : v'"),
    ],
)
def test_a_malformed_pair_is_refused_naming_its_syntax(syntax: PairSyntax, pair: str, message: str) -> None:
    with pytest.raises(click.BadParameter) as caught:
        parse_pairs([pair], syntax)
    assert caught.value.message == message


@pytest.mark.os_agnostic
def test_a_repeated_key_keeps_its_last_value() -> None:
    assert parse_pairs(["k=1", "k=2"], PairSyntax.KEY_VALUE) == {"k": "2"}


@pytest.mark.os_agnostic
@pytest.mark.parametrize("syntax", list(PairSyntax))
def test_every_syntax_parses(syntax: PairSyntax) -> None:
    """A new member without a split rule would raise KeyError here rather than in a command."""
    assert parse_pairs([], syntax) == {}
