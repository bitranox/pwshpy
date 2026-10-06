"""Exit codes and stderr through the real ``main()`` entry point.

``CliRunner.invoke(cli, ...)`` calls the click command tree directly and never runs
``main()``'s ``standalone_mode=False`` handling. That is where a command's ``ctx.exit(N)``
was discarded (rich_click's ``main()`` returns N, and ``main()`` returned 0 regardless), and
where a bare ``SystemExit`` from a command falls into the catch-all branch and is printed to
stderr as ``SystemExit: N`` - text a user reads as a crash. These tests go through ``main()``
itself, as a console script does, with the services replaced at the composition seam.
"""

from __future__ import annotations

import dataclasses
import sys
from typing import TYPE_CHECKING, Any

import click
import pytest
from lib_layered_config import Config

from pwshpy.adapters.cli.main import main
from pwshpy.composition import AppServices, build_testing

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _services(data: dict[str, Any], **overrides: Any) -> Callable[[], AppServices]:
    config = Config(data, {})

    def get_config(**_kwargs: Any) -> Config:
        return config

    def build() -> AppServices:
        return dataclasses.replace(build_testing(), get_config=get_config, **overrides)

    return build


def _raising(error: BaseException) -> Callable[..., Any]:
    def raise_it(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    return raise_it


@pytest.mark.os_agnostic
def test_test_path_of_a_missing_path_exits_1_through_main(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """``test_path`` signals through ``ctx.exit``; main() used to drop that code and return 0."""
    exit_code = main(["test_path", str(tmp_path / "missing")], services_factory=_services({}))

    assert exit_code == 1
    assert capsys.readouterr().out.strip() == "False"


@pytest.mark.os_agnostic
def test_test_path_of_an_existing_path_exits_0_through_main(tmp_path: Path) -> None:
    """Control: the same command exits 0 when the path exists, so the 1 above is the answer."""
    exit_code = main(["test_path", "--quiet", str(tmp_path)], services_factory=_services({}))

    assert exit_code == 0


@pytest.mark.os_agnostic
def test_exec_exits_with_the_child_s_code_through_main() -> None:
    """``exec`` promises the child's exit code; main() used to turn every one into 0."""
    argv = ["exec", "--", sys.executable, "-c", "raise SystemExit(3)"]

    assert main(argv, services_factory=_services({})) == 3


@pytest.mark.os_agnostic
def test_a_config_display_error_exits_22_without_printing_systemexit(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["config"], services_factory=_services({}, display_config=_raising(ValueError("boom"))))

    err = capsys.readouterr().err
    assert exit_code == 22
    assert "boom" in err
    assert "SystemExit" not in err


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("error", "expected"),
    [(PermissionError("denied"), 13), (OSError("disk full"), 1)],
    ids=["permission-denied", "general-error"],
)
def test_a_deploy_failure_exits_with_its_code_without_printing_systemexit(
    capsys: pytest.CaptureFixture[str], error: Exception, expected: int
) -> None:
    services = _services({}, deploy_configuration=_raising(error))
    exit_code = main(["config-deploy", "--target", "user"], services_factory=services)

    err = capsys.readouterr().err
    assert exit_code == expected
    assert str(error) in err
    assert "SystemExit" not in err


@pytest.mark.os_agnostic
def test_a_deliberate_exit_inside_deploy_keeps_its_own_code(capsys: pytest.CaptureFixture[str]) -> None:
    """click's Exit is a RuntimeError; the deploy failure branch must let it through unrelabelled."""
    services = _services({}, deploy_configuration=_raising(click.exceptions.Exit(78)))
    exit_code = main(["config-deploy", "--target", "user"], services_factory=services)

    err = capsys.readouterr().err
    assert exit_code == 78
    assert "Failed to deploy configuration" not in err


@pytest.mark.os_agnostic
def test_a_generate_examples_failure_exits_1_without_printing_systemexit(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")

    exit_code = main(
        ["config-generate-examples", "--destination", str(blocker / "sub")], services_factory=_services({})
    )

    err = capsys.readouterr().err
    assert exit_code == 1
    assert "Error:" in err
    assert "SystemExit" not in err


@pytest.mark.os_agnostic
def test_a_successful_command_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["info"], services_factory=_services({}))

    assert exit_code == 0
    assert "pwshpy" in capsys.readouterr().out
