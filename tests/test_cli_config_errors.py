"""A configuration that cannot be loaded: who reports it, and who still runs.

The root group loads the configuration before any subcommand's options are parsed, so it
cannot report a failure in the form the subcommand would, and it must not block a command
that never reads the configuration: ``config-deploy`` is how a broken file gets replaced,
and ``info``, ``--help`` and every native command (``get_process``, ``test_path`` ...) have to
work meanwhile. The root records the failure; a command that reads the configuration refuses
with exit 78 and one stderr line naming it. Every way loading can fail (a broken or invalid
file, an unreadable file) takes that path; ``--traceback`` adds the loader's traceback. What
the command line itself gets wrong - a malformed or conflicting ``--set``, an invalid
``--profile`` name - is a usage error (exit 2) for every command, checked before loading, so a
load failure cannot hide it. Any other exception from the loader is a bug and is not dressed up
as a configuration error.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from lib_layered_config import Config, ConfigError

from pwshpy import __init__conf__
from pwshpy.adapters import cli as cli_mod
from pwshpy.adapters.cli import commands as commands_pkg
from pwshpy.composition import AppServices, build_production, build_testing

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import CliRunner

BROKEN_TOML = "Invalid TOML in /etc/xdg/app/config.toml: expected a right bracket"
BROKEN_PROFILE_TOML = "Invalid TOML in /etc/xdg/app/profile/prod/config.toml"
UNREADABLE = PermissionError(13, "Permission denied", "/etc/xdg/app/config.toml")

#: The module that holds every command which reads the loaded configuration.
_CONFIG_COMMANDS_MODULE = "pwshpy.adapters.cli.commands.config"

#: Each command of the config module (and the other non-native commands) with the arguments
#: that get it past click's own option parsing. A command added to the config module without
#: an entry fails ``test_every_config_module_command_is_classified``, so nobody can add one
#: that silently reads an empty configuration after a load failure.
NEEDS_CONFIG: dict[str, list[str]] = {
    "config": ["config"],
}
RUNS_WITHOUT_CONFIG: dict[str, list[str]] = {
    "config-deploy": ["config-deploy", "--target", "user"],
    "info": ["info"],
    "get_uptime": ["get_uptime", "--json"],
    "env": ["env", "--json"],
}
#: Covered by their own tests below: one writes into a directory, one fails on purpose, one
#: replaces the logging runtime.
OTHER = ("config-generate-examples", "fail", "logdemo")


def _failing_config(error: Exception) -> Callable[[], AppServices]:
    def get_config(**_kwargs: Any) -> Config:
        raise error

    return lambda: dataclasses.replace(build_testing(), get_config=get_config)


def _module_of(command_name: str) -> str:
    callback = cli_mod.cli.commands[command_name].callback
    assert callback is not None
    return callback.__module__


@pytest.mark.os_agnostic
def test_every_config_module_command_is_classified() -> None:
    in_config_module = {name for name in cli_mod.cli.commands if _module_of(name) == _CONFIG_COMMANDS_MODULE}

    assert in_config_module  # control: the lookup finds the config commands at all
    assert in_config_module <= set(NEEDS_CONFIG) | set(RUNS_WITHOUT_CONFIG) | set(OTHER)


def _reads_an_attribute_named_config(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "config"]


@pytest.mark.os_agnostic
def test_no_command_outside_the_config_module_reads_the_loaded_configuration() -> None:
    """The native commands never read ``cli_ctx.config``, so a load failure cannot reach them.

    A command that started reading it would have to ask through ``require_config`` (and be
    classified above), or it would silently run on the empty configuration a failed load leaves.
    """
    package_dir = Path(commands_pkg.__file__).parent
    config_module = package_dir / "config.py"
    assert _reads_an_attribute_named_config(config_module)  # control: the scan sees a real read

    readers = {
        path.name: lines
        for path in sorted(package_dir.glob("*.py"))
        if path != config_module and (lines := _reads_an_attribute_named_config(path))
    }

    assert readers == {}


@pytest.mark.os_agnostic
@pytest.mark.parametrize("args", NEEDS_CONFIG.values(), ids=NEEDS_CONFIG.keys())
@pytest.mark.parametrize(
    ("error", "message"),
    [(ConfigError(BROKEN_TOML), BROKEN_TOML), (UNREADABLE, str(UNREADABLE))],
    ids=["broken-file", "unreadable-file"],
)
def test_a_command_that_reads_the_config_refuses_with_exit_78(
    cli_runner: CliRunner, args: list[str], error: Exception, message: str
) -> None:
    result = cli_runner.invoke(cli_mod.cli, args, obj=_failing_config(error))

    assert result.exit_code == 78, result.output
    assert f"Error: {message}" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.os_agnostic
@pytest.mark.parametrize("args", RUNS_WITHOUT_CONFIG.values(), ids=RUNS_WITHOUT_CONFIG.keys())
def test_a_command_that_does_not_read_the_config_still_runs(cli_runner: CliRunner, args: list[str]) -> None:
    result = cli_runner.invoke(cli_mod.cli, args, obj=_failing_config(ConfigError(BROKEN_TOML)))

    assert result.exit_code == 0, result.output


@pytest.mark.os_agnostic
def test_a_native_exit_code_survives_a_broken_config(cli_runner: CliRunner, tmp_path: Path) -> None:
    """``test_path`` signals its answer through the exit code; a broken file must not replace it."""
    args = ["test_path", str(tmp_path / "missing")]
    result = cli_runner.invoke(cli_mod.cli, args, obj=_failing_config(ConfigError(BROKEN_TOML)))

    assert result.exit_code == 1, result.output
    assert result.stdout.strip() == "False"  # it ran: not a load failure's exit 1
    assert BROKEN_TOML not in result.stderr


@pytest.mark.os_agnostic
def test_config_deploy_says_it_deploys_without_the_broken_config(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(
        cli_mod.cli, ["config-deploy", "--target", "user"], obj=_failing_config(ConfigError(BROKEN_TOML))
    )

    assert result.exit_code == 0, result.output
    assert BROKEN_TOML in result.stderr


@pytest.mark.os_agnostic
def test_config_generate_examples_still_runs(cli_runner: CliRunner, tmp_path: Path) -> None:
    args = ["config-generate-examples", "--destination", str(tmp_path)]
    result = cli_runner.invoke(cli_mod.cli, args, obj=_failing_config(ConfigError(BROKEN_TOML)))

    assert result.exit_code == 0, result.output


@pytest.mark.os_agnostic
def test_the_fail_command_is_not_refused_for_the_config(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(cli_mod.cli, ["fail"], obj=_failing_config(ConfigError(BROKEN_TOML)))

    assert isinstance(result.exception, RuntimeError)
    assert BROKEN_TOML not in result.stderr


@pytest.mark.os_agnostic
@pytest.mark.parametrize("args", [["config-deploy", "--help"], []], ids=["subcommand-help", "bare-group"])
def test_help_works_while_the_config_is_broken(cli_runner: CliRunner, args: list[str]) -> None:
    result = cli_runner.invoke(cli_mod.cli, args, obj=_failing_config(ConfigError(BROKEN_TOML)))

    assert result.exit_code == 0, result.output
    assert "Usage" in result.stdout


@pytest.mark.os_agnostic
def test_a_malformed_set_is_still_a_usage_error(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(cli_mod.cli, ["--set", "no-equals-sign", "info"], obj=build_testing)

    assert result.exit_code == 2, result.output


@pytest.mark.os_agnostic
@pytest.mark.parametrize("command", [["info"], ["get_uptime"], ["config"]], ids=["info", "get_uptime", "config"])
def test_a_malformed_set_is_a_usage_error_even_when_the_config_does_not_load(
    cli_runner: CliRunner, command: list[str]
) -> None:
    """Were the overrides applied only to a loaded configuration, info would ignore them."""
    args = ["--set", "no-equals-sign", *command]
    result = cli_runner.invoke(cli_mod.cli, args, obj=_failing_config(ConfigError(BROKEN_TOML)))

    assert result.exit_code == 2, result.output
    assert "no-equals-sign" in result.output


@pytest.mark.os_agnostic
@pytest.mark.parametrize("command", [["info"], ["config"]], ids=["info", "config"])
@pytest.mark.parametrize("loads", [True, False], ids=["config-loads", "config-broken"])
def test_conflicting_set_overrides_are_a_usage_error(cli_runner: CliRunner, command: list[str], loads: bool) -> None:
    """``--set a.b=1 --set a.b.c=2`` used to escape as a TypeError (exit 22 through main)."""
    factory = build_testing if loads else _failing_config(ConfigError(BROKEN_TOML))
    result = cli_runner.invoke(cli_mod.cli, ["--set", "a.b=1", "--set", "a.b.c=2", *command], obj=factory)

    assert result.exit_code == 2, result.output
    assert "conflicting --set overrides: a.b is given a value and a.b.c" in result.output


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "args",
    [
        ["--profile", "../x", "info"],
        ["--profile", "../x", "get_uptime"],
        ["config", "--profile", "../x"],
        ["config-deploy", "--target", "user", "--no-permissions", "--profile", "../x"],
    ],
    ids=["root-info", "root-get_uptime", "config-option", "config-deploy-option"],
)
def test_an_invalid_profile_name_is_a_usage_error_for_every_command(cli_runner: CliRunner, args: list[str]) -> None:
    result = cli_runner.invoke(cli_mod.cli, args, obj=build_testing)

    assert result.exit_code == 2, result.output
    assert "../x" in result.output


@pytest.mark.os_agnostic
def test_a_bug_in_the_loader_is_not_reported_as_a_configuration_error(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(cli_mod.cli, ["config"], obj=_failing_config(ValueError("a bug, not a config file")))

    assert result.exit_code != 78, result.output
    assert isinstance(result.exception, ValueError)


@pytest.fixture
def non_utf8_env_file(tmp_path: Path) -> Path:
    env_file = tmp_path / "latin1.env"
    env_file.write_bytes(b"A=\xff\xfe\n")
    return env_file


@pytest.fixture
def real_loader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clear_config_cache: None) -> Callable[[], AppServices]:
    """The testing composition over the production loader, with an empty user layer.

    A ``.env`` file that is not UTF-8 is lib_layered_config's to report: it raises its own
    ConfigError naming the file, so the CLI records it like any other broken file.
    """
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    return lambda: dataclasses.replace(build_testing(), get_config=build_production().get_config)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("args", RUNS_WITHOUT_CONFIG.values(), ids=RUNS_WITHOUT_CONFIG.keys())
def test_a_command_that_does_not_read_the_config_runs_past_a_non_utf8_env_file(
    cli_runner: CliRunner, real_loader: Callable[[], AppServices], non_utf8_env_file: Path, args: list[str]
) -> None:
    argv = ["--env-file", str(non_utf8_env_file), *args]
    result = cli_runner.invoke(cli_mod.cli, argv, obj=real_loader)

    assert result.exit_code == 0, result.output


@pytest.mark.os_agnostic
@pytest.mark.parametrize("args", NEEDS_CONFIG.values(), ids=NEEDS_CONFIG.keys())
def test_a_non_utf8_env_file_is_a_configuration_error_naming_the_file(
    cli_runner: CliRunner, real_loader: Callable[[], AppServices], non_utf8_env_file: Path, args: list[str]
) -> None:
    argv = ["--env-file", str(non_utf8_env_file), *args]
    result = cli_runner.invoke(cli_mod.cli, argv, obj=real_loader)

    error_lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert result.exit_code == 78, result.output
    assert len(error_lines) == 1, result.stderr
    assert f"{non_utf8_env_file} is not valid UTF-8" in error_lines[0]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("args", [["config"], ["config-deploy", "--target", "user"]], ids=["config", "config-deploy"])
def test_traceback_shows_why_the_config_did_not_load(
    cli_runner: CliRunner, managed_traceback_state: None, args: list[str]
) -> None:
    def get_config(**_kwargs: Any) -> Config:
        try:
            raise OSError("the underlying cause")
        except OSError as cause:
            raise ConfigError(BROKEN_TOML) from cause

    services = dataclasses.replace(build_testing(), get_config=get_config)
    result = cli_runner.invoke(cli_mod.cli, ["--traceback", *args], obj=lambda: services)

    assert "Traceback" in result.stderr
    assert "OSError: the underlying cause" in result.stderr
    assert BROKEN_TOML in result.stderr


@pytest.mark.os_agnostic
def test_a_profile_given_to_config_itself_fails_like_one_given_to_the_root(cli_runner: CliRunner) -> None:
    def get_config(*, profile: str | None = None, **_kwargs: Any) -> Config:
        if profile == "prod":
            raise ConfigError(BROKEN_PROFILE_TOML)
        return Config({}, {})

    services = dataclasses.replace(build_testing(), get_config=get_config)
    result = cli_runner.invoke(cli_mod.cli, ["config", "--profile", "prod"], obj=lambda: services)

    assert result.exit_code == 78, result.output
    assert f"Error: {BROKEN_PROFILE_TOML}" in result.stderr


@pytest.mark.os_agnostic
def test_a_profile_given_to_config_itself_keeps_the_root_env_file(cli_runner: CliRunner, tmp_path: Path) -> None:
    env_file = tmp_path / "app.env"
    env_file.write_text("", encoding="utf-8")
    calls: list[tuple[str | None, str | None]] = []

    def get_config(*, profile: str | None = None, dotenv_path: str | None = None, **_kwargs: Any) -> Config:
        calls.append((profile, dotenv_path))
        return Config({}, {})

    services = dataclasses.replace(build_testing(), get_config=get_config)
    args = ["--env-file", str(env_file), "config", "--profile", "prod"]
    result = cli_runner.invoke(cli_mod.cli, args, obj=lambda: services)

    assert result.exit_code == 0, result.output
    assert calls == [(None, str(env_file)), ("prod", str(env_file))]


@pytest.fixture
def broken_user_config_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose user layer holds a config.toml that does not parse."""
    config_dir = tmp_path / __init__conf__.LAYEREDCONF_SLUG
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("broken = [\n", encoding="utf-8")
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path)}


