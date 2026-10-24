"""native external-process runner: os_agnostic, hermetic tests.

Every case drives the current Python interpreter (``sys.executable``) so it runs
identically on Linux, macOS, and Windows without mocking - proving the real
contract: argv is passed as data (no shell), exit_code is always captured, stderr
is data, stdin/env/timeout/missing-exe all behave.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pytest
from process_tree_programs import finished, is_stopped, parent_argv, wait_for_beats

from pwshpy.adapters.native.process_exec import run_process
from pwshpy.adapters.native.process_tree import ProcessTree, _Reader  # pyright: ignore[reportPrivateUsage]
from pwshpy.composition import build_ps
from pwshpy.domain.errors import NativeCallError, OperationTimeoutError
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
def test_timeout_raises_operation_timeout_error() -> None:
    """[130]: a run that outlives its timeout is killed and raises the typed timeout, still a NativeCallError."""
    with pytest.raises(OperationTimeoutError) as caught:
        build_ps().exec([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert isinstance(caught.value, NativeCallError)  # existing `except NativeCallError` callers keep working
    assert caught.value.timeout == 0.5
    assert "exceeded its 0.5s timeout" in str(caught.value)  # message kept


@pytest.mark.os_agnostic
def test_missing_executable_raises() -> None:
    """A nonexistent executable raises NativeCallError, never the timeout type (the item's whole point)."""
    with pytest.raises(NativeCallError) as caught:
        run_process(["pwshpy-definitely-not-a-real-binary-xyz"])
    assert not isinstance(caught.value, OperationTimeoutError)


@pytest.mark.os_agnostic
def test_empty_argv_raises() -> None:
    """An empty argv list is rejected up front."""
    with pytest.raises(NativeCallError):
        run_process([])


# --- [52] wait_tree: wait for every process the started program spawned -------------------------

_GRANDCHILD_SECONDS = 3.0
_ON_MACOS = sys.platform == "darwin"
_MACOS_GAP = "macOS has no subreaper: a setsid/daemonized grandchild leaves the process group (documented gap)"


def _timed_exec(argv: list[str], *, wait_tree: bool = False) -> tuple[ProcessResult, float]:
    started = time.monotonic()
    result = run_process(argv, wait_tree=wait_tree, timeout=120)
    return result, time.monotonic() - started


@pytest.mark.os_agnostic
def test_wait_tree_waits_for_a_background_grandchild(tmp_path: Path) -> None:
    """wait_tree=True returns only after the grandchild the program started has finished."""
    base = tmp_path / "bg"
    result, elapsed = _timed_exec(parent_argv("background", base, _GRANDCHILD_SECONDS), wait_tree=True)
    assert finished(base), f"returned after {elapsed:.2f}s while the grandchild still ran"
    assert elapsed >= _GRANDCHILD_SECONDS
    assert result.exit_code == 0
    assert "parent done" in result.stdout


@pytest.mark.os_agnostic
@pytest.mark.skipif(_ON_MACOS, reason=_MACOS_GAP)
def test_wait_tree_waits_for_a_daemonized_grandchild(tmp_path: Path) -> None:
    """A grandchild that double-forks + setsid (POSIX) or is DETACHED (Windows) is still waited for."""
    base = tmp_path / "daemon"
    result, elapsed = _timed_exec(parent_argv("daemon", base, _GRANDCHILD_SECONDS), wait_tree=True)
    assert finished(base), f"returned after {elapsed:.2f}s while the daemonized grandchild still ran"
    assert elapsed >= _GRANDCHILD_SECONDS
    assert result.exit_code == 0


@pytest.mark.os_macos
@pytest.mark.skipif(not _ON_MACOS, reason="pins the macOS-only gap")
def test_wait_tree_on_macos_does_not_cover_a_daemonized_grandchild(tmp_path: Path) -> None:
    """The documented macOS gap: a setsid grandchild leaves the process group and is not waited for."""
    base = tmp_path / "daemon"
    result, elapsed = _timed_exec(parent_argv("daemon", base, _GRANDCHILD_SECONDS), wait_tree=True)
    assert result.exit_code == 0
    assert elapsed < _GRANDCHILD_SECONDS
    assert not finished(base)


@pytest.mark.os_agnostic
def test_without_wait_tree_returns_before_the_grandchild(tmp_path: Path) -> None:
    """The default (wait_tree=False) keeps today's behaviour: only the started program is waited for."""
    base = tmp_path / "default"
    result, elapsed = _timed_exec(parent_argv("background", base, _GRANDCHILD_SECONDS))
    assert result.exit_code == 0
    assert elapsed < _GRANDCHILD_SECONDS
    assert not finished(base)


def _assert_timeout_kills_the_tree(tmp_path: Path, mode: str) -> None:
    base = tmp_path / mode
    started = time.monotonic()
    with pytest.raises(OperationTimeoutError) as caught:
        run_process(parent_argv(mode, base, 60.0), wait_tree=True, timeout=3.0)
    elapsed = time.monotonic() - started
    assert caught.value.timeout == 3.0
    assert elapsed < 20.0  # the 60 s grandchild was not waited out
    wait_for_beats(base)  # it really ran, so a quiet heartbeat below means it was killed
    assert is_stopped(base), "the grandchild kept running after the timeout"
    assert not finished(base)


@pytest.mark.os_agnostic
def test_wait_tree_timeout_kills_the_whole_tree(tmp_path: Path) -> None:
    """On timeout the tree is killed (nothing left running) and OperationTimeoutError is raised."""
    _assert_timeout_kills_the_tree(tmp_path, "background")


@pytest.mark.os_agnostic
@pytest.mark.skipif(_ON_MACOS, reason=_MACOS_GAP)
def test_wait_tree_timeout_kills_a_daemonized_grandchild(tmp_path: Path) -> None:
    """The kill on timeout reaches a grandchild that left the process group / console too."""
    _assert_timeout_kills_the_tree(tmp_path, "daemon")


@pytest.mark.os_agnostic
def test_wait_tree_captures_output_of_a_grandchild_that_inherits_stdout(tmp_path: Path) -> None:
    """A grandchild that keeps the inherited stdout contributes its output to the result."""
    result = run_process(parent_argv("inherit", tmp_path / "inherit", 1.0), wait_tree=True, timeout=60)
    assert "parent done" in result.stdout
    assert "grandchild out" in result.stdout


@pytest.mark.os_agnostic
def test_wait_tree_reports_the_started_programs_exit_code_under_env(tmp_path: Path) -> None:
    """The exit code is the started program's own; env reaches it like the default path."""
    result = run_process(
        parent_argv("background", tmp_path / "rc", 0.5),
        wait_tree=True,
        env={**os.environ, "PARENT_EXIT": "3"},
    )
    assert result.exit_code == 3
    assert finished(tmp_path / "rc")


@pytest.mark.os_agnostic
def test_wait_tree_pipes_input_text_and_honours_cwd(tmp_path: Path) -> None:
    """input_text reaches stdin and cwd is the child's working directory under wait_tree."""
    result = run_process(
        [sys.executable, "-c", "import os, sys; print(sys.stdin.read().upper(), os.getcwd())"],
        wait_tree=True,
        input_text="abc",
        cwd=str(tmp_path),
    )
    assert result.stdout.split()[0] == "ABC"
    assert Path(result.stdout.split(maxsplit=1)[1].strip()).samefile(tmp_path)


@pytest.mark.os_agnostic
def test_wait_tree_missing_executable_raises_native_call_error() -> None:
    """A missing program is a NativeCallError naming it, never the timeout type."""
    with pytest.raises(NativeCallError, match="Executable not found") as caught:
        run_process(["pwshpy-definitely-not-a-real-binary-xyz"], wait_tree=True)
    assert not isinstance(caught.value, OperationTimeoutError)


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_wait_tree_reports_a_signal_death_like_the_default_path() -> None:
    """A program killed by a signal reports -N, exactly as subprocess (and wait_tree=False) does."""
    argv = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
    assert run_process(argv, wait_tree=True).exit_code == run_process(argv).exit_code == -9


#: What Popen itself says about a non-str argv element, env value or cwd (the helper adds no wording).
_POPEN_REFUSES_AN_INT = "Failed to run 'echo': TypeError: expected str, bytes or os.PathLike object, not int"


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the wait_tree helper runs on POSIX only")
@pytest.mark.parametrize(
    ("argv", "env"),
    [(["echo", 5], None), (["echo"], {"A": 1})],
    ids=["int-argv-element", "int-env-value"],
)
def test_wait_tree_reports_popens_own_refusal_of_a_non_str_element(argv: list[Any], env: dict[str, Any] | None) -> None:
    """A non-str argv element or env value reaches the caller as Popen's own TypeError text."""
    with pytest.raises(NativeCallError) as caught:
        build_ps().exec(argv, env=env, wait_tree=True, timeout=60)
    assert str(caught.value) == _POPEN_REFUSES_AN_INT


def _platform_tree() -> ProcessTree:
    if sys.platform == "win32":
        from pwshpy.adapters.native.process_tree_windows import WindowsJobTree

        return WindowsJobTree()
    from pwshpy.adapters.native.process_tree_posix import PosixReaperTree

    return PosixReaperTree()


@pytest.mark.os_agnostic
def test_an_aborted_tree_wait_kills_the_tree(tmp_path: Path) -> None:
    """Closing the tree without a finished wait (an exception, Ctrl-C) kills it, as subprocess.run does."""
    base = tmp_path / "abort"
    tree = _platform_tree()
    try:
        tree.launch(parent_argv("background", base, 60.0), cwd=None, env=None, with_stdin=False)
        wait_for_beats(base)
    finally:
        tree.close()
    assert is_stopped(base), "the grandchild kept running after the aborted wait"


@pytest.mark.os_agnostic
def test_tree_output_reader_returns_what_it_has_when_the_pipe_stays_open() -> None:
    """A pipe held open by a process outside the tree cannot block the result past the drain deadline."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"partial\r\n")
        reader = _Reader(os.fdopen(read_fd, "rb"))
        started = time.monotonic()
        assert reader.text(time.monotonic() + 0.5) == "partial\n"
        assert time.monotonic() - started < 5.0
    finally:
        os.close(write_fd)


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Win32 job objects")
def test_a_launch_that_finds_no_thread_to_resume_raises_and_reaps(monkeypatch: pytest.MonkeyPatch) -> None:
    """[B2]: the program is created suspended; with no thread to resume it would never run and the wait
    would never end, so launch raises NativeCallError and the suspended process is killed and reaped."""
    # Imported here: the module needs pywin32, which only a Windows install has.
    from pwshpy.adapters.native.process_tree_windows import WindowsJobTree

    launched: list[int] = []

    def _no_threads(process: psutil.Process) -> list[object]:
        launched.append(process.pid)
        return []

    # psutil is the third-party edge: no real process can be made to report no thread.
    monkeypatch.setattr(psutil.Process, "threads", _no_threads)
    tree = WindowsJobTree()
    try:
        with pytest.raises(NativeCallError, match="no thread"):
            tree.launch([sys.executable, "-c", "pass"], cwd=None, env=None, with_stdin=False)
    finally:
        tree.close()
    assert launched and not psutil.pid_exists(launched[0])
