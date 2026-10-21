"""CLI tests for the mutating commands - every verb invoked against a fake ps facade.

``os_agnostic`` and hermetic: the facade is faked, so no host state changes. Covers
each command's argument/option parsing and its delegation to the right ps method.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.main import main
from pwshpy.adapters.cli.root import cli
from pwshpy.composition import build_testing
from pwshpy.domain.enums import (
    AceType,
    RegistryValueType,
    ServiceKind,
    ServiceStartType,
    ServiceState,
    TaskLogonType,
    TaskRunLevel,
    TaskState,
)
from pwshpy.domain.errors import PlatformUnsupportedError
from pwshpy.domain.records import LocalGroup, LocalUser, RegistryKey, ScheduledTaskInfo, ServiceInfo
from pwshpy.domain.scheduling import TaskPrincipal, TaskTrigger


def _svc(name: str) -> ServiceInfo:
    return ServiceInfo(
        name=name,
        display_name=name,
        status=ServiceState.RUNNING,
        service_type=ServiceKind.WIN32_OWN_PROCESS,
        start_type=ServiceStartType.AUTOMATIC,
    )


class _FakePs:
    def __init__(self, register_error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._register_error = register_error

    def _rec(self, *args: Any) -> None:
        self.calls.append(args)

    def start_service(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        self._rec("start_service", name)
        return _svc(name)

    def stop_service(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        self._rec("stop_service", name)
        return _svc(name)

    def restart_service(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        self._rec("restart_service", name)
        return _svc(name)

    def set_service(self, name: str, start_type: ServiceStartType) -> ServiceInfo:
        self._rec("set_service", name, start_type)
        return _svc(name)

    def set_item_property(self, key: str, name: str, data: Any, value_type: RegistryValueType) -> Any:
        self._rec("set_item_property", key, name, data, value_type)

    def remove_item_property(self, key: str, name: str) -> None:
        self._rec("remove_item_property", key, name)

    def new_registry_key(self, key: str) -> RegistryKey:
        self._rec("new_registry_key", key)
        return RegistryKey(hive="HKCU", key=key, name="k")  # type: ignore[arg-type]

    def remove_registry_key(self, key: str, *, recursive: bool = False) -> None:
        self._rec("remove_registry_key", key, recursive)

    def clear_event_log(self, log_name: str, *, backup_path: str | None = None) -> None:
        self._rec("clear_event_log", log_name, backup_path)

    def new_local_user(
        self, name: str, *, password: str = "", full_name: str = "", description: str = "", disabled: bool = False
    ) -> LocalUser:
        self._rec("new_local_user", name)
        return LocalUser(name=name, sid="S-1-5-21-1", enabled=not disabled)

    def remove_local_user(self, name: str) -> None:
        self._rec("remove_local_user", name)

    def enable_local_user(self, name: str) -> LocalUser:
        self._rec("enable_local_user", name)
        return LocalUser(name=name, sid="S-1-5-21-1", enabled=True)

    def disable_local_user(self, name: str) -> LocalUser:
        self._rec("disable_local_user", name)
        return LocalUser(name=name, sid="S-1-5-21-1", enabled=False)

    def new_local_group(self, name: str, *, description: str = "") -> LocalGroup:
        self._rec("new_local_group", name)
        return LocalGroup(name=name, sid="S-1-5-32-1")

    def remove_local_group(self, name: str) -> None:
        self._rec("remove_local_group", name)

    def add_local_group_member(self, group: str, member: str) -> None:
        self._rec("add_local_group_member", group, member)

    def remove_local_group_member(self, group: str, member: str) -> None:
        self._rec("remove_local_group_member", group, member)

    def add_acl_ace(self, path: str, trustee: str, rights: int, *, access_type: AceType = AceType.ALLOW) -> None:
        self._rec("add_acl_ace", path, trustee, rights, access_type)

    def remove_acl_ace(self, path: str, trustee: str, *, access_type: AceType | None = None) -> None:
        self._rec("remove_acl_ace", path, trustee)

    def set_owner(self, path: str, owner: str) -> None:
        self._rec("set_owner", path, owner)

    def register_scheduled_task(
        self,
        task_path: str,
        *,
        program: str,
        arguments: str = "",
        description: str = "",
        principal: TaskPrincipal | None = None,
        triggers: Sequence[TaskTrigger] = (),
    ) -> ScheduledTaskInfo:
        if self._register_error is not None:
            raise self._register_error
        self._rec("register_scheduled_task", task_path, principal, tuple(triggers))
        user_id = principal.user_id if principal is not None and principal.user_id is not None else ""
        return ScheduledTaskInfo(
            task_name="t", task_path=task_path, state=TaskState.READY, enabled=True, user_id=user_id
        )

    def unregister_scheduled_task(self, task_path: str) -> None:
        self._rec("unregister_scheduled_task", task_path)

    def enable_scheduled_task(self, task_path: str) -> ScheduledTaskInfo:
        self._rec("enable_scheduled_task", task_path)
        return ScheduledTaskInfo(task_name="t", task_path=task_path, state=TaskState.READY, enabled=True)

    def disable_scheduled_task(self, task_path: str) -> ScheduledTaskInfo:
        self._rec("disable_scheduled_task", task_path)
        return ScheduledTaskInfo(task_name="t", task_path=task_path, state=TaskState.DISABLED, enabled=False)

    def start_scheduled_task(self, task_path: str) -> None:
        self._rec("start_scheduled_task", task_path)

    def stop_scheduled_task(self, task_path: str) -> None:
        self._rec("stop_scheduled_task", task_path)


def _factory(fake: _FakePs) -> Any:
    services = dataclasses.replace(build_testing(), ps=fake)  # type: ignore[arg-type]
    return lambda: services


_CASES: list[tuple[list[str], str]] = [
    (["start_service", "S"], "start_service"),
    (["stop_service", "S"], "stop_service"),
    (["restart_service", "S"], "restart_service"),
    (["set_service", "S", "Automatic"], "set_service"),
    (["set_item_property", "HKCU/S", "N", "42", "--type", "REG_DWORD"], "set_item_property"),
    (["remove_item_property", "HKCU/S", "N"], "remove_item_property"),
    (["new_registry_key", "HKCU/S/K"], "new_registry_key"),
    (["remove_registry_key", "HKCU/S/K", "-r"], "remove_registry_key"),
    (["clear_event_log", "Application"], "clear_event_log"),
    (["new_local_user", "u", "--password", "P@ss1", "--disabled"], "new_local_user"),
    (["remove_local_user", "u"], "remove_local_user"),
    (["enable_local_user", "u"], "enable_local_user"),
    (["disable_local_user", "u"], "disable_local_user"),
    (["new_local_group", "g"], "new_local_group"),
    (["remove_local_group", "g"], "remove_local_group"),
    (["add_local_group_member", "g", "u"], "add_local_group_member"),
    (["remove_local_group_member", "g", "u"], "remove_local_group_member"),
    (["add_acl_ace", "C:/x", "S-1-1-0", "1", "--deny"], "add_acl_ace"),
    (["remove_acl_ace", "C:/x", "S-1-1-0"], "remove_acl_ace"),
    (["set_owner", "C:/x", "S-1-5-32-544"], "set_owner"),
    (["register_scheduled_task", "\\T", "--program", "cmd.exe"], "register_scheduled_task"),
    (["unregister_scheduled_task", "\\T"], "unregister_scheduled_task"),
    (["enable_scheduled_task", "\\T"], "enable_scheduled_task"),
    (["disable_scheduled_task", "\\T"], "disable_scheduled_task"),
    (["start_scheduled_task", "\\T"], "start_scheduled_task"),
    (["stop_scheduled_task", "\\T"], "stop_scheduled_task"),
]


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("args", "verb"), _CASES)
def test_mutating_command_delegates(args: list[str], verb: str) -> None:
    """Each mutating CLI command parses its args and delegates to the matching ps method."""
    fake = _FakePs()
    result = CliRunner().invoke(cli, args, obj=_factory(fake))
    assert result.exit_code == 0, result.output
    assert fake.calls and fake.calls[0][0] == verb


@pytest.mark.os_agnostic
def test_set_item_property_coerces_dword() -> None:
    """--type REG_DWORD coerces the string data to an int."""
    fake = _FakePs()
    result = CliRunner().invoke(
        cli, ["set_item_property", "HKCU/S", "N", "42", "--type", "REG_DWORD"], obj=_factory(fake)
    )
    assert result.exit_code == 0
    assert fake.calls[0] == ("set_item_property", "HKCU/S", "N", 42, RegistryValueType.REG_DWORD)


@pytest.mark.os_agnostic
def test_add_acl_ace_deny_flag() -> None:
    """--deny selects a DENY ACE and rights parse as int."""
    fake = _FakePs()
    result = CliRunner().invoke(cli, ["add_acl_ace", "C:/x", "S-1-1-0", "5", "--deny"], obj=_factory(fake))
    assert result.exit_code == 0
    assert fake.calls[0] == ("add_acl_ace", "C:/x", "S-1-1-0", 5, AceType.DENY)


@pytest.mark.os_agnostic
def test_register_scheduled_task_cli_builds_principal_and_triggers() -> None:
    """Every principal and trigger option reaches the facade as typed models, in option order."""
    fake = _FakePs()
    args = [
        "register_scheduled_task",
        "\\T",
        "--program",
        "cmd.exe",
        "--run-as",
        "local_system",
        "--run-level",
        "highest",
        "--at",
        "2026-10-21T23:59:00+02:00",
        "--daily",
        "2026-10-21T03:00:00Z",
        "--every-days",
        "2",
        "--at-boot",
        "--boot-delay",
        "90",
        "--at-logon",
        "--logon-user",
        "S-1-5-21-1-2-3-1001",
    ]
    result = CliRunner().invoke(cli, args, obj=_factory(fake))
    assert result.exit_code == 0, result.output
    _name, _path, principal, triggers = fake.calls[-1]
    assert principal == TaskPrincipal(user_id="S-1-5-18", run_level=TaskRunLevel.HIGHEST)
    assert [t.kind for t in triggers] == ["once", "daily", "boot", "logon"]
    assert triggers[0].at == datetime(2026, 10, 21, 21, 59, tzinfo=timezone.utc)
    # a trailing Z is UTC on every supported Python, 3.10's fromisoformat included
    assert triggers[1].at == datetime(2026, 10, 21, 3, tzinfo=timezone.utc)
    assert triggers[1].every_days == 2
    assert triggers[2].delay == timedelta(seconds=90)
    assert triggers[3].user_id == "S-1-5-21-1-2-3-1001"
    assert "registered \\T as S-1-5-18" in result.output


@pytest.mark.os_agnostic
def test_register_scheduled_task_cli_without_schedule_options_passes_no_principal() -> None:
    """With none of the new options the call is the old run-on-demand one: no principal, no triggers."""
    fake = _FakePs()
    result = CliRunner().invoke(cli, ["register_scheduled_task", "\\T", "--program", "x"], obj=_factory(fake))
    assert result.exit_code == 0, result.output
    assert fake.calls[-1] == ("register_scheduled_task", "\\T", None, ())
    assert "registered \\T as an unknown principal" in result.output


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("run_as", "expected"),
    [
        ("0", "registered \\T as uid 0"),
        ("1000", "registered \\T as uid 1000"),
        ("S-1-5-21-1-2-3-1001", "registered \\T as S-1-5-21-1-2-3-1001"),
    ],
)
def test_register_scheduled_task_cli_names_the_read_back_principal(run_as: str, expected: str) -> None:
    """A decimal uid reads 'uid <n>' (root's "0" too, never a bare 0), a SID reads as itself."""
    fake = _FakePs()
    args = ["register_scheduled_task", "\\T", "--program", "x", "--run-as", run_as]
    result = CliRunner().invoke(cli, args, obj=_factory(fake))
    assert result.exit_code == 0, result.output
    assert expected in result.output.splitlines()


@pytest.mark.os_agnostic
def test_register_scheduled_task_cli_maps_choices_case_insensitively() -> None:
    """--run-level and --logon-type accept any case and arrive as the enum members."""
    fake = _FakePs()
    args = [
        "register_scheduled_task",
        "\\T",
        "--program",
        "x",
        "--run-as",
        "1000",
        "--run-level",
        "HIGHEST",
        "--logon-type",
        "s4u",
    ]
    result = CliRunner().invoke(cli, args, obj=_factory(fake))
    assert result.exit_code == 0, result.output
    principal = fake.calls[-1][2]
    assert principal == TaskPrincipal(user_id="1000", run_level=TaskRunLevel.HIGHEST, logon_type=TaskLogonType.S4U)


@pytest.mark.os_agnostic
def test_register_scheduled_task_cli_refuses_an_account_name() -> None:
    """A localized account name is refused at the CLI boundary, never resolved."""
    fake = _FakePs()
    result = CliRunner().invoke(
        cli, ["register_scheduled_task", "\\T", "--program", "x", "--run-as", "Administrator"], obj=_factory(fake)
    )
    assert result.exit_code == 2
    assert "SID" in result.output
    assert "errors.pydantic.dev" not in result.output  # rendered field: reason, not the library's dump
    assert fake.calls == []


@pytest.mark.os_agnostic
@pytest.mark.parametrize("option", ["--at", "--daily"])
def test_register_scheduled_task_cli_refuses_a_naive_time(option: str) -> None:
    """A time without a UTC offset is refused, never silently read as local time or UTC."""
    fake = _FakePs()
    result = CliRunner().invoke(
        cli, ["register_scheduled_task", "\\T", "--program", "x", option, "2026-10-21T03:00:00"], obj=_factory(fake)
    )
    assert result.exit_code == 2
    assert "UTC offset" in result.output
    assert fake.calls == []


@pytest.mark.os_agnostic
def test_register_scheduled_task_cli_refuses_an_unparseable_time() -> None:
    """A value that is not ISO 8601 is a usage error, not a traceback."""
    fake = _FakePs()
    result = CliRunner().invoke(
        cli, ["register_scheduled_task", "\\T", "--program", "x", "--at", "tomorrow"], obj=_factory(fake)
    )
    assert result.exit_code == 2
    assert "tomorrow" in result.output
    assert fake.calls == []


@pytest.mark.os_agnostic
def test_register_scheduled_task_cli_renders_a_model_refusal_per_field() -> None:
    """A value the trigger model refuses is reported as '<field>: <reason>'."""
    fake = _FakePs()
    result = CliRunner().invoke(
        cli,
        ["register_scheduled_task", "\\T", "--program", "x", "--at-boot", "--boot-delay", "-1"],
        obj=_factory(fake),
    )
    assert result.exit_code == 2
    assert "delay: Value error, delay must not be negative" in result.output
    assert fake.calls == []


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("modifier", "needs"),
    [
        (["--every-days", "2"], "--daily"),
        (["--boot-delay", "60"], "--at-boot"),
        (["--logon-user", "S-1-5-21-1-2-3-1001"], "--at-logon"),
    ],
)
def test_register_scheduled_task_cli_refuses_a_modifier_without_its_trigger(modifier: list[str], needs: str) -> None:
    """A trigger modifier given without its trigger is refused rather than silently ignored."""
    fake = _FakePs()
    result = CliRunner().invoke(
        cli, ["register_scheduled_task", "\\T", "--program", "x", *modifier], obj=_factory(fake)
    )
    assert result.exit_code == 2
    assert needs in result.output
    assert fake.calls == []


@pytest.mark.os_agnostic
def test_register_scheduled_task_cli_reports_an_adapter_refusal_on_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    """A PwshPyError from the adapter leaves through main() as one stderr line and exit 1, no traceback."""
    services = dataclasses.replace(
        build_testing(),
        ps=_FakePs(register_error=PlatformUnsupportedError("logon triggers are Windows-only")),  # type: ignore[arg-type]
    )
    rc = main(["register_scheduled_task", "\\T", "--program", "x", "--at-logon"], services_factory=lambda: services)
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.strip() == "PlatformUnsupportedError: logon triggers are Windows-only"
    assert "Traceback" not in captured.err + captured.out