_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="XDG_CONFIG_HOME locates the user layer on Linux"
)


def _stderr_of(completed: subprocess.CompletedProcess[bytes]) -> str:
    stderr = completed.stderr.decode("utf-8", "replace")
    assert "UnicodeDecodeError" not in stderr
    return stderr


@_LINUX_ONLY
def test_a_real_non_utf8_env_file_does_not_stop_info(tmp_path: Path, non_utf8_env_file: Path) -> None:
    """End to end, in a process of its own, through the real loader."""
    completed = subprocess.run(
        [sys.executable, "-m", "pwshpy", "--env-file", "latin1.env", "info"],
        capture_output=True,
        check=False,
        cwd=non_utf8_env_file.parent,
        env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "xdg")},
    )

    assert completed.returncode == 0, _stderr_of(completed)


@_LINUX_ONLY
def test_a_real_non_utf8_env_file_refuses_config_with_78(tmp_path: Path, non_utf8_env_file: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "pwshpy", "--env-file", "latin1.env", "config"],
        capture_output=True,
        check=False,
        cwd=non_utf8_env_file.parent,
        env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "xdg")},
    )

    stderr = _stderr_of(completed)
    assert completed.returncode == 78, stderr
    assert "latin1.env is not valid UTF-8" in stderr


@_LINUX_ONLY
def test_a_real_broken_user_config_does_not_stop_info(broken_user_config_env: dict[str, str]) -> None:
    """End to end through the real loader: info does not read the configuration."""
    completed = subprocess.run(
        [sys.executable, "-m", "pwshpy", "info"],
        capture_output=True,
        check=False,
        env=broken_user_config_env,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert completed.stdout


@_LINUX_ONLY
def test_a_real_broken_user_config_does_not_stop_a_native_command(broken_user_config_env: dict[str, str]) -> None:
    """End to end through the real loader: a native command never reads the configuration."""
    completed = subprocess.run(
        [sys.executable, "-m", "pwshpy", "get_uptime", "--json"],
        capture_output=True,
        check=False,
        env=broken_user_config_env,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert completed.stdout


@_LINUX_ONLY
def test_a_real_broken_user_config_refuses_config_with_78(broken_user_config_env: dict[str, str]) -> None:
    """End to end through the real loader: config reads it, so it refuses and names the file."""
    completed = subprocess.run(
        [sys.executable, "-m", "pwshpy", "config"],
        capture_output=True,
        check=False,
        env=broken_user_config_env,
    )

    stderr = completed.stderr.decode("utf-8", "replace")
    assert completed.returncode == 78, stderr
    assert "config.toml is not valid TOML" in stderr
    assert "Traceback" not in stderr


@_LINUX_ONLY
def test_a_real_invalid_profile_name_stops_info_with_a_usage_error(tmp_path: Path) -> None:
    """End to end through the real loader: an invalid --profile is not silently ignored."""
    completed = subprocess.run(
        [sys.executable, "-m", "pwshpy", "--profile", "../x", "info"],
        capture_output=True,
        check=False,
        env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path)},
    )

    assert completed.returncode == 2, completed.stderr.decode("utf-8", "replace")
