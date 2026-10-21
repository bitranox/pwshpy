"""Module-level targets for run_bounded tests: a spawn child must import them by name."""

from __future__ import annotations

import time


def echo(value: object) -> object:
    return value


def hang(_seconds: float) -> None:
    time.sleep(3600)


def boom(message: str) -> None:
    raise RuntimeError(message)
