"""Copy an exception the two ways it leaves the raising frame: pickle and deepcopy.

A multiprocessing pipe pickles an error in one process and loads it in another, and
``copy.deepcopy`` goes through the same ``__reduce__``; a test runs a check over both.
Generic over the exception type, so a caller reads the subclass's own attributes
(``hresult``, ``errno``) from the copy without a cast; the tests assert the copy's type.

Imported by bare module name (``from exception_round_trips import ...``): pytest puts ``tests`` on the path.
"""

from __future__ import annotations

import copy
import pickle
from typing import TypeVar

_E = TypeVar("_E", bound=BaseException)


def pickle_round_trip(exc: _E) -> _E:
    """What a multiprocessing pipe does to an error: dumps in one process, loads in the other.

    Example:
        >>> str(pickle_round_trip(ValueError("x")))
        'x'
    """
    # The loaded bytes are the ones this line just dumped, so the untrusted-data warning has no subject.
    rebuilt: _E = pickle.loads(pickle.dumps(exc))  # noqa: S301 - self-produced bytes
    return rebuilt


def deepcopy_round_trip(exc: _E) -> _E:
    """``copy.deepcopy``, which rebuilds an exception through the same ``__reduce__`` as pickle.

    Example:
        >>> str(deepcopy_round_trip(ValueError("x")))
        'x'
    """
    return copy.deepcopy(exc)


__all__ = ["deepcopy_round_trip", "pickle_round_trip"]
