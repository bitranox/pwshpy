"""Domain error types: instantiation and message preservation."""

from __future__ import annotations

import copy
import pickle
from collections.abc import Callable

import pytest

from pwshpy.domain.errors import ConfigurationError, OperationTimeoutError


@pytest.mark.os_agnostic
def test_configuration_error_preserves_message() -> None:
    """Instantiation stores the message for display."""
    exc = ConfigurationError("output format not configured")
    assert str(exc) == "output format not configured"


def _pickle_round_trip(exc: BaseException) -> BaseException:
    """What a multiprocessing pipe does to an error: dumps in one process, loads in the other."""
    # The loaded bytes are the ones this line just dumped, so the untrusted-data warning has no subject.
    rebuilt: BaseException = pickle.loads(pickle.dumps(exc))  # noqa: S301 - self-produced bytes
    return rebuilt


def _deepcopy_round_trip(exc: BaseException) -> BaseException:
    return copy.deepcopy(exc)


_ROUND_TRIPS: list[Callable[[BaseException], BaseException]] = [_pickle_round_trip, _deepcopy_round_trip]


class _LabelledTimeoutError(OperationTimeoutError):
    """A subclass with its own keyword and attribute: pickling must keep both, and the type."""

    def __init__(self, message: str, *, timeout: float, label: str) -> None:
        super().__init__(message, timeout=timeout)
        self.label = label


@pytest.mark.os_agnostic
@pytest.mark.parametrize("round_trip", _ROUND_TRIPS, ids=["pickle", "deepcopy"])
def test_operation_timeout_error_survives_a_round_trip(round_trip: Callable[[BaseException], BaseException]) -> None:
    """The keyword-only ``timeout`` is not in ``args``, so the default reduce cannot rebuild it."""
    rebuilt = round_trip(OperationTimeoutError("slow call", timeout=1.5))
    assert type(rebuilt) is OperationTimeoutError
    assert isinstance(rebuilt, OperationTimeoutError)
    assert rebuilt.timeout == 1.5
    assert str(rebuilt) == "slow call"


@pytest.mark.os_agnostic
@pytest.mark.parametrize("round_trip", _ROUND_TRIPS, ids=["pickle", "deepcopy"])
def test_an_operation_timeout_error_subclass_survives_a_round_trip(
    round_trip: Callable[[BaseException], BaseException],
) -> None:
    rebuilt = round_trip(_LabelledTimeoutError("slow call", timeout=2.0, label="wu-search"))
    assert type(rebuilt) is _LabelledTimeoutError
    assert isinstance(rebuilt, _LabelledTimeoutError)
    assert (rebuilt.timeout, rebuilt.label, str(rebuilt)) == (2.0, "wu-search", "slow call")
