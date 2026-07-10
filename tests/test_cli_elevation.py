"""CLI tests for the elevation surface - ``is-elevated`` and the ``--elevate`` flag.

``os_agnostic`` and hermetic: the facade is faked, so ``is_elevated``/``elevate`` never
touch the real token or trigger UAC.  Covers the exit-code contract of ``is-elevated``
and the relaunch-vs-noop branch of the global ``--elevate`` flag.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli
from pwshpy.composition import build_testing


class _FakePs:
    def __init__(self, *, elevated: bool, elevate_code: int | None = 0) -> None:
        self._elevated = elevated
        self._elevate_code = elevate_code
        self.elevate_calls: list[tuple[Any, ...]] = []

    def is_elevated(self) -> bool:
        return self._elevated

    def elevate(
        self,
        argv: Sequence[str] | None = None,
        *,
        executable: str | None = None,
        cwd: str | None = None,
        wait: bool = True,
    ) -> int | None:
        self.elevate_calls.append((argv, executable, cwd, wait))
        return self._elevate_code


def _factory(fake: _FakePs) -> Any:
    services = dataclasses.replace(build_testing(), ps=fake)  # type: ignore[arg-type]
    return lambda: services


@pytest.mark.os_agnostic
def test_is_elevated_exits_1_and_reports_when_not_elevated() -> None:
    """is-elevated prints 'not elevated' and exits 1 when unprivileged."""
    fake = _FakePs(elevated=False)
    result = CliRunner().invoke(cli, ["is-elevated"], obj=_factory(fake))
    assert result.exit_code == 1
    assert "not elevated" in result.output


@pytest.mark.os_agnostic
def test_is_elevated_exits_0_and_reports_when_elevated() -> None:
    """is-elevated prints 'elevated' and exits 0 when privileged."""
    fake = _FakePs(elevated=True)
    result = CliRunner().invoke(cli, ["is-elevated"], obj=_factory(fake))
    assert result.exit_code == 0
    assert "elevated" in result.output


@pytest.mark.os_agnostic
def test_is_elevated_quiet_suppresses_output() -> None:
    """--quiet signals only via the exit code."""
    fake = _FakePs(elevated=True)
    result = CliRunner().invoke(cli, ["is-elevated", "--quiet"], obj=_factory(fake))
    assert result.exit_code == 0
    assert result.output.strip() == ""


@pytest.mark.os_agnostic
def test_elevate_flag_relaunches_when_not_elevated() -> None:
    """--elevate relaunches (once) and exits with the child's code; the subcommand does not run here."""
    fake = _FakePs(elevated=False, elevate_code=5)
    result = CliRunner().invoke(cli, ["--elevate", "is-elevated"], obj=_factory(fake))
    assert result.exit_code == 5
    assert len(fake.elevate_calls) == 1


@pytest.mark.os_agnostic
def test_elevate_flag_is_noop_when_already_elevated() -> None:
    """--elevate does not relaunch when already elevated; the subcommand runs normally."""
    fake = _FakePs(elevated=True)
    result = CliRunner().invoke(cli, ["--elevate", "is-elevated"], obj=_factory(fake))
    assert result.exit_code == 0
    assert fake.elevate_calls == []
    assert "elevated" in result.output
