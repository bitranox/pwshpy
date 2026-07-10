"""CLI tests for the mutating commands - every verb invoked against a fake ps facade.

``os_agnostic`` and hermetic: the facade is faked, so no host state changes. Covers
each command's argument/option parsing and its delegation to the right ps method.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli
from pwshpy.composition import build_testing
from pwshpy.domain.enums import AceType, RegistryValueType, ServiceKind, ServiceStartType, ServiceState, TaskState
from pwshpy.domain.records import LocalGroup, LocalUser, RegistryKey, ScheduledTaskInfo, ServiceInfo


def _svc(name: str) -> ServiceInfo:
    return ServiceInfo(
        name=name,
        display_name=name,
        status=ServiceState.RUNNING,
        service_type=ServiceKind.WIN32_OWN_PROCESS,
        start_type=ServiceStartType.AUTOMATIC,
    )


class _FakePs:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

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
        self, task_path: str, *, program: str, arguments: str = "", description: str = ""
    ) -> ScheduledTaskInfo:
        self._rec("register_scheduled_task", task_path, program)
        return ScheduledTaskInfo(task_name="t", task_path=task_path, state=TaskState.READY, enabled=True)

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
