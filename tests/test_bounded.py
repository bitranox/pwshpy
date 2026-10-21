"""Bounded child-process runner (``run_bounded``): os_agnostic, real ``spawn`` children.

No mocking: each case starts a real ``spawn`` child that imports its target from
``bounded_targets`` by module name, so the same tests prove on Linux, macOS and
Windows that a child can find its target, that a hang is killed within the bound,
and that a child exception crosses the pipe as a typed error.
"""

from __future__ import annotations

import time

import pytest
from bounded_targets import boom, echo, hang

from pwshpy.adapters.native.bounded import run_bounded
from pwshpy.domain.errors import NativeCallError, OperationTimeoutError


@pytest.mark.os_agnostic
def test_returns_the_child_result() -> None:
    assert run_bounded(echo, ({"a": [1, 2]},), timeout=30) == {"a": [1, 2]}


@pytest.mark.os_agnostic
def test_a_hang_is_killed_and_reported_as_a_timeout() -> None:
    started = time.monotonic()
    with pytest.raises(OperationTimeoutError) as caught:
        run_bounded(hang, (0.0,), timeout=2)
    assert time.monotonic() - started < 20  # bounded: kill + join, not the child's 3600 s sleep
    assert caught.value.timeout == 2


@pytest.mark.os_agnostic
def test_a_child_exception_becomes_native_call_error() -> None:
    with pytest.raises(NativeCallError, match="kaputt") as caught:
        run_bounded(boom, ("kaputt",), timeout=30)
    assert not isinstance(caught.value, OperationTimeoutError)
