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
import errno
import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

import click
import lib_cli_exit_tools
import pytest
from com_fakes import com_error_for
from fake_services import services_with_ps
from lib_layered_config import Config

import pwshpy.adapters.native.dns as dns_module
from pwshpy import NativeAccessDeniedError
from pwshpy.adapters.cli.main import main
from pwshpy.adapters.native.marshal import native_error
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
def test_a_timed_out_exec_exits_110_through_main(capsys: pytest.CaptureFixture[str]) -> None:
    """An OperationTimeoutError leaves main() as ETIMEDOUT on every OS, not the generic 1."""
    argv = ["exec", "--timeout", "1", "--", sys.executable, "-c", "import time; time.sleep(60)"]

    assert main(argv, services_factory=_services({})) == 110
    assert "OperationTimeoutError" in capsys.readouterr().err


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


@pytest.mark.os_agnostic
def test_a_timed_out_wait_process_exits_110_through_main(capsys: pytest.CaptureFixture[str]) -> None:
    """[D1]: wait_process --timeout raises OperationTimeoutError, so it exits 110 like every timeout (was 1)."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert main(["wait_process", "--timeout", "0.5", str(child.pid)], services_factory=_services({})) == 110
    finally:
        child.kill()
        child.wait()
    assert "OperationTimeoutError" in capsys.readouterr().err


@pytest.mark.os_agnostic
def test_a_timed_out_resolve_dns_name_exits_110_through_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[D1]: resolve_dns_name --timeout raises OperationTimeoutError, so it exits 110 (was 1)."""

    def _slow_getaddrinfo(*_args: object, **_kwargs: object) -> list[object]:
        time.sleep(2.0)
        return []

    # The OS resolver is the external edge: no test can make a real lookup reliably slow.
    monkeypatch.setattr(dns_module.socket, "getaddrinfo", _slow_getaddrinfo)
    argv = ["resolve_dns_name", "--timeout", "0.05", "slow.example."]
    assert main(argv, services_factory=_services({})) == 110
    assert "OperationTimeoutError" in capsys.readouterr().err


#: The exit code lib_cli_exit_tools gives a ValueError: EINVAL on POSIX, ERROR_INVALID_PARAMETER on Windows
#: (its table is keyed on os.name).
_VALUE_ERROR_EXIT = 87 if os.name == "nt" else 22


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("value", "code"), [("-1", 2), ("0", 2), ("1e12", 2), ("nan", _VALUE_ERROR_EXIT)])
def test_an_exec_timeout_outside_the_accepted_range_is_refused(value: str, code: int) -> None:
    """[D3]: exec --timeout takes (0, 2147483] like get_windows_update: out of range is a usage error
    (2) before any child starts, NaN (which passes a range check) the facade's ValueError (22, or 87 on
    Windows). It
    was: -1 a timeout at once (110), NaN an internal message (22), 1e12 a raw OverflowError."""
    argv = ["exec", "--timeout", value, "--", sys.executable, "-c", "pass"]
    assert main(argv, services_factory=_services({})) == code


#: E_ACCESSDENIED, the HRESULT a refused COM or Win32 call carries.
_E_ACCESSDENIED = 0x80070005

#: Every shape a refused native call takes on its way into NativeAccessDeniedError: two POSIX
#: OSErrors whose errno differs, and a pywin32 COM error that carries no errno at all.
_DENIED_CAUSES: list[BaseException] = [
    PermissionError(errno.EACCES, "Permission denied"),
    PermissionError(errno.EPERM, "Operation not permitted"),
    com_error_for(_E_ACCESSDENIED),
]


class _RefusingAcl:
    """A facade double whose ``get_acl`` is refused the way the native adapter refuses it."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def get_acl(self, _path: str) -> Any:
        raise self._error


def _refused_get_acl_exit_code(cause: BaseException) -> int:
    """Run ``get_acl`` through main() with the facade raising what ``native_error`` makes of ``cause``."""
    error = native_error("refused", cause)
    assert isinstance(error, NativeAccessDeniedError)
    services = _services({}, ps=services_with_ps(_RefusingAcl(error)).ps)
    return main(["get_acl", "/opt/locked/file"], services_factory=services)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("cause", _DENIED_CAUSES, ids=["eacces", "eperm", "com-e-accessdenied"])
def test_a_refused_native_call_exits_with_the_permission_error_code_whatever_its_cause(
    capsys: pytest.CaptureFixture[str], cause: BaseException
) -> None:
    """One error type, one exit code: the errno a cause carried must not pick it (EPERM gave 1)."""
    expected = lib_cli_exit_tools.get_system_exit_code(PermissionError())

    assert _refused_get_acl_exit_code(cause) == expected
    assert expected != lib_cli_exit_tools.get_system_exit_code(RuntimeError())
    assert "NativeAccessDeniedError: refused" in capsys.readouterr().err


@pytest.mark.os_agnostic
@pytest.mark.parametrize("cause", _DENIED_CAUSES, ids=["eacces", "eperm", "com-e-accessdenied"])
def test_a_refused_native_call_exits_with_the_windows_permission_error_code_on_windows(
    monkeypatch: pytest.MonkeyPatch, cause: BaseException
) -> None:
    """lib_cli_exit_tools picks its table by os.name, so patching it reads the Windows arm on any OS."""
    posix_code = lib_cli_exit_tools.get_system_exit_code(PermissionError())
    with monkeypatch.context() as patch:
        patch.setattr(os, "name", "nt")
        windows_code = lib_cli_exit_tools.get_system_exit_code(PermissionError())
        code = _refused_get_acl_exit_code(cause)

    assert code == windows_code
    # The patch reached the library: on Windows a PermissionError has its own code.
    assert windows_code != posix_code or os.name == "nt"
