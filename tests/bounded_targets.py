"""Module-level targets for run_bounded tests: a spawn child must import them by name."""

from __future__ import annotations

import os
import time
from typing import Protocol

from pwshpy.adapters.native.bounded import run_bounded


class _Sender(Protocol):
    """The write end of a Pipe: a Connection on POSIX and a PipeConnection on Windows."""

    def send(self, obj: object) -> None: ...

    def close(self) -> None: ...


def echo(value: object) -> object:
    return value


def hang(_seconds: float) -> None:
    time.sleep(3600)


def boom(message: str) -> None:
    raise RuntimeError(message)


def die(code: int) -> None:
    # os._exit skips every handler, so the child sends nothing: the parent sees only the pipe close.
    os._exit(code)


def run_bounded_and_report(conn: _Sender) -> None:
    """Call run_bounded here and send back what it raised (or None), for a daemonic worker test."""
    try:
        run_bounded(echo, (1,), timeout=30)
        conn.send(None)
    # BaseException: an AssertionError (the unwrapped failure) must reach the parent as data too.
    except BaseException as exc:
        conn.send(exc)
    finally:
        conn.close()
