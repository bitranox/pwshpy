"""native external-process runner: os_agnostic, hermetic tests.

Every case drives the current Python interpreter (``sys.executable``) so it runs
identically on Linux, macOS, and Windows without mocking - proving the real
contract: argv is passed as data (no shell), exit_code is always captured, stderr
is data, stdin/env/timeout/missing-exe all behave.
"""

from __future__ import annotations

import os
import sys

import pytest

from pwshpy.adapters.native.process_exec import run_process
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import ProcessResult


@pytest.mark.os_agnostic
def test_captures_stdout_and_zero_exit() -> None:
    """A successful run returns a ProcessResult with captured stdout and exit 0."""
    result = run_process([sys.executable, "-c", "print('hello')"])
    assert isinstance(result, ProcessResult)
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.succeeded is True
    assert result.duration_s >= 0.0


@pytest.mark.os_agnostic
def test_captures_stderr_and_nonzero_exit() -> None:
    """stderr is captured as plain data and a nonzero exit is reported, not raised."""
    result = run_process([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
    assert result.exit_code == 3
    assert "boom" in result.stderr
    assert result.succeeded is False


@pytest.mark.os_agnostic
def test_check_raises_on_nonzero_exit() -> None:
    """.check() raises NativeCallError when the process failed."""
    result = run_process([sys.executable, "-c", "import sys; sys.exit(2)"])
    with pytest.raises(NativeCallError):
        result.check()


@pytest.mark.os_agnostic
def test_check_returns_self_on_success() -> None:
    """.check() returns the result unchanged when the process succeeded."""
    result = run_process([sys.executable, "-c", ""])
    assert result.check() is result


@pytest.mark.os_agnostic
def test_argv_is_data_never_shell_interpreted() -> None:
    """A shell-metacharacter argument reaches the child verbatim (no shell in the middle)."""
    payload = "; echo owned && rm -rf /"
    result = run_process([sys.executable, "-c", "import sys; print(sys.argv[1])", payload])
    assert result.stdout.strip() == payload


@pytest.mark.os_agnostic
def test_input_text_is_piped_to_stdin() -> None:
    """input_text is delivered on the child's stdin."""
    result = run_process(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        input_text="abc",
    )
    assert result.stdout.strip() == "ABC"


@pytest.mark.os_agnostic
def test_env_replaces_child_environment() -> None:
    """A provided env is the child's full environment (here: os.environ plus one var)."""
    result = run_process(
        [sys.executable, "-c", "import os; print(os.environ.get('PWSHPY_EXEC_PROBE', 'MISSING'))"],
        env={**os.environ, "PWSHPY_EXEC_PROBE": "present"},
    )
    assert result.stdout.strip() == "present"


@pytest.mark.os_agnostic
def test_timeout_kills_and_raises() -> None:
    """A run that outlives its timeout is killed and raises NativeCallError."""
    with pytest.raises(NativeCallError):
        run_process([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)


@pytest.mark.os_agnostic
def test_missing_executable_raises() -> None:
    """A nonexistent executable raises NativeCallError, not a bare FileNotFoundError."""
    with pytest.raises(NativeCallError):
        run_process(["pwshpy-definitely-not-a-real-binary-xyz"])


@pytest.mark.os_agnostic
def test_empty_argv_raises() -> None:
    """An empty argv list is rejected up front."""
    with pytest.raises(NativeCallError):
        run_process([])
