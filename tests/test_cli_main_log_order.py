"""The CLI's error line comes after the log lines the failing command already emitted.

lib_log_rich renders console events on a queue worker thread, while ``main()`` prints the error
that ends a command on the calling thread. When the error was printed with an INFO line still
queued, both threads wrote to stderr at once and the two lines interleaved (measured on Windows:
``[INFO]: Looking up scheduled task 'a\\b'ConfigurationError: ...`` with both newlines at the end).
These tests slow the console worker down through lib_log_rich's own adapter seam, so the queued
line is guaranteed to be pending when the error is printed.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import lib_log_rich.runtime
import pytest
from fake_services import services_with_ps
from lib_layered_config import Config

from pwshpy.adapters.cli.main import main
from pwshpy.composition import AppServices
from pwshpy.domain.errors import ConfigurationError

if TYPE_CHECKING:
    from lib_log_rich.domain.events import LogEvent
    from lib_log_rich.runtime import ConsoleAppearance

#: Long enough that, without a drain, the error is printed while the INFO line is still queued.
_CONSOLE_DELAY_SECONDS = 0.3
_REFUSAL = "task_name 'a\\b' contains a backslash"


class _SlowStderrConsole:
    """A console port that renders each event to ``sys.stderr`` after a delay, in one write."""

    def emit(self, event: LogEvent, *, colorize: bool) -> None:
        del colorize
        time.sleep(_CONSOLE_DELAY_SECONDS)
        sys.stderr.write(f"[{event.level.name}]: {event.message}\n")

    def flush(self) -> None:
        sys.stderr.flush()


def _slow_console(_appearance: ConsoleAppearance) -> _SlowStderrConsole:
    return _SlowStderrConsole()


def _init_queued_logging(_config: Config, *, dotenv_path: str | None = None) -> None:
    """The production shape that matters here: the console renders on the queue worker."""
    del dotenv_path
    if lib_log_rich.runtime.is_initialised():
        return
    lib_log_rich.runtime.init(
        lib_log_rich.runtime.RuntimeConfig(
            service="pwshpy-test",
            environment="test",
            queue_enabled=True,
            console_level="INFO",
            console_adapter_factory=_slow_console,
        )
    )
    # The commands log through stdlib logging, as in production (see ``init_logging``).
    lib_log_rich.runtime.attach_std_logging()


class _RefusingScheduledTasks:
    """A facade double whose task lookup refuses the name, as the native adapter does."""

    def get_scheduled_task(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ConfigurationError(_REFUSAL)


def _services() -> Callable[[], AppServices]:
    config = Config({}, {})

    def get_config(**_kwargs: Any) -> Config:
        return config

    def build() -> AppServices:
        base = services_with_ps(_RefusingScheduledTasks())
        return dataclasses.replace(base, get_config=get_config, init_logging=_init_queued_logging)

    return build


@pytest.fixture
def no_runtime() -> Iterator[None]:
    """Start and end without a lib_log_rich runtime, and restore the root logger it attached to."""
    if lib_log_rich.runtime.is_initialised():
        lib_log_rich.runtime.shutdown()
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    if lib_log_rich.runtime.is_initialised():
        lib_log_rich.runtime.shutdown()
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.mark.os_agnostic
@pytest.mark.usefixtures("no_runtime")
def test_the_error_line_follows_the_info_line_it_was_logged_after(capsys: pytest.CaptureFixture[str]) -> None:
    """Each line stays whole and in causal order: the INFO line, then the error, never one line."""
    exit_code = main(["get_scheduled_task", "--task-name", "a\\b"], services_factory=_services())

    assert exit_code != 0
    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert lines[0] == "[INFO]: Looking up scheduled task 'a\\\\b'"
    assert lines[1].startswith("ConfigurationError: ") and _REFUSAL in lines[1]
    assert len(lines) == 2
