"""Run a call in a killable child process, bounded by a timeout (portable, stdlib).

A thread cannot stop a blocked COM call (Windows Update's Search can sit for 25+ minutes),
so the call runs in a ``spawn`` child that the parent kills on timeout.  ``spawn`` on every
OS so the behaviour tested on Linux is the behaviour shipped on Windows.

The target and its arguments cross into the child by pickle, so the target must be a
module-level callable the child can import by name (no lambda, no nested function), and
the result must be picklable to cross back.

Contents:
    * :func:`run_bounded` - return ``target(*args)`` computed in a child, or raise on timeout.
"""

from __future__ import annotations

import multiprocessing
import sys
from collections.abc import Callable
from enum import Enum
from multiprocessing.connection import wait
from multiprocessing.context import SpawnContext, SpawnProcess
from typing import Any, NamedTuple

from ...domain.errors import NativeCallError, OperationTimeoutError

# Pipe() returns a PipeConnection on Windows and a Connection elsewhere; the two share only the
# private _ConnectionBase, so name the concrete class per platform (pyright narrows on
# sys.platform) rather than annotate with Connection, which is wrong on Windows.
if sys.platform == "win32":
    from multiprocessing.connection import PipeConnection as PipeEnd
else:
    from multiprocessing.connection import Connection as PipeEnd

# How long the parent waits for a killed (or finished) child to be reaped, so a call never
# leaves a zombie behind and never blocks on a child that ignores the kill.
_KILL_GRACE_SECONDS = 5.0


class _MessageKind(str, Enum):
    """What the child's one message carries: the target's result, or the text of its failure.

    Module-level so it pickles by name across the spawn pipe; an Enum member unpickles to the
    same singleton, so the parent compares it with ``is``.
    """

    OK = "ok"
    ERROR = "error"


class _Message(NamedTuple):
    """The child's one message: ``payload`` is the result for OK, ``"<Type>: <text>"`` for ERROR."""

    kind: _MessageKind
    payload: Any


def _child(conn: PipeEnd, target: Callable[..., Any], args: tuple[Any, ...]) -> None:
    try:
        conn.send(_Message(_MessageKind.OK, target(*args)))
    # BaseException, not Exception: every failure (SystemExit and KeyboardInterrupt included)
    # must cross the pipe as data, or the parent sees only an EOF with no cause.
    except BaseException as exc:
        conn.send(_Message(_MessageKind.ERROR, f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def run_bounded(target: Callable[..., Any], args: tuple[Any, ...], *, timeout: float) -> Any:
    """Return ``target(*args)`` computed in a ``spawn`` child, killed after ``timeout`` seconds.

    Args:
        target: A module-level callable the child imports by name.
        args: Positional arguments for ``target``; must be picklable.
        timeout: Seconds to wait for the result before the child is killed.

    Returns:
        Whatever ``target(*args)`` returned in the child (it must be picklable).

    Raises:
        OperationTimeoutError: If no result arrives within ``timeout`` (the child is killed).
        NativeCallError: If ``target`` raised in the child (its type and message are kept
            in the message), the child exited without sending a result (its exit code is
            in the message), or the child could not be started - including when the caller
            is itself a daemonic process (a ``multiprocessing.Pool`` worker), which may not
            start children.

    Example:
        >>> run_bounded(abs, (-3,), timeout=30)
        3
    """
    name = getattr(target, "__name__", "call")
    _refuse_in_daemonic_process(name)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    try:
        process = _start(context, child, target, args)
        message = _await_result(parent, process, name=name, timeout=timeout)
    finally:
        parent.close()
    if message.kind is _MessageKind.ERROR:
        raise NativeCallError(str(message.payload))
    return message.payload


def _refuse_in_daemonic_process(name: str) -> None:
    """Raise NativeCallError when the calling process is daemonic, before any pipe or child exists.

    multiprocessing refuses a child of a daemonic process with a bare ``assert`` in
    ``BaseProcess.start`` (the same test of ``current_process().daemon`` in CPython 3.10 to
    3.15), so without this check the caller gets an AssertionError that is no PwshPyError, and
    under ``python -O`` the assert is stripped and the child starts after all.  Checking the same
    flag up front gives one typed answer on every version and in every optimisation mode.
    """
    if multiprocessing.current_process().daemon:
        raise NativeCallError(
            f"{name} worker could not be started: this process is a daemonic multiprocessing worker"
            " (a multiprocessing.Pool worker is one), and daemonic processes cannot start children;"
            " call it from a non-daemonic process, such as a concurrent.futures.ProcessPoolExecutor"
            " worker or a Process started with daemon=False"
        )


def _start(context: SpawnContext, child: PipeEnd, target: Callable[..., Any], args: tuple[Any, ...]) -> SpawnProcess:
    """Start the child, or raise NativeCallError when the OS cannot spawn it."""
    process = context.Process(target=_child, args=(child, target, args), daemon=True)
    # Only OSError (the OS cannot spawn) is wrapped: a PicklingError from a lambda or nested
    # target is a programming error and stays raw.
    try:
        process.start()
    except OSError as exc:
        name = getattr(target, "__name__", "call")
        raise NativeCallError(f"{name} worker could not be started: {exc}") from exc
    finally:
        # The parent's copy of the write end must close, or recv() never sees EOF when the child
        # dies; on a failed start it would otherwise stay open until garbage collection.
        child.close()
    return process


def _await_result(parent: PipeEnd, process: SpawnProcess, *, name: str, timeout: float) -> _Message:
    """Return the child's message; the child is reaped on every path."""
    try:
        # Wait on the process sentinel too, not only the pipe: on Windows the parent holds a
        # duplicate of the child's write end until the child unpickles it, so a child that dies
        # earlier (a script without a __main__ guard dies while bootstrapping) never closes the
        # pipe, and a pipe-only wait would run out the whole timeout instead.
        if not wait([parent, process.sentinel], timeout):
            raise OperationTimeoutError(f"{name} timed out after {timeout}s", timeout=timeout)
        # Only the sentinel fired: the child is gone.  A result it sent before exiting is still
        # buffered (poll is True, recv returns it); with none, recv sees EOF on POSIX, while on
        # Windows the duplicate keeps the pipe open, so report the dead child directly.
        if not parent.poll():
            raise EOFError
        message: _Message = parent.recv()
    # A child that dies sends nothing; the parent sees its write end close.  recv() reports that
    # as EOFError on every OS, but on Windows PipeConnection.poll calls PeekNamedPipe unwrapped,
    # which raises BrokenPipeError instead when the child died before the parent reached poll.
    except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
        _reap_exited(process)
        raise NativeCallError(f"{name} worker exited without a result (exit code {process.exitcode})") from exc
    finally:
        _reap(process)
    return message


def _reap(process: SpawnProcess) -> None:
    """Kill the child if it still runs and wait for it, so no call leaves a zombie behind."""
    if process.is_alive():
        process.kill()
    process.join(_KILL_GRACE_SECONDS)


def _reap_exited(process: SpawnProcess) -> None:
    """Wait for a child whose pipe already closed, and kill it only if it does not exit.

    The pipe closes while the child is still shutting down, so killing first would replace its
    real exit code with the kill's; that code is the only clue the caller gets about why no
    result came.
    """
    process.join(_KILL_GRACE_SECONDS)
    _reap(process)


__all__ = ["run_bounded"]
