"""CLI tests for ``get_windows_update``: output, option forwarding, and the timeout exit code.

``os_agnostic`` and hermetic: the facade is a ``Ps`` subclass whose ``get_windows_update``
returns canned records or raises, so no Windows Update agent is touched.  It goes in through
the ``AppServices`` seam the CLI reads, through ``main()``, so the exit code tested is the
process exit code.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from pwshpy.adapters.cli.exit_codes import ExitCode
from pwshpy.adapters.cli.main import main
from pwshpy.composition import Ps, build_testing
from pwshpy.domain.errors import OperationTimeoutError
from pwshpy.domain.pipeline import Pipeline
from pwshpy.domain.records import WindowsUpdate


class _WindowsUpdatePs(Ps):
    """A Ps whose only live verb is get_windows_update; it records what the CLI passed."""

    def __init__(self, *, records: list[WindowsUpdate] | None = None, error: Exception | None = None) -> None:
        # Deliberately no super().__init__(): the CLI under test calls only get_windows_update.
        self._records = records or []
        self._error = error
        self.calls: list[tuple[str, float]] = []

    def get_windows_update(
        self, *, criteria: str = "IsInstalled=0 and Type='Software'", timeout: float = 300.0
    ) -> Pipeline[WindowsUpdate]:
        self.calls.append((criteria, timeout))
        if self._error is not None:
            raise self._error
        return Pipeline(iter(self._records))


def _run(fake: Ps, args: list[str]) -> int:
    services = dataclasses.replace(build_testing(), ps=fake)
    return main(["get_windows_update", *args], services_factory=lambda: services)


@pytest.mark.os_agnostic
def test_timeout_exits_110_with_a_timed_out_message(capsys: pytest.CaptureFixture[str]) -> None:
    """An OperationTimeoutError leaves through main() as exit 110 (ETIMEDOUT), its message on stderr."""
    error = OperationTimeoutError("Windows Update search timed out after 3s", timeout=3)
    rc = _run(_WindowsUpdatePs(error=error), [])
    captured = capsys.readouterr()
    assert rc == ExitCode.TIMEOUT == 110
    assert "timed out" in captured.err
    assert "Traceback" not in captured.err + captured.out


@pytest.mark.os_agnostic
def test_jsonl_prints_the_record(capsys: pytest.CaptureFixture[str]) -> None:
    """--jsonl emits each record the facade yields as one JSON line."""
    record = WindowsUpdate(update_id="u-1", title="Cumulative Update", kb_article_ids=("5031455",))
    rc = _run(_WindowsUpdatePs(records=[record]), ["--jsonl"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["update_id"] == "u-1"
    assert row["kb_article_ids"] == ["5031455"]


@pytest.mark.os_agnostic
def test_defaults_and_options_reach_the_facade(capsys: pytest.CaptureFixture[str]) -> None:
    """Without options the facade gets the documented defaults; --criteria/--timeout override them."""
    fake = _WindowsUpdatePs()
    assert _run(fake, ["--json"]) == 0
    assert _run(fake, ["--criteria", "IsInstalled=1", "--timeout", "12.5", "--json"]) == 0
    capsys.readouterr()
    assert fake.calls == [("IsInstalled=0 and Type='Software'", 300.0), ("IsInstalled=1", 12.5)]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("bad", ["0", "-1", "2147484"])
def test_out_of_range_timeout_is_a_usage_error(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    """--timeout outside (0, 2147483] is refused by click (exit 2) before the facade is called."""
    fake = _WindowsUpdatePs()
    rc = _run(fake, ["--timeout", bad])
    capsys.readouterr()
    assert rc == 2
    assert fake.calls == []
