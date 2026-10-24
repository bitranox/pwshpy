"""Bounded child-process runner (``run_bounded``): os_agnostic, real ``spawn`` children.

No mocking: each case starts a real ``spawn`` child that imports its target from
``bounded_targets`` by module name, so the same tests prove on Linux, macOS and
Windows that a child can find its target, that a hang is killed within the bound,
and that a child exception crosses the pipe as a typed error.
"""

from __future__ import annotations

import json
import multiprocessing
import multiprocessing.spawn
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from bounded_targets import boom, die, echo, hang, run_bounded_and_report

from pwshpy.adapters.native.bounded import run_bounded
from pwshpy.domain.errors import NativeCallError, OperationTimeoutError


@pytest.mark.os_agnostic
def test_returns_the_child_result() -> None:
    assert run_bounded(echo, ({"a": [1, 2]},), timeout=30) == {"a": [1, 2]}


@pytest.mark.os_agnostic
def test_a_result_shaped_like_a_failure_message_comes_back_as_data() -> None:
    """The child tags its message outside the payload, so a result that reads like an error is not one."""
    assert run_bounded(echo, (("error", "kaputt"),), timeout=30) == ("error", "kaputt")


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


_START_FAILED = r"could not be started"
# A real integer exit code: "exit code None" would mean the child was never reaped.
_EXITED_WITH_CODE = r"exited without a result \(exit code -?[0-9]+\)"


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
    """An unstartable child is a NativeCallError at once, never a raw OSError, a hang, or a leftover.

    Where the missing interpreter surfaces depends on the platform: Windows' CreateProcess fails
    inside start(), and so does POSIX spawn on the CPython builds whose launcher closes the
    child's read end before writing to it (EPIPE).  Other POSIX builds (measured: 3.10-3.12,
    3.14.0) return from start() and the child dies with exit code 255, which run_bounded reports
    as a worker without a result.  Both are correct; only Windows is pinned to the start path.
    """
    before = set(multiprocessing.active_children())
    started = time.monotonic()
    with pytest.raises(NativeCallError) as caught:
        run_bounded(echo, (1,), timeout=30)
    assert time.monotonic() - started < 20  # reported at once, not after the 30 s timeout
    assert not isinstance(caught.value, OperationTimeoutError)
    expected = _START_FAILED if sys.platform == "win32" else f"{_START_FAILED}|{_EXITED_WITH_CODE}"
    assert re.search(expected, str(caught.value)), str(caught.value)
    assert set(multiprocessing.active_children()) - before == set()


# A script that calls run_bounded at module level with no __main__ guard: the spawn child
# re-imports it, calls run_bounded again while still bootstrapping, and dies before it has
# unpickled its arguments.  Only the top-level process reports the outcome.
_UNGUARDED_SCRIPT = """
import json, time
from pwshpy.adapters.native.bounded import run_bounded
started = time.monotonic()
try:
    run_bounded(abs, (-3,), timeout=60)
    outcome = "returned"
except Exception as exc:
    if __name__ != "__main__":
        raise
    outcome = type(exc).__name__
if __name__ == "__main__":
    print(json.dumps({"outcome": outcome, "seconds": time.monotonic() - started}))
"""


@pytest.mark.os_agnostic
def test_a_child_that_dies_while_bootstrapping_is_reported_at_once(tmp_path: Path) -> None:
    """A child that dies before unpickling its arguments is a dead worker, not a 60 s timeout.

    On Windows the parent itself holds a duplicate of the child's write end until the child
    unpickles it, so the pipe never reports EOF: only the process sentinel shows the death.
    """
    script = tmp_path / "unguarded.py"
    script.write_text(_UNGUARDED_SCRIPT, encoding="utf-8")
    completed = subprocess.run(  # noqa: S603 - this interpreter on a script the test wrote
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    lines = completed.stdout.strip().splitlines()
    assert lines, completed.stderr
    report = json.loads(lines[-1])
    assert report["outcome"] == "NativeCallError", completed.stderr
    assert report["seconds"] < 30, report  # reported when the child dies, not after the 60 s timeout


@pytest.mark.os_agnostic
def test_inside_a_daemonic_worker_raises_a_typed_error() -> None:
    """A daemonic process cannot start children (a multiprocessing.Pool worker is one).

    multiprocessing refuses with a bare AssertionError; run_bounded must say so as a
    NativeCallError that names the cause, like every other failure to start its child.
    """
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=run_bounded_and_report, args=(sender,), daemon=True)
    worker.start()
    sender.close()
    try:
        assert receiver.poll(60), "the daemonic worker sent no outcome"
        outcome = receiver.recv()
    finally:
        receiver.close()
        worker.join(30)
        if worker.is_alive():
            worker.kill()
            worker.join()
    assert isinstance(outcome, NativeCallError), repr(outcome)
    assert not isinstance(outcome, OperationTimeoutError)
    assert re.search(r"echo worker could not be started: .*daemonic", str(outcome)), str(outcome)
    assert worker.exitcode == 0
