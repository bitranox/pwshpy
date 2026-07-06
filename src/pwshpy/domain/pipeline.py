"""Lazy fluent pipeline over records — the Pythonic PowerShell object pipeline.

:class:`Pipeline` wraps any iterable of records and exposes chainable, generator-
backed operators (``.where`` / ``.select`` / ``.sort_by`` / ``.take`` / ``.first``
/ ``.to_list``).  ``where``/``select``/``take`` stay lazy; ``sort_by`` is blocking
by nature (it must see every item), mirroring PowerShell's ``Sort-Object``.

Laziness note:
    A pipeline fed a one-shot generator is itself single-consumption — iterate it
    once.  Fed a re-iterable source (list, tuple), it can be traversed repeatedly.
    The facade hands out a fresh generator per call (``ps.processes()``), so each
    call is independently consumable.

Contents:
    * :class:`Pipeline` — the fluent, lazy record pipeline.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Generic, TypeVar, overload

T = TypeVar("T")
R = TypeVar("R")
_MISSING = object()


class Pipeline(Generic[T]):
    """A lazy, chainable pipeline over an iterable of records.

    Example:
        >>> Pipeline([1, 2, 3, 4]).where(lambda n: n % 2 == 0).to_list()
        [2, 4]
        >>> Pipeline([1, 2, 3]).select(lambda n: n * 10).to_list()
        [10, 20, 30]
        >>> Pipeline([3, 1, 2]).sort_by(lambda n: n).to_list()
        [1, 2, 3]
    """

    def __init__(self, source: Iterable[T]) -> None:
        self._source = source

    def __iter__(self) -> Iterator[T]:
        """Iterate the underlying source lazily.

        Example:
            >>> list(iter(Pipeline([1, 2])))
            [1, 2]
        """
        return iter(self._source)

    def where(self, predicate: Callable[[T], bool]) -> Pipeline[T]:
        """Keep only records for which ``predicate`` is true (lazy).

        Example:
            >>> Pipeline(range(5)).where(lambda n: n > 2).to_list()
            [3, 4]
        """
        return Pipeline(item for item in self._source if predicate(item))

    def select(self, selector: Callable[[T], R]) -> Pipeline[R]:
        """Project each record through ``selector`` (lazy).

        Example:
            >>> Pipeline([1, 2]).select(lambda n: str(n)).to_list()
            ['1', '2']
        """
        return Pipeline(selector(item) for item in self._source)

    def sort_by(self, key: Callable[[T], Any], *, reverse: bool = False) -> Pipeline[T]:
        """Return a pipeline sorted by ``key`` (blocking — consumes the source).

        Example:
            >>> Pipeline([3, 1, 2]).sort_by(lambda n: n, reverse=True).to_list()
            [3, 2, 1]
        """
        return Pipeline(sorted(self._source, key=key, reverse=reverse))

    def take(self, count: int) -> Pipeline[T]:
        """Yield at most the first ``count`` records (lazy).

        Example:
            >>> Pipeline(itertools.count()).take(3).to_list()
            [0, 1, 2]
        """
        return Pipeline(itertools.islice(self._source, max(count, 0)))

    @overload
    def first(self, predicate: Callable[[T], bool] | None = ...) -> T | None: ...
    @overload
    def first(self, predicate: Callable[[T], bool] | None, *, default: R) -> T | R: ...

    def first(self, predicate: Callable[[T], bool] | None = None, *, default: Any = None) -> Any:
        """Return the first matching record, or ``default`` if none (lazy — stops early).

        Example:
            >>> Pipeline([1, 2, 3]).first(lambda n: n > 1)
            2
            >>> Pipeline([1, 2, 3]).first(lambda n: n > 9, default=-1)
            -1
        """
        for item in self._source:
            if predicate is None or predicate(item):
                return item
        return default

    def to_list(self) -> list[T]:
        """Materialize the pipeline into a list.

        Example:
            >>> Pipeline(range(3)).to_list()
            [0, 1, 2]
        """
        return list(self._source)


__all__ = ["Pipeline"]
