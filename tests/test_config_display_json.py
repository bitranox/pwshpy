"""The ``display_config`` port renders JSON through record_json, and every log drain is guarded.

[160] ``config --format json`` must survive a byte that is not valid UTF-8 (a lone surrogate from
``surrogateescape``) whichever way it is reached: through the CLI, and through the injected
``display_config`` port itself, which is what the CLI calls. Before the log lines queued so far are
drained (so none lands inside the JSON), and that drain must never replace the output with its own
error: lib_log_rich's ``flush`` raises ``TimeoutError`` after its timeout and ``RuntimeError`` inside a
running event loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import lib_log_rich.runtime
import pytest
from click.testing import CliRunner
from lib_layered_config import Config

from pwshpy.adapters import cli as cli_mod
from pwshpy.adapters.config.display import display_config
from pwshpy.adapters.memory import init_logging_in_memory
from pwshpy.composition import AppServices, build_production
from pwshpy.domain.enums import OutputFormat

_BAD = chr(0xDCFF)
_FFFD = chr(0xFFFD)
_TAIL = ": not valid UTF-8; each undecodable byte written as U+FFFD"


@pytest.mark.os_agnostic
def test_display_config_json_writes_fffd_for_a_bad_byte_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    """The port's JSON branch: U+FFFD for the byte and one warning naming the path, never TypeError."""
    display_config(Config({"s": {"k": "a" + _BAD}}, {}), output_format=OutputFormat.JSON)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"s": {"k": "a" + _FFFD}}
    assert captured.err.splitlines() == ["warning: output field s.k" + _TAIL]


@pytest.mark.os_agnostic
def test_display_config_json_section_keeps_its_shape(capsys: pytest.CaptureFixture[str]) -> None:
    """``section`` prints ``{section: value}``, redacted, as lib_layered_config did."""
    display_config(Config({"s": {"k": "v", "password": "p"}, "o": 1}, {}), output_format=OutputFormat.JSON, section="s")
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"s"}
    assert data["s"]["k"] == "v"
    assert data["s"]["password"] != "p"


@pytest.mark.os_agnostic
def test_the_cli_calls_the_injected_display_config_for_json() -> None:
    """``config --format json`` goes through the ``display_config`` port, as the human view does."""
    calls: list[tuple[OutputFormat, str | None]] = []

    def recording_display(
        config: Config,
        *,
        output_format: OutputFormat = OutputFormat.HUMAN,
        section: str | None = None,
        profile: str | None = None,
    ) -> None:
        calls.append((output_format, section))

    config = Config({"s": {"k": "v"}}, {})

    def fixed_config(**_kwargs: object) -> Config:
        return config

    prod = build_production()
    services = AppServices(
        get_config=fixed_config,
        get_default_config_path=prod.get_default_config_path,
        deploy_configuration=prod.deploy_configuration,
        display_config=recording_display,
        init_logging=init_logging_in_memory,
    )
    result = CliRunner().invoke(cli_mod.cli, ["config", "--format", "json", "--section", "s"], obj=lambda: services)
    assert result.exit_code == 0, result.output
    assert calls == [(OutputFormat.JSON, "s")]


@pytest.fixture
def failing_flush(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """A running lib_log_rich whose flush raises (the third-party edge, patched at its module)."""
    error: BaseException = request.param

    def flush(*_args: Any, **_kwargs: Any) -> None:
        raise error

    init_logging_in_memory(Config({}, {}))
    monkeypatch.setattr(lib_log_rich.runtime, "flush", flush)


_FLUSH_ERRORS = [TimeoutError("flush timed out"), RuntimeError("event loop is running")]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("failing_flush", _FLUSH_ERRORS, indirect=True, ids=["timeout", "event-loop"])
@pytest.mark.parametrize("output_format", [OutputFormat.JSON, OutputFormat.HUMAN])
@pytest.mark.usefixtures("failing_flush")
def test_display_config_prints_even_when_the_log_drain_fails(
    output_format: OutputFormat, capsys: pytest.CaptureFixture[str]
) -> None:
    """A drain that times out or cannot run is skipped; the configuration still prints."""
    display_config(Config({"s": {"k": "v"}}, {}), output_format=output_format)
    assert "k" in capsys.readouterr().out


@pytest.mark.os_agnostic
def test_only_the_shared_drain_helper_calls_lib_log_rich_flush() -> None:
    """One guarded drain for every caller: a second, unguarded ``runtime.flush(`` call is refused."""
    source = Path(__file__).resolve().parents[1] / "src" / "pwshpy"
    callers = sorted(
        path.relative_to(source).as_posix()
        for path in source.rglob("*.py")
        if "runtime.flush(" in path.read_text(encoding="utf-8")
    )
    assert callers == ["adapters/logging/setup.py"]
