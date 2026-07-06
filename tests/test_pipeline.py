"""Lazy fluent pipeline: operators, laziness, and single-consumption semantics."""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pytest

from pwshpy.domain.pipeline import Pipeline


@pytest.mark.os_agnostic
def test_where_filters_records() -> None:
    """where keeps only records matching the predicate."""
    assert Pipeline(range(6)).where(lambda n: n % 2 == 0).to_list() == [0, 2, 4]


@pytest.mark.os_agnostic
def test_select_projects_records() -> None:
    """select maps each record through the selector."""
    assert Pipeline([1, 2, 3]).select(lambda n: n * n).to_list() == [1, 4, 9]


@pytest.mark.os_agnostic
def test_sort_by_orders_records() -> None:
    """sort_by returns a pipeline ordered by the key."""
    assert Pipeline([3, 1, 2]).sort_by(lambda n: n).to_list() == [1, 2, 3]


@pytest.mark.os_agnostic
def test_sort_by_reverse() -> None:
    """sort_by honors reverse=True."""
    assert Pipeline([1, 3, 2]).sort_by(lambda n: n, reverse=True).to_list() == [3, 2, 1]


@pytest.mark.os_agnostic
def test_take_limits_and_stays_lazy_over_infinite_source() -> None:
    """take yields at most N and does not exhaust an infinite source."""
    assert Pipeline(itertools.count()).take(4).to_list() == [0, 1, 2, 3]


@pytest.mark.os_agnostic
def test_take_negative_yields_nothing() -> None:
    """take with a non-positive count yields no records."""
    assert Pipeline(range(5)).take(-3).to_list() == []


@pytest.mark.os_agnostic
def test_first_returns_first_match() -> None:
    """first returns the first record matching the predicate."""
    assert Pipeline([1, 2, 3, 4]).first(lambda n: n > 2) == 3


@pytest.mark.os_agnostic
def test_first_returns_default_when_no_match() -> None:
    """first returns the default when nothing matches."""
    assert Pipeline([1, 2]).first(lambda n: n > 9, default=-1) == -1


@pytest.mark.os_agnostic
def test_first_without_predicate_returns_head() -> None:
    """first with no predicate returns the first record."""
    assert Pipeline([7, 8]).first() == 7


@pytest.mark.os_agnostic
def test_chaining_composes_lazily() -> None:
    """where -> select -> take chains and stays lazy over an infinite source."""
    result = Pipeline(itertools.count()).where(lambda n: n % 3 == 0).select(lambda n: n + 1).take(3).to_list()
    assert result == [1, 4, 7]


@pytest.mark.os_agnostic
def test_first_stops_early_without_consuming_all() -> None:
    """first stops at the first match, leaving the rest of the generator unconsumed."""
    consumed: list[int] = []

    def source() -> Iterator[int]:
        for value in range(100):
            consumed.append(value)
            yield value

    Pipeline(source()).first(lambda n: n == 2)
    assert consumed == [0, 1, 2]


@pytest.mark.os_agnostic
def test_generator_source_is_single_consumption() -> None:
    """A pipeline fed a one-shot generator is exhausted after one traversal."""
    pipe = Pipeline(x for x in [1, 2, 3])
    assert pipe.to_list() == [1, 2, 3]
    assert pipe.to_list() == []
