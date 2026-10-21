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
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

from ...domain.errors import NativeCallError, OperationTimeoutError

# How long the parent waits for a killed (or finished) child to be reaped, so a call never
# leaves a zombie behind and never blocks on a child that ignores the kill.
_KILL_GRACE_SECONDS = 5.0


def _child(conn: Connection, target: Callable[..., Any], args: tuple[Any, ...]) -> None:
    try:
        conn.send(("ok", target(*args)))
    # BaseException, not Exception: every failure (SystemExit and KeyboardInterrupt included)
    # must cross the pipe as data, or the parent sees only an EOF with no cause.
    except BaseException as exc:
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
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
            in the message), or the child exited without sending a result.

    Example:
        >>> run_bounded(abs, (-3,), timeout=30)
        3
    """
    name = getattr(target, "__name__", "call")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(child, target, args), daemon=True)
    process.start()
    # The parent's copy of the write end must close, or recv() never sees EOF when the child dies.
    child.close()
    try:
        if not parent.poll(timeout):
            raise OperationTimeoutError(f"{name} timed out after {timeout}s", timeout=timeout)
        kind, payload = parent.recv()
    except EOFError as exc:
        raise NativeCallError(f"{name} worker exited without a result") from exc
    finally:
        if process.is_alive():
            process.kill()
        process.join(_KILL_GRACE_SECONDS)
        parent.close()
    if kind == "error":
        raise NativeCallError(str(payload))
    return payload


__all__ = ["run_bounded"]
