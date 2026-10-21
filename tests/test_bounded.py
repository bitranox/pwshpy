"""Bounded child-process runner (``run_bounded``): os_agnostic, real ``spawn`` children.

No mocking: each case starts a real ``spawn`` child that imports its target from
``bounded_targets`` by module name, so the same tests prove on Linux, macOS and
Windows that a child can find its target, that a hang is killed within the bound,
and that a child exception crosses the pipe as a typed error.
"""

from __future__ import annotations

import multiprocessing
import multiprocessing.spawn
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from bounded_targets import boom, die, echo, hang

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
def test_a_timed_out_child_is_not_left_running() -> None:
    """The timeout bound alone passes even if the child is only abandoned; this pins the kill."""
    with pytest.raises(OperationTimeoutError):
        run_bounded(hang, (0.0,), timeout=1)
    assert multiprocessing.active_children() == []


@pytest.mark.os_agnostic
def test_a_child_exception_becomes_native_call_error() -> None:
    with pytest.raises(NativeCallError, match="kaputt") as caught:
        run_bounded(boom, ("kaputt",), timeout=30)
    assert not isinstance(caught.value, OperationTimeoutError)


@pytest.mark.os_agnostic
def test_a_child_that_dies_without_a_result_becomes_native_call_error() -> None:
    """A crash is not a timeout: it is reported at once, with the exit code, and leaves no child."""
    before = set(multiprocessing.active_children())
    started = time.monotonic()
    with pytest.raises(NativeCallError, match="exit code 3") as caught:
        run_bounded(die, (3,), timeout=30)
    assert time.monotonic() - started < 20  # reported when the child dies, not after the 30 s timeout
    assert not isinstance(caught.value, OperationTimeoutError)
    assert set(multiprocessing.active_children()) - before == set()


@pytest.fixture
def missing_interpreter(tmp_path: Path) -> Iterator[None]:
    """Point spawn at an interpreter that does not exist, so the OS itself refuses the start."""
    # set_executable is multiprocessing's own seam for the child interpreter; restored afterwards.
    original = multiprocessing.spawn.get_executable()
    multiprocessing.spawn.set_executable(str(tmp_path / "no-such-python"))
    try:
        yield
    finally:
        multiprocessing.spawn.set_executable(original)


@pytest.mark.os_agnostic
@pytest.mark.usefixtures("missing_interpreter")
def test_a_child_that_cannot_be_started_becomes_native_call_error() -> None:
    with pytest.raises(NativeCallError, match="could not be started") as caught:
        run_bounded(echo, (1,), timeout=30)
    assert not isinstance(caught.value, OperationTimeoutError)
