"""The ps facade: pipeline wiring and .NET delegation."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

import pytest
from pydantic import SecretStr, ValidationError

import pwshpy
from pwshpy import ps
from pwshpy.adapters.native.known_folders import posix_known_folder, windows_known_folder
from pwshpy.application.ports import AclSource, WindowsImageSource
from pwshpy.composition import Ps, build_ps
from pwshpy.domain.enums import (
    AceType,
    AddressFamily,
    EventLevel,
    FileItemType,
    ImageArchitecture,
    KnownFolder,
    RegistryHive,
    RegistryValueType,
    ServiceKind,
    ServiceStartType,
    ServiceState,
    TaskMultipleInstances,
    TaskState,
    TransportProtocol,
)
from pwshpy.domain.errors import (
    ConfigurationError,
    ElevationRequiredError,
    FeatureUnavailableError,
    NativeCallError,
    PlatformUnsupportedError,
)
from pwshpy.domain.packing import DEFAULT_PACK_OPTIONS, PackOptions
from pwshpy.domain.pipeline import Pipeline
from pwshpy.domain.records import (
    AclEntry,
    CimInstance,
    CommandInfo,
    CommandParameter,
    ComputerInfo,
    ConnectionTest,
    Credential,
    CurrentIdentity,
    DiskUsage,
    DnsRecord,
    EnvVar,
    EventLogEntry,
    FileSystemItem,
    Hotfix,
    LocalGroup,
    LocalUser,
    NetAdapter,
    NetConnection,
    NetIpAddress,
    PackedScript,
    ProcessInfo,
    ProcessResult,
    PSInvocationResult,
    RegistryKey,
    RegistryValue,
    ScheduledTaskInfo,
    ServiceInfo,
    ShortcutInfo,
    SystemUptime,
    WebResponse,
    WindowsImage,
    WindowsUpdate,
)
from pwshpy.domain.scheduling import BootTrigger, TaskPrincipal, TaskSettings, TaskTrigger
from pwshpy.domain.shortcuts import ShortcutSpec

#: An opaque bool, as in the facade: a literal sys.platform test lets pyright prune one branch
#: and report the other resolver's import unused.
_ON_WINDOWS: bool = sys.platform == "win32"


@pytest.mark.os_agnostic
def test_default_ps_is_a_facade() -> None:
    """The module-level ps is a wired Ps facade."""
    assert isinstance(ps, Ps)


@pytest.mark.os_agnostic
def test_processes_returns_a_pipeline_of_records() -> None:
    """ps.get_process() returns a Pipeline yielding ProcessInfo records."""
    pipeline = build_ps().get_process()
    assert isinstance(pipeline, Pipeline)
    assert isinstance(pipeline.first(), ProcessInfo)


@pytest.mark.os_agnostic
def test_connections_returns_a_pipeline() -> None:
    """ps.get_net_tcp_connection() returns a Pipeline (possibly empty in sandboxes)."""
    pipeline = build_ps().get_net_tcp_connection()
    assert isinstance(pipeline, Pipeline)
    for conn in pipeline.take(1):
        assert isinstance(conn, NetConnection)


@pytest.mark.os_agnostic
def test_disks_returns_a_pipeline_of_records() -> None:
    """ps.get_volume() returns a Pipeline yielding DiskUsage records."""
    pipeline = build_ps().get_volume()
    assert isinstance(pipeline, Pipeline)
    assert isinstance(pipeline.first(), DiskUsage)


@pytest.mark.os_agnostic
def test_processes_is_independently_consumable_per_call() -> None:
    """Each processes() call hands out a fresh, independently consumable pipeline."""
    facade = build_ps()
    assert facade.get_process().take(1).to_list()
    assert facade.get_process().take(1).to_list()


@pytest.mark.os_agnostic
def test_run_delegates_to_tier_b_guard() -> None:
    """ps.run without the [full] extra surfaces FeatureUnavailableError."""
    from pwshpy.adapters.powershell import is_available

    if is_available():
        pytest.skip("[full] extra is installed; guard path not exercised")
    with pytest.raises(FeatureUnavailableError):
        build_ps().run("Get-Process")


@pytest.mark.os_agnostic
def test_module_cmdlet_wrappers_hit_tier_b_guard() -> None:
    """AD/Exchange/Azure wrappers delegate to .NET, so without [full] they raise the guard."""
    from pwshpy.adapters.powershell import is_available

    if is_available():
        pytest.skip("[full] extra is installed; guard path not exercised")
    facade = build_ps()
    for call in (facade.get_ad_user, facade.get_ad_group, facade.get_mailbox, facade.get_az_vm):
        with pytest.raises(FeatureUnavailableError):
            call()


# ---------------------------------------------------------------------------
# Injected-adapter fakes (Testing-API style): every Ps port gets a fake, and the
# stateful ones record their calls so a test can assert on delegation order.
# ---------------------------------------------------------------------------


def _fake_source() -> Iterator[ProcessInfo]:
    yield from [ProcessInfo(pid=1, name="a"), ProcessInfo(pid=2, name="b")]


def _fake_connections() -> Iterator[NetConnection]:
    yield from ()


def _fake_net_adapters() -> Iterator[NetAdapter]:
    yield NetAdapter(name="eth0", is_up=True, speed_mbps=1000, mtu=1500, mac_address="00:11:22:33:44:55")


def _fake_net_ip_addresses() -> Iterator[NetIpAddress]:
    yield NetIpAddress(interface="eth0", address="10.0.0.5", family=AddressFamily.IPV4)


def _fake_net_udp_endpoints() -> Iterator[NetConnection]:
    yield NetConnection(protocol=TransportProtocol.UDP, local_address="127.0.0.1", local_port=53)


def _fake_disks() -> Iterator[DiskUsage]:
    yield from ()


def _fake_uptime() -> SystemUptime:
    return SystemUptime(boot_time=datetime(2020, 1, 1, tzinfo=timezone.utc), uptime_seconds=42.0)


def _fake_resolver(name: str, *, timeout: float | None = None) -> Iterator[DnsRecord]:
    yield from ()


def _fake_tester(host: str, *, port: int = 443, timeout: float = 5.0) -> ConnectionTest:
    return ConnectionTest(host=host, port=port, reachable=True, latency_ms=1.0)


def _fake_env() -> Iterator[EnvVar]:
    yield EnvVar(name="FOO", value="bar")


def _fake_runner(script: str, *, timeout: float | None = None) -> list[object]:
    return ["ran", script, timeout]


def _fake_cmdlet(name: str, *args: object, timeout: float | None = None, **params: object) -> PSInvocationResult:
    return PSInvocationResult(output=[name, list(args), params, timeout])


def _fake_get_command(name: str) -> CommandInfo:
    return CommandInfo(name=name, command_type="Cmdlet", parameters=[CommandParameter(name="Path")])


def _fake_exec(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    wait_tree: bool = False,
) -> ProcessResult:
    return ProcessResult(argv=list(argv), exit_code=0, stdout=f"cwd={cwd}", stderr=f"wait_tree={wait_tree}")


class _FakeWriters:
    """File writers and the downloader, sharing one call log so their order is visible."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def write_text(
        self, path: str | Path, text: str, *, encoding: str = "utf-8", newline: str = "\n", bom: bool = False
    ) -> Path:
        self.calls.append(("text", str(path), text, encoding, str(bom)))
        return Path(path)

    def write_records(self, path: str | Path, records: object, *, jsonl: bool = True) -> Path:
        self.calls.append(("records", str(path), str(jsonl)))
        return Path(path)

    def write_text_stream(
        self,
        path: str | Path,
        chunks: Iterable[str],
        *,
        encoding: str = "utf-8",
        newline: str = "\n",
        bom: bool = False,
    ) -> Path:
        self.calls.append(("stream", str(path), "".join(chunks), newline))
        return Path(path)

    def download_file(self, url: str, dest: str | Path, *, chunk_size: int = 65536, timeout: float = 30.0) -> Path:
        self.calls.append(("download", url, str(dest)))
        return Path(dest)


def _fake_prompt(username: str | None = None, *, prompt: str = "Password: ", target: str = "") -> Credential:
    return Credential(username=username or "prompted", secret=SecretStr("s3cret"), target=target)


def _fake_web_request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: str | None = None,
    timeout: float = 30.0,
    skip_http_error_check: bool = False,
) -> WebResponse:
    return WebResponse(url=url, status_code=200, text=f"{method} {body} skip={skip_http_error_check}")


def _fake_rest_method(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    json_body: object = None,
    timeout: float = 30.0,
    skip_http_error_check: bool = False,
) -> object:
    return {"url": url, "echo": json_body, "skip": skip_http_error_check}


class _FakeProcessControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def stop_process(self, pid: int, *, force: bool = False) -> None:
        self.calls.append(("stop_process", str(pid), str(force)))

    def wait_process(self, pid: int, *, timeout: float | None = None) -> int | None:
        self.calls.append(("wait_process", str(pid)))
        return 0

    def restart_computer(self, *, delay_seconds: int = 0, force: bool = True) -> None:
        self.calls.append(("restart_computer", str(delay_seconds), str(force)))

    def stop_computer(self, *, delay_seconds: int = 0, force: bool = True) -> None:
        self.calls.append(("stop_computer", str(delay_seconds), str(force)))


def _fake_hotfixes() -> Iterator[Hotfix]:
    yield Hotfix(hotfix_id="KB5001", description="Security Update")


def _fake_windows_images(path: Path) -> Iterator[WindowsImage]:
    # The name carries the path back, so a test can see the facade passed a Path through.
    yield WindowsImage(index=1, name=f"{path.name}:Pro", architecture=ImageArchitecture.X64)
    yield WindowsImage(index=2, name=f"{path.name}:Home", architecture=ImageArchitecture.ARM64)


def _fake_computer_info() -> ComputerInfo:
    return ComputerInfo(hostname="pc1", os_name="TestOS", cpu_count=4)


def _fake_current_identity() -> CurrentIdentity:
    return CurrentIdentity(sid="S-1-5-18", account="NT AUTHORITY\\SYSTEM", is_system=True, elevated=True)


class _FakeCredentialStore:
    def __init__(self) -> None:
        self.vault: dict[str, Credential] = {}

    def save(self, target: str, username: str, secret: str) -> Credential:
        cred = Credential(target=target, username=username, secret=SecretStr(secret))
        self.vault[target] = cred
        return cred

    def load(self, target: str) -> Credential | None:
        return self.vault.get(target)

    def delete(self, target: str) -> None:
        self.vault.pop(target, None)


class _FakeFileSystem:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def _item(self, path: str, *, is_dir: bool = False) -> FileSystemItem:
        return FileSystemItem(path=path, name=path.rsplit("/", 1)[-1], is_directory=is_dir, size=1)

    def get_child_item(self, path: str, *, recurse: bool = False) -> Iterator[FileSystemItem]:
        self.calls.append(("get_child_item", path, str(recurse)))
        yield self._item(f"{path}/a.txt")

    def get_item(self, path: str) -> FileSystemItem:
        self.calls.append(("get_item", path))
        return self._item(path)

    def get_content(self, path: str, *, encoding: str = "utf-8") -> str:
        self.calls.append(("get_content", path, encoding))
        return "hello"

    def get_content_lines(self, path: str, *, encoding: str = "utf-8") -> Iterator[str]:
        self.calls.append(("get_content_lines", path, encoding))
        yield from ("l1\n", "l2\n")

    def test_path(self, path: str) -> bool:
        self.calls.append(("test_path", path))
        return True

    def new_item(self, path: str, *, item_type: FileItemType = FileItemType.FILE) -> FileSystemItem:
        self.calls.append(("new_item", path, item_type.value))
        return self._item(path, is_dir=item_type is FileItemType.DIRECTORY)

    def copy_item(self, source: str, destination: str, *, recurse: bool = False) -> None:
        self.calls.append(("copy_item", source, destination, str(recurse)))

    def move_item(self, source: str, destination: str) -> None:
        self.calls.append(("move_item", source, destination))

    def remove_item(self, path: str, *, recurse: bool = False) -> None:
        self.calls.append(("remove_item", path, str(recurse)))


class _FakeElevator:
    """Reports a non-elevated process and records every elevation request."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def is_elevated(self) -> bool:
        return False

    def elevate(
        self,
        argv: Sequence[str] | None = None,
        *,
        executable: str | None = None,
        cwd: str | None = None,
        wait: bool = True,
    ) -> int | None:
        self.calls.append((argv, executable, cwd, wait))
        return 7


def _fake_registry_values(key: str) -> Iterator[RegistryValue]:
    yield RegistryValue(hive=RegistryHive.HKLM, key=key, name="Build", type=RegistryValueType.REG_SZ, data="1")


def _fake_registry_keys(key: str) -> Iterator[RegistryKey]:
    yield RegistryKey(hive=RegistryHive.HKLM, key=key, name="Sub")


def _fake_services(*, strict: bool = False) -> Iterator[ServiceInfo]:
    # The display name echoes the strict choice so a test can see what the facade forwarded.
    yield ServiceInfo(
        name="Svc",
        display_name=f"strict={strict}",
        status=ServiceState.RUNNING,
        service_type=ServiceKind.WIN32_OWN_PROCESS,
        start_type=ServiceStartType.AUTOMATIC,
    )


def _fake_event_log(log_name: str) -> Iterator[EventLogEntry]:
    yield EventLogEntry(log_name=log_name, record_id=1, event_id=5, level=EventLevel.WARNING, provider_name="P")


def _fake_cim(class_name: str, *, where: str | None = None, namespace: str = "root/cimv2") -> Iterator[CimInstance]:
    yield CimInstance(class_name=class_name, properties={"where": where, "namespace": namespace})


def _fake_scheduled_tasks(
    folder_path: str = "\\", *, strict: bool = False, task_name: str | None = None
) -> Iterator[ScheduledTaskInfo]:
    # The description echoes the strict choice, and a by-name lookup answers with the name it was
    # given, so a test can see what the facade forwarded; without a name the listing yields "T".
    yield ScheduledTaskInfo(
        task_name="T" if task_name is None else task_name,
        task_path=folder_path,
        state=TaskState.READY,
        enabled=True,
        description=f"strict={strict}",
    )


def _fake_local_users() -> Iterator[LocalUser]:
    yield LocalUser(name="u", sid="S-1-5-21-1", enabled=True)


def _fake_local_groups() -> Iterator[LocalGroup]:
    yield LocalGroup(name="g", sid="S-1-5-32-544")


def _fake_acl(path: str) -> Iterator[AclEntry]:
    yield AclEntry(path=path, owner_sid="S-1-5-18", trustee_sid="S-1-5-32-544", access_type=AceType.ALLOW, rights=1)


class _FakeServiceController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.last_start_type: ServiceStartType | None = None
        self.stop_waits: list[bool] = []

    def _svc(self, name: str) -> ServiceInfo:
        return ServiceInfo(
            name=name,
            display_name=name,
            status=ServiceState.RUNNING,
            service_type=ServiceKind.WIN32_OWN_PROCESS,
            start_type=ServiceStartType.AUTOMATIC,
        )

    def start(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        self.calls.append(("start", name))
        return self._svc(name)

    def stop(self, name: str, *, timeout: float = 30.0, wait: bool = True) -> ServiceInfo:
        self.calls.append(("stop", name))
        self.stop_waits.append(wait)
        return self._svc(name)

    def restart(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        self.calls.append(("restart", name))
        return self._svc(name)

    def set_startup(self, name: str, start_type: ServiceStartType) -> ServiceInfo:
        self.calls.append(("set_startup", name))
        self.last_start_type = start_type
        return self._svc(name)


class _FakeRegistryController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def set_value(
        self, key: str, name: str, data: str | int | list[str] | None, value_type: RegistryValueType
    ) -> RegistryValue:
        self.calls.append(("set_value", key))
        return RegistryValue(hive=RegistryHive.HKCU, key=key, name=name, type=value_type, data=data)

    def remove_value(self, key: str, name: str) -> None:
        self.calls.append(("remove_value", key))

    def create_key(self, key: str) -> RegistryKey:
        self.calls.append(("create_key", key))
        return RegistryKey(hive=RegistryHive.HKCU, key=key, name="new")

    def remove_key(self, key: str, *, recursive: bool = False) -> None:
        self.calls.append(("remove_key", key))


class _FakeEventLogController:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    def clear(self, log_name: str, *, backup_path: str | None = None) -> None:
        self.cleared.append(log_name)


class _FakeLocalAccountController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def _user(self, name: str, *, enabled: bool = True) -> LocalUser:
        return LocalUser(name=name, sid="S-1-5-21-99", enabled=enabled)

    def new_user(
        self,
        name: str,
        *,
        password: str = "",
        full_name: str = "",
        description: str = "",
        disabled: bool = False,
        timeout: float = 30.0,
    ) -> LocalUser:
        self.calls.append(("new_user", name))
        return self._user(name, enabled=not disabled)

    def remove_user(self, name: str, *, timeout: float = 30.0) -> None:
        self.calls.append(("remove_user", name))

    def set_user_enabled(self, name: str, *, enabled: bool, timeout: float = 30.0) -> LocalUser:
        self.calls.append(("enable" if enabled else "disable", name))
        return self._user(name, enabled=enabled)

    def new_group(self, name: str, *, description: str = "", timeout: float = 30.0) -> LocalGroup:
        self.calls.append(("new_group", name))
        return LocalGroup(name=name, sid="S-1-5-32-999")

    def remove_group(self, name: str, *, timeout: float = 30.0) -> None:
        self.calls.append(("remove_group", name))

    def add_group_member(self, group: str, member: str, *, timeout: float = 30.0) -> None:
        self.calls.append(("add_member", group, member))

    def remove_group_member(self, group: str, member: str, *, timeout: float = 30.0) -> None:
        self.calls.append(("remove_member", group, member))


class _FakeAclController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.last_rights: int | None = None
        self.last_type: AceType | None = None

    def add_ace(self, path: str, trustee: str, rights: int, *, access_type: AceType = AceType.ALLOW) -> None:
        self.calls.append(("add_ace", path, trustee))
        self.last_rights = rights
        self.last_type = access_type

    def remove_ace(self, path: str, trustee: str, *, access_type: AceType | None = None) -> None:
        self.calls.append(("remove_ace", path, trustee))

    def set_owner(self, path: str, owner: str) -> None:
        self.calls.append(("set_owner", path, owner))


class _FakeScheduledTaskController:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def _task(self, task_path: str, *, enabled: bool = True) -> ScheduledTaskInfo:
        name = task_path.rstrip("\\").rpartition("\\")[2]
        return ScheduledTaskInfo(task_name=name, task_path="\\", state=TaskState.READY, enabled=enabled)

    def enable(self, task_path: str) -> ScheduledTaskInfo:
        self.calls.append(("enable", task_path))
        return self._task(task_path, enabled=True)

    def disable(self, task_path: str) -> ScheduledTaskInfo:
        self.calls.append(("disable", task_path))
        return self._task(task_path, enabled=False)

    def run(self, task_path: str) -> None:
        self.calls.append(("run", task_path))

    def stop(self, task_path: str) -> None:
        self.calls.append(("stop", task_path))

    def unregister(self, task_path: str) -> None:
        self.calls.append(("unregister", task_path))

    def register(
        self,
        task_path: str,
        *,
        program: str,
        arguments: str = "",
        description: str = "",
        principal: TaskPrincipal | None = None,
        triggers: Sequence[TaskTrigger] = (),
        settings: TaskSettings | None = None,
    ) -> ScheduledTaskInfo:
        self.calls.append(("register", task_path, program, principal, tuple(triggers), settings))
        return self._task(task_path)


class _FakeWindowsUpdates:
    """Windows Update source: records the criteria and timeout it was asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def __call__(self, criteria: str, *, timeout: float) -> Iterator[WindowsUpdate]:
        self.calls.append((criteria, timeout))
        yield WindowsUpdate(update_id="u-1", title="Cumulative Update", kb_article_ids=("5031455",))


class _FakePacker:
    """Script packer and unpacker, sharing one call log."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def pack_script(
        self, entry: str | Path, dest: str | Path | None = None, *, options: PackOptions = DEFAULT_PACK_OPTIONS
    ) -> PackedScript:
        self.calls.append((str(entry), str(dest), tuple(options.with_packages), options.force))
        return PackedScript(
            output_path=str(dest or "tool.ps1"),
            entry=str(entry),
            files=[str(entry)],
            payload_sha256="ab",
            payload_bytes=1,
        )

    def unpack_script(self, source: str | Path, dest: str | Path, *, force: bool = False) -> PackedScript:
        self.calls.append(("unpack", str(source), str(dest), force))
        return PackedScript(
            output_path=str(dest), entry="tool.py", files=["tool.py"], payload_sha256="ab", payload_bytes=1
        )


class _FakeKnownFolders:
    """Known-folder source: records each member asked for and answers a path named after it."""

    def __init__(self) -> None:
        self.asked: list[object] = []

    def __call__(self, folder: KnownFolder) -> Path | None:
        self.asked.append(folder)
        return None if folder is KnownFolder.PROGRAMS else Path("/known") / folder.value


class _FakeShortcutController:
    """Shortcut controller: records the spec or path it was given and echoes it back as a record."""

    def __init__(self) -> None:
        self.created: list[tuple[ShortcutSpec, bool]] = []
        self.read_paths: list[str] = []

    def create(self, spec: ShortcutSpec, *, force: bool) -> ShortcutInfo:
        self.created.append((spec, force))
        return ShortcutInfo(path=spec.path, target=spec.target, arguments=spec.joined_arguments)

    def read(self, path: str) -> ShortcutInfo:
        self.read_paths.append(path)
        return ShortcutInfo(path=path, target="C:\\Windows\\notepad.exe")


@dataclass
class _InjectedPs:
    """A Ps wired over fresh fakes, plus the fakes that record what the facade delegated."""

    writers: _FakeWriters = field(default_factory=_FakeWriters)
    process_control: _FakeProcessControl = field(default_factory=_FakeProcessControl)
    credential_store: _FakeCredentialStore = field(default_factory=_FakeCredentialStore)
    file_system: _FakeFileSystem = field(default_factory=_FakeFileSystem)
    elevator: _FakeElevator = field(default_factory=_FakeElevator)
    service_controller: _FakeServiceController = field(default_factory=_FakeServiceController)
    registry_controller: _FakeRegistryController = field(default_factory=_FakeRegistryController)
    event_log_controller: _FakeEventLogController = field(default_factory=_FakeEventLogController)
    local_account_controller: _FakeLocalAccountController = field(default_factory=_FakeLocalAccountController)
    acl_controller: _FakeAclController = field(default_factory=_FakeAclController)
    scheduled_task_controller: _FakeScheduledTaskController = field(default_factory=_FakeScheduledTaskController)
    packer: _FakePacker = field(default_factory=_FakePacker)
    windows_image_source: WindowsImageSource = field(default=_fake_windows_images)
    windows_updates: _FakeWindowsUpdates = field(default_factory=_FakeWindowsUpdates)
    known_folders: _FakeKnownFolders = field(default_factory=_FakeKnownFolders)
    shortcut_controller: _FakeShortcutController | None = field(default_factory=_FakeShortcutController)
    acl_source: AclSource = field(default=_fake_acl)
    #: An OS with ACL support, named so the fakes are reached on the macOS CI cell too.
    platform: str = "linux"
    facade: Ps = field(init=False)

    def __post_init__(self) -> None:
        """Wire the facade over one fake per port."""
        self.facade = Ps(
            process_source=_fake_source,
            connection_source=_fake_connections,
            net_adapter_source=_fake_net_adapters,
            net_ip_address_source=_fake_net_ip_addresses,
            net_udp_endpoint_source=_fake_net_udp_endpoints,
            disk_source=_fake_disks,
            uptime_source=_fake_uptime,
            dns_resolver=_fake_resolver,
            connection_tester=_fake_tester,
            environment_source=_fake_env,
            registry_value_source=_fake_registry_values,
            registry_key_source=_fake_registry_keys,
            registry_controller=self.registry_controller,
            service_source=_fake_services,
            service_controller=self.service_controller,
            event_log_source=_fake_event_log,
            event_log_controller=self.event_log_controller,
            cim_source=_fake_cim,
            scheduled_task_source=_fake_scheduled_tasks,
            scheduled_task_controller=self.scheduled_task_controller,
            local_user_source=_fake_local_users,
            local_group_source=_fake_local_groups,
            local_account_controller=self.local_account_controller,
            acl_source=self.acl_source,
            acl_controller=self.acl_controller,
            ps_runner=_fake_runner,
            cmdlet_runner=_fake_cmdlet,
            command_introspector=_fake_get_command,
            process_runner=_fake_exec,
            text_writer=self.writers.write_text,
            text_stream_writer=self.writers.write_text_stream,
            record_writer=self.writers.write_records,
            credential_prompter=_fake_prompt,
            credential_store=self.credential_store,
            file_system=self.file_system,
            web_requester=_fake_web_request,
            rest_invoker=_fake_rest_method,
            file_downloader=self.writers.download_file,
            process_control=self.process_control,
            hotfix_source=_fake_hotfixes,
            windows_image_source=self.windows_image_source,
            windows_update_source=self.windows_updates,
            computer_info_source=_fake_computer_info,
            current_identity_source=_fake_current_identity,
            known_folder_source=self.known_folders,
            shortcut_controller=self.shortcut_controller,
            elevation_check=self.elevator.is_elevated,
            elevator=self.elevator.elevate,
            script_packer=self.packer.pack_script,
            script_unpacker=self.packer.unpack_script,
            platform=self.platform,
        )


@pytest.fixture
def injected() -> _InjectedPs:
    """A Ps composed over injected adapters (Testing-API style), with fresh call logs per test."""
    return _InjectedPs()


@pytest.mark.os_agnostic
def test_injected_system_sources_feed_the_facade(injected: _InjectedPs) -> None:
    """Process, network, disk, uptime, DNS, connection, environment and host-info sources reach the facade."""
    facade = injected.facade
    assert facade.get_process().select(lambda p: p.pid).to_list() == [1, 2]
    assert facade.get_net_tcp_connection().to_list() == []
    assert facade.get_volume().to_list() == []
    assert facade.get_uptime().uptime_seconds == 42.0
    assert facade.resolve_dns_name("host").to_list() == []
    assert facade.test_connection("h", port=22).reachable is True
    assert facade.environment().select(lambda e: e.name).to_list() == ["FOO"]
    assert facade.get_hotfix().select(lambda h: h.hotfix_id).to_list() == ["KB5001"]
    assert facade.get_computer_info().hostname == "pc1"
    assert facade.get_current_identity().sid == "S-1-5-18"
    assert facade.get_net_adapter().select(lambda a: a.name).to_list() == ["eth0"]
    assert facade.get_net_ip_address().select(lambda a: a.address).to_list() == ["10.0.0.5"]
    assert facade.get_net_udp_endpoint().select(lambda c: c.local_port).to_list() == [53]


@pytest.mark.os_agnostic
def test_injected_windows_image_source_filters_by_index(injected: _InjectedPs) -> None:
    """``index`` keeps only that image; without it every image of the file is listed."""
    facade = injected.facade
    assert facade.get_windows_image(Path("x.wim"), index=2).select(lambda i: (i.index, i.name)).to_list() == [
        (2, "x.wim:Home")
    ]
    assert facade.get_windows_image("x.wim").select(lambda i: i.index).to_list() == [1, 2]


@pytest.mark.os_agnostic
def test_a_missing_windows_image_index_raises_naming_path_and_index(injected: _InjectedPs) -> None:
    """An index the file does not hold is an error, not an empty list.

    The facade stays lazy, so the error surfaces when the pipeline is consumed.
    """
    pipeline = injected.facade.get_windows_image("x.wim", index=7)
    with pytest.raises(NativeCallError, match=r"x\.wim.*index 7"):
        pipeline.to_list()


def _image_two_then_damage(path: Path) -> Iterator[WindowsImage]:
    yield WindowsImage(index=1, name="Pro")
    yield WindowsImage(index=2, name="Home")
    # Indices are unique, so a reader that stops after the match never gets here.
    raise AssertionError(f"{path}: read past the requested image")


@pytest.mark.os_agnostic
def test_a_matching_windows_image_index_stops_reading_after_the_match() -> None:
    """Once image N is yielded the source is not advanced, so damage after it is never read."""
    facade = _InjectedPs(windows_image_source=_image_two_then_damage).facade
    assert facade.get_windows_image("x.wim", index=2).select(lambda i: i.name).to_list() == ["Home"]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("index", [0, -1])
def test_a_windows_image_index_below_one_is_refused_at_the_call(injected: _InjectedPs, index: int) -> None:
    """Indices are 1-based; a smaller one is a caller error raised before anything is read."""
    with pytest.raises(ValueError, match=r"index must be >= 1"):
        injected.facade.get_windows_image("x.wim", index=index)


@pytest.mark.os_agnostic
def test_injected_registry_adapters(injected: _InjectedPs) -> None:
    """Registry reads go to the sources and writes go to the controller, in call order."""
    facade = injected.facade
    fake_registry_controller = injected.registry_controller
    assert facade.get_item_property("HKLM/X").select(lambda v: v.name).to_list() == ["Build"]
    assert facade.registry_keys("HKLM/X").select(lambda k: k.name).to_list() == ["Sub"]
    assert facade.set_item_property("HKCU/S", "N", 1, RegistryValueType.REG_DWORD).name == "N"
    facade.remove_item_property("HKCU/S", "N")
    assert facade.new_registry_key("HKCU/S/K").name == "new"
    facade.remove_registry_key("HKCU/S/K", recursive=True)
    assert fake_registry_controller.calls == [
        ("set_value", "HKCU/S"),
        ("remove_value", "HKCU/S"),
        ("create_key", "HKCU/S/K"),
        ("remove_key", "HKCU/S/K"),
    ]


@pytest.mark.os_agnostic
def test_injected_service_adapters(injected: _InjectedPs) -> None:
    """Service reads go to the source and control verbs go to the controller, in call order."""
    facade = injected.facade
    fake_service_controller = injected.service_controller
    assert facade.get_service().select(lambda s: s.name).to_list() == ["Svc"]
    # [86] the per-call strict choice reaches the source; lenient is the default.
    assert facade.get_service().select(lambda s: s.display_name).to_list() == ["strict=False"]
    assert facade.get_service(strict=True).select(lambda s: s.display_name).to_list() == ["strict=True"]
    assert facade.start_service("Svc").name == "Svc"
    assert facade.stop_service("Svc").status is ServiceState.RUNNING
    assert facade.restart_service("Svc").name == "Svc"
    assert facade.set_service("Svc", ServiceStartType.DISABLED).name == "Svc"
    assert fake_service_controller.calls == [
        ("start", "Svc"),
        ("stop", "Svc"),
        ("restart", "Svc"),
        ("set_startup", "Svc"),
    ]
    assert fake_service_controller.last_start_type is ServiceStartType.DISABLED


@pytest.mark.os_agnostic
def test_stop_service_forwards_the_wait_choice(injected: _InjectedPs) -> None:
    """stop_service waits unless told not to, and hands that choice to the controller."""
    injected.facade.stop_service("Svc")
    injected.facade.stop_service("Svc", wait=False)
    assert injected.service_controller.stop_waits == [True, False]


@pytest.mark.os_agnostic
def test_injected_event_log_and_cim_adapters(injected: _InjectedPs) -> None:
    """Event-log reads and clears, and CIM queries, reach their injected adapters."""
    facade = injected.facade
    fake_event_log_controller = injected.event_log_controller
    assert facade.get_win_event("System").select(lambda e: e.event_id).to_list() == [5]
    facade.clear_event_log("Application")
    assert fake_event_log_controller.cleared == ["Application"]
    cim_first = facade.get_cim_instance("Win32_Bios", where="x=1").first()
    assert cim_first is not None
    assert cim_first.properties == {"where": "x=1", "namespace": "root/cimv2"}


@pytest.mark.os_agnostic
def test_injected_scheduled_task_adapters(injected: _InjectedPs) -> None:
    """Scheduled-task reads go to the source and control verbs go to the controller, in call order."""
    facade = injected.facade
    fake_scheduled_task_controller = injected.scheduled_task_controller
    assert facade.get_scheduled_task().select(lambda t: t.task_name).to_list() == ["T"]
    # [54] the per-call strict choice reaches the source; lenient is the default.
    assert facade.get_scheduled_task().select(lambda t: t.description).to_list() == ["strict=False"]
    assert facade.get_scheduled_task("\\F\\", strict=True).select(lambda t: (t.task_path, t.description)).to_list() == [
        ("\\F\\", "strict=True")
    ]
    # [50] a by-name lookup hands folder, name and strict to the source unchanged.
    looked_up = facade.get_scheduled_task("\\F\\", task_name="x", strict=True).to_list()
    assert [(t.task_path, t.task_name, t.description) for t in looked_up] == [("\\F\\", "x", "strict=True")]
    assert facade.register_scheduled_task("\\T", program="cmd.exe", arguments="/c exit").task_name == "T"
    facade.unregister_scheduled_task("\\T")
    assert facade.enable_scheduled_task("\\T").enabled is True
    assert facade.disable_scheduled_task("\\T").enabled is False
    facade.start_scheduled_task("\\T")
    facade.stop_scheduled_task("\\T")
    assert fake_scheduled_task_controller.calls == [
        ("register", "\\T", "cmd.exe", None, (), None),
        ("unregister", "\\T"),
        ("enable", "\\T"),
        ("disable", "\\T"),
        ("run", "\\T"),
        ("stop", "\\T"),
    ]


@pytest.mark.os_agnostic
def test_register_scheduled_task_forwards_principal_and_triggers(injected: _InjectedPs) -> None:
    """The facade hands the principal and the triggers to the controller unchanged."""
    trigger = BootTrigger()
    injected.facade.register_scheduled_task("\\T", program="x", principal=TaskPrincipal.system(), triggers=[trigger])
    assert injected.scheduled_task_controller.calls[-1] == (
        "register",
        "\\T",
        "x",
        TaskPrincipal.system(),
        (trigger,),
        None,
    )


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "settings",
    [
        TaskSettings(),
        TaskSettings(
            allow_start_if_on_batteries=True,
            stop_if_going_on_batteries=False,
            execution_time_limit=timedelta(hours=72),
            multiple_instances=TaskMultipleInstances.IGNORE_NEW,
            allow_hard_terminate=True,
            allow_demand_start=False,
        ),
    ],
)
def test_register_scheduled_task_forwards_the_settings(injected: _InjectedPs, settings: TaskSettings) -> None:
    """[45] The facade hands an equal TaskSettings to the controller; TaskSettings() stays all-None."""
    injected.facade.register_scheduled_task("\\T", program="x", settings=settings)
    assert injected.scheduled_task_controller.calls[-1][-1] == settings


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("limit", "reason"),
    [
        (timedelta(seconds=1.5), "whole seconds"),
        (timedelta(seconds=2147483648), "2147483647"),
        (timedelta(seconds=-1), "negative"),
    ],
)
def test_register_scheduled_task_revalidates_unvalidated_settings(
    injected: _InjectedPs, limit: timedelta, reason: str
) -> None:
    """[45] A model_construct-built TaskSettings (as read back onto a record) is validated before the controller."""
    unvalidated = TaskSettings.model_construct(execution_time_limit=limit)
    with pytest.raises(ValidationError, match=reason):
        injected.facade.register_scheduled_task("\\T", program="x", settings=unvalidated)
    assert injected.scheduled_task_controller.calls == []


@pytest.mark.os_agnostic
def test_injected_local_account_adapters(injected: _InjectedPs) -> None:
    """Local user and group reads go to the sources and changes go to the controller, in call order."""
    facade = injected.facade
    fake_local_account_controller = injected.local_account_controller
    assert facade.get_local_user().select(lambda u: u.sid).to_list() == ["S-1-5-21-1"]
    assert facade.get_local_group().select(lambda g: g.name).to_list() == ["g"]
    assert facade.new_local_user("u1", disabled=True).enabled is False
    facade.remove_local_user("u1")
    assert facade.enable_local_user("u1").enabled is True
    assert facade.disable_local_user("u1").enabled is False
    assert facade.new_local_group("g1").sid == "S-1-5-32-999"
    facade.remove_local_group("g1")
    facade.add_local_group_member("g1", "u1")
    facade.remove_local_group_member("g1", "u1")
    assert fake_local_account_controller.calls == [
        ("new_user", "u1"),
        ("remove_user", "u1"),
        ("enable", "u1"),
        ("disable", "u1"),
        ("new_group", "g1"),
        ("remove_group", "g1"),
        ("add_member", "g1", "u1"),
        ("remove_member", "g1", "u1"),
    ]


@pytest.mark.os_agnostic
def test_injected_acl_adapters(injected: _InjectedPs) -> None:
    """ACL reads go to the source and ACE/owner changes go to the controller, in call order."""
    facade = injected.facade
    fake_acl_controller = injected.acl_controller
    assert facade.get_acl("P").select(lambda e: e.trustee_sid).to_list() == ["S-1-5-32-544"]
    facade.add_acl_ace("C:/x", "S-1-5-32-545", 0x1200A9, access_type=AceType.DENY)
    facade.remove_acl_ace("C:/x", "S-1-5-32-545")
    facade.set_owner("C:/x", "S-1-5-32-544")
    assert fake_acl_controller.calls == [
        ("add_ace", "C:/x", "S-1-5-32-545"),
        ("remove_ace", "C:/x", "S-1-5-32-545"),
        ("set_owner", "C:/x", "S-1-5-32-544"),
    ]
    assert fake_acl_controller.last_rights == 0x1200A9
    assert fake_acl_controller.last_type is AceType.DENY


def _call_each_acl_verb(facade: Ps) -> Iterator[tuple[str, PlatformUnsupportedError]]:
    """Call get_acl and the three ACL verbs; yield each verb with the refusal it raised."""
    calls = {
        "get_acl": lambda: facade.get_acl("/tmp"),
        "add_acl_ace": lambda: facade.add_acl_ace("/tmp", "user:nobody", 4),
        "remove_acl_ace": lambda: facade.remove_acl_ace("/tmp", "user:nobody"),
        "set_owner": lambda: facade.set_owner("/tmp", "nobody"),
    }
    for verb, call in calls.items():
        with pytest.raises(PlatformUnsupportedError) as caught:
            call()
        yield verb, caught.value


@pytest.mark.os_agnostic
def test_acl_verbs_on_macos_are_platform_unsupported_before_any_adapter_call() -> None:
    """macOS has no POSIX ACL xattr backend: all four verbs refuse, and no adapter is reached."""
    read: list[str] = []

    def _recording_acl(path: str) -> Iterator[AclEntry]:
        read.append(path)
        return _fake_acl(path)

    injected = _InjectedPs(platform="darwin", acl_source=_recording_acl)
    refusals = dict(_call_each_acl_verb(injected.facade))
    assert sorted(refusals) == ["add_acl_ace", "get_acl", "remove_acl_ace", "set_owner"]
    for verb, error in refusals.items():
        assert str(error) == f"ps.{verb} needs Windows or Linux: macOS ACLs are not supported (got darwin)"
    assert read == []
    assert injected.acl_controller.calls == []


@pytest.mark.os_agnostic
def test_acl_verbs_on_another_posix_os_are_platform_unsupported_naming_it() -> None:
    """Only Windows and Linux have an ACL backend: any other OS is refused by its own name."""
    injected = _InjectedPs(platform="freebsd14")
    for verb, error in _call_each_acl_verb(injected.facade):
        assert str(error) == f"ps.{verb} needs Windows or Linux: ACLs are not supported (got freebsd14)"
    assert injected.acl_controller.calls == []


@pytest.mark.os_agnostic
@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_acl_verbs_reach_the_adapters_on_windows_and_linux(platform: str) -> None:
    """The guard lets both supported OSes through to the injected adapters."""
    injected = _InjectedPs(platform=platform)
    assert injected.facade.get_acl("P").select(lambda e: e.trustee_sid).to_list() == ["S-1-5-32-544"]
    injected.facade.set_owner("P", "S-1-5-32-544")
    assert injected.acl_controller.calls == [("set_owner", "P", "S-1-5-32-544")]


@pytest.mark.os_macos
@pytest.mark.skipif(sys.platform != "darwin", reason="the production facade's macOS refusal")
def test_production_acl_verbs_are_platform_unsupported_on_macos() -> None:
    """On macOS the real facade refuses all four verbs (posix_acl would raise a raw AttributeError)."""
    refusals = dict(_call_each_acl_verb(build_ps()))
    assert sorted(refusals) == ["add_acl_ace", "get_acl", "remove_acl_ace", "set_owner"]
    assert all("macOS ACLs are not supported" in str(error) for error in refusals.values())


@pytest.mark.os_agnostic
def test_injected_powershell_runner_adapters(injected: _InjectedPs) -> None:
    """run, cmdlet, get_command and the module-cmdlet wrappers reach the injected runners."""
    facade = injected.facade
    assert facade.run("x", timeout=1.0) == ["ran", "x", 1.0]
    cmdlet_result = facade.cmdlet("Get-Item", "C:/", timeout=2.0, Path="C:/", Recurse=True)
    assert cmdlet_result.output == ["Get-Item", ["C:/"], {"Path": "C:/", "Recurse": True}, 2.0]
    command = facade.get_command("Get-Item")
    assert command.name == "Get-Item"
    assert command.parameters[0].name == "Path"
    # Module-cmdlet wrappers delegate to cmdlet() with the real cmdlet name + safe kwargs.
    assert facade.get_ad_user(Filter="*").output[0] == "Get-ADUser"
    assert facade.get_ad_group(Identity="Admins").output[:1] == ["Get-ADGroup"]
    assert facade.get_ad_computer().output[0] == "Get-ADComputer"
    assert facade.get_mailbox(Identity="a@b.c").output[0] == "Get-Mailbox"
    assert facade.get_az_vm().output[0] == "Get-AzVM"
    assert facade.get_az_resource_group().output[0] == "Get-AzResourceGroup"
    assert facade.get_ad_user(Filter="*").output[2] == {"Filter": "*"}


@pytest.mark.os_agnostic
def test_injected_exec_and_writer_adapters(injected: _InjectedPs) -> None:
    """exec reaches the process runner, and the writers are called in order with their options."""
    facade = injected.facade
    write_calls = injected.writers.calls
    exec_result = facade.exec(["git", "status"], cwd="C:/repo")
    assert exec_result.argv == ["git", "status"]
    assert exec_result.stdout == "cwd=C:/repo"
    assert exec_result.stderr == "wait_tree=False"  # the default is today's behaviour
    assert facade.exec(["setup.exe"], wait_tree=True).stderr == "wait_tree=True"
    assert facade.write_text("out.txt", "hi", bom=True) == Path("out.txt")
    assert facade.write_records("recs.jsonl", [EnvVar(name="A", value="1")]) == Path("recs.jsonl")
    assert facade.write_text_stream("s.txt", ["a\n", "b\n"], newline="\r\n") == Path("s.txt")
    assert write_calls == [
        ("text", "out.txt", "hi", "utf-8", "True"),
        ("records", "recs.jsonl", "True"),
        ("stream", "s.txt", "a\nb\n", "\r\n"),
    ]


@pytest.mark.os_agnostic
def test_injected_credential_adapters(injected: _InjectedPs) -> None:
    """The prompter masks the secret on serialization, and the store saves, loads and deletes."""
    facade = injected.facade
    prompted = facade.get_credential("svc", target="db")
    assert prompted.username == "svc"
    assert prompted.to_dict()["secret"] == "**********"  # masked in serialization
    assert facade.save_credential("db", "svc", "pw").username == "svc"
    loaded = facade.load_credential("db")
    assert loaded is not None
    assert loaded.secret.get_secret_value() == "pw"
    facade.delete_credential("db")
    assert facade.load_credential("db") is None


@pytest.mark.os_agnostic
def test_injected_process_control_adapter(injected: _InjectedPs) -> None:
    """Process and computer control verbs reach the injected controller with their options, in order."""
    facade = injected.facade
    fake_process_control = injected.process_control
    facade.stop_process(123, force=True)
    assert facade.wait_process(123) == 0
    facade.restart_computer(delay_seconds=5)
    facade.stop_computer()
    assert fake_process_control.calls == [
        ("stop_process", "123", "True"),
        ("wait_process", "123"),
        ("restart_computer", "5", "True"),
        ("stop_computer", "0", "True"),
    ]


@pytest.mark.os_agnostic
def test_injected_web_adapters(injected: _InjectedPs) -> None:
    """Web requests, REST calls and downloads reach their injected adapters."""
    facade = injected.facade
    write_calls = injected.writers.calls
    web = facade.invoke_web_request("http://x/", method="POST", body="hi")
    assert web.status_code == 200
    assert web.text == "POST hi skip=False"
    assert facade.invoke_web_request("http://x/", skip_http_error_check=True).text == "GET None skip=True"
    assert facade.invoke_rest_method("http://api/", json_body={"k": 1}) == {
        "url": "http://api/",
        "echo": {"k": 1},
        "skip": False,
    }
    assert facade.invoke_rest_method("http://api/", skip_http_error_check=True) == {
        "url": "http://api/",
        "echo": None,
        "skip": True,
    }
    assert facade.download_file("http://x/f.bin", "out.bin") == Path("out.bin")
    assert ("download", "http://x/f.bin", "out.bin") in write_calls


@pytest.mark.os_agnostic
def test_injected_file_system_adapter(injected: _InjectedPs) -> None:
    """File-system verbs reach the injected file system with their options, in call order."""
    facade = injected.facade
    fake_file_system = injected.file_system
    assert facade.get_child_item("/d", recurse=True).select(lambda f: f.name).to_list() == ["a.txt"]
    assert facade.get_item("/d/x").name == "x"
    assert facade.get_content("/d/x") == "hello"
    assert facade.get_content_lines("/d/x").to_list() == ["l1\n", "l2\n"]
    assert facade.test_path("/d/x") is True
    assert facade.new_item("/d/sub", item_type=FileItemType.DIRECTORY).is_directory is True
    facade.copy_item("/a", "/b", recurse=True)
    facade.move_item("/a", "/b")
    facade.remove_item("/a", recurse=True)
    assert fake_file_system.calls == [
        ("get_child_item", "/d", "True"),
        ("get_item", "/d/x"),
        ("get_content", "/d/x", "utf-8"),
        ("get_content_lines", "/d/x", "utf-8"),
        ("test_path", "/d/x"),
        ("new_item", "/d/sub", "directory"),
        ("copy_item", "/a", "/b", "True"),
        ("move_item", "/a", "/b"),
        ("remove_item", "/a", "True"),
    ]


@pytest.mark.os_agnostic
def test_injected_elevation_adapters(injected: _InjectedPs) -> None:
    """The elevation check gates require_elevation, and elevate forwards its arguments."""
    facade = injected.facade
    elevate_calls = injected.elevator.calls
    assert facade.is_elevated() is False
    with pytest.raises(ElevationRequiredError):
        facade.require_elevation()
    assert facade.elevate(["a", "b"], cwd="C:/work", wait=False) == 7
    assert elevate_calls == [(["a", "b"], None, "C:/work", False)]


@pytest.mark.os_agnostic
def test_injected_script_packer_adapters(injected: _InjectedPs) -> None:
    """pack_script and unpack_script reach the injected packer with their options, in call order."""
    facade = injected.facade
    pack_calls = injected.packer.calls
    assert (
        facade.pack_script("tool.py", "out.ps1", options=PackOptions(with_packages=["rich"], force=True)).entry
        == "tool.py"
    )
    assert facade.unpack_script("out.ps1", "restored").files == ["tool.py"]
    assert pack_calls == [
        ("tool.py", "out.ps1", ("rich",), True),
        ("unpack", "out.ps1", "restored", False),
    ]


@pytest.mark.os_agnostic
def test_get_windows_update_forwards_criteria_and_timeout(injected: _InjectedPs) -> None:
    """The facade hands its defaults, or the caller's values, to the source and wraps it in a Pipeline."""
    facade = injected.facade
    pipeline = facade.get_windows_update()
    assert isinstance(pipeline, Pipeline)
    assert pipeline.select(lambda u: u.kb_article_ids).to_list() == [("5031455",)]
    facade.get_windows_update(criteria="IsInstalled=1", timeout=2147483).to_list()
    assert injected.windows_updates.calls == [
        ("IsInstalled=0 and Type='Software'", 300.0),
        ("IsInstalled=1", 2147483),
    ]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("bad", [0, -1.0, 2147483.5, float("nan"), float("inf")])
def test_get_windows_update_refuses_a_timeout_outside_the_waitable_range(injected: _InjectedPs, bad: float) -> None:
    """A timeout outside (0, 2147483] raises ValueError at the call, before the source is asked."""
    with pytest.raises(ValueError, match="timeout"):
        injected.facade.get_windows_update(timeout=bad)
    assert injected.windows_updates.calls == []


@pytest.mark.os_agnostic
@pytest.mark.parametrize("bad", [-1.0, 0.0, float("nan"), 1e12])
def test_exec_refuses_a_timeout_outside_the_waitable_range(injected: _InjectedPs, bad: float) -> None:
    """[D3]: exec takes (0, 2147483] like get_windows_update and refuses anything else at the call,
    before a child starts (it was: -1 a timeout at once, NaN an internal error, 1e12 an OverflowError)."""
    with pytest.raises(ValueError, match="timeout must be in"):
        injected.facade.exec(["prog"], timeout=bad)


# --- Known folders and shortcuts ---------------------------------------------------------------


@pytest.mark.os_agnostic
def test_get_known_folder_hands_the_member_to_the_source(injected: _InjectedPs) -> None:
    """A member, or its .NET SpecialFolder name, reaches the source as the member; the answer comes back."""
    facade = injected.facade
    assert facade.get_known_folder(KnownFolder.DESKTOP) == Path("/known/Desktop")
    assert facade.get_known_folder("CommonApplicationData") == Path("/known/CommonApplicationData")
    assert facade.get_known_folder(KnownFolder.PROGRAMS) is None
    assert injected.known_folders.asked == [KnownFolder.DESKTOP, KnownFolder.PROGRAM_DATA, KnownFolder.PROGRAMS]
    assert all(type(asked) is KnownFolder for asked in injected.known_folders.asked)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("name", ["desktop", "Startup", "DESKTOP", ""])
def test_get_known_folder_refuses_a_name_that_is_no_member_value(injected: _InjectedPs, name: str) -> None:
    """Only the exact .NET SpecialFolder names are taken; anything else is refused before the source is asked."""
    with pytest.raises(ValueError, match="KnownFolder"):
        injected.facade.get_known_folder(name)
    assert injected.known_folders.asked == []


@pytest.mark.os_agnostic
def test_build_ps_resolves_known_folders_with_the_running_oses_resolver() -> None:
    """The production facade answers what the resolver for this OS answers, member by member."""
    resolver = windows_known_folder if _ON_WINDOWS else posix_known_folder
    facade = build_ps()
    for member in KnownFolder:
        if member is KnownFolder.PROGRAMS and sys.platform != "win32":
            with pytest.raises(PlatformUnsupportedError, match="Programs"):
                facade.get_known_folder(member)
            continue
        assert facade.get_known_folder(member) == resolver(member), member


@pytest.mark.os_agnostic
def test_new_shortcut_builds_the_spec_and_returns_the_controllers_record(injected: _InjectedPs) -> None:
    """Every keyword reaches the spec, force reaches create, and the controller's record is returned."""
    info = injected.facade.new_shortcut(
        "C:\\Users\\me\\Desktop\\Tool.lnk",
        target="%SystemRoot%\\notepad.exe",
        arguments=["C:\\notes\\a b.txt", "-x"],
        working_directory="C:\\notes",
        icon="C:\\Windows\\notepad.exe",
        icon_index=-2,
        description="Notes",
        force=True,
    )
    assert injected.shortcut_controller is not None
    [(spec, force)] = injected.shortcut_controller.created
    assert force is True
    assert spec == ShortcutSpec(
        path="C:\\Users\\me\\Desktop\\Tool.lnk",
        target="%SystemRoot%\\notepad.exe",
        arguments=("C:\\notes\\a b.txt", "-x"),
        working_directory="C:\\notes",
        icon="C:\\Windows\\notepad.exe",
        icon_index=-2,
        description="Notes",
    )
    assert info == ShortcutInfo(
        path="C:\\Users\\me\\Desktop\\Tool.lnk",
        target="%SystemRoot%\\notepad.exe",
        arguments='"C:\\notes\\a b.txt" -x',
    )


@pytest.mark.os_agnostic
def test_new_shortcut_defaults_leave_the_optional_fields_unset_and_do_not_force(injected: _InjectedPs) -> None:
    """Without the optional keywords the spec carries no arguments, no optional text and force False."""
    injected.facade.new_shortcut("C:\\Tool.lnk", target="C:\\Tools\\tool.exe")
    assert injected.shortcut_controller is not None
    assert injected.shortcut_controller.created == [
        (ShortcutSpec(path="C:\\Tool.lnk", target="C:\\Tools\\tool.exe"), False)
    ]


@pytest.mark.os_agnostic
def test_new_shortcut_normalises_the_path_before_the_length_limit(injected: _InjectedPs) -> None:
    """The .lnk path is made absolute and normalised first, so the 259 limit applies to what COM receives.

    The given text is 262 characters; normalised it is ``C:\\Tool.lnk``, which the spec accepts. A
    path-like is taken as well, and its forward slashes come out as backslashes.
    """
    long_detour = "C:\\" + "x" * 250 + "\\..\\Tool.lnk"
    assert len(long_detour) > 259
    injected.facade.new_shortcut(long_detour, target="C:\\Tools\\tool.exe")
    injected.facade.new_shortcut(PureWindowsPath("C:/Users/me/Tool.lnk"), target="C:\\Tools\\tool.exe")
    assert injected.shortcut_controller is not None
    assert [spec.path for spec, _force in injected.shortcut_controller.created] == [
        "C:\\Tool.lnk",
        "C:\\Users\\me\\Tool.lnk",
    ]


@pytest.mark.os_agnostic
def test_new_shortcut_lets_the_specs_configuration_error_through_before_the_controller(
    injected: _InjectedPs,
) -> None:
    """A refused field surfaces as the spec's own ConfigurationError, and nothing is written."""
    with pytest.raises(ConfigurationError, match="target"):
        injected.facade.new_shortcut("C:\\Tool.lnk", target="notepad.exe")
    assert injected.shortcut_controller is not None
    assert injected.shortcut_controller.created == []


@pytest.mark.os_agnostic
def test_get_shortcut_reads_the_normalised_path(injected: _InjectedPs) -> None:
    """get_shortcut hands the controller the absolute, normalised path and returns its record."""
    info = injected.facade.get_shortcut("C:\\Users\\me\\..\\Public\\Tool.lnk")
    assert info.path == "C:\\Users\\Public\\Tool.lnk"
    assert injected.shortcut_controller is not None
    assert injected.shortcut_controller.read_paths == ["C:\\Users\\Public\\Tool.lnk"]


@pytest.mark.os_agnostic
def test_without_a_shortcut_controller_both_verbs_are_platform_unsupported_before_the_spec() -> None:
    """No controller (not Windows): PlatformUnsupportedError, even for input the spec would refuse."""
    facade = _InjectedPs(shortcut_controller=None).facade
    with pytest.raises(PlatformUnsupportedError, match="new_shortcut"):
        facade.new_shortcut("relative.txt", target="notepad.exe")
    with pytest.raises(PlatformUnsupportedError, match="get_shortcut"):
        facade.get_shortcut("C:\\Tool.lnk")


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the off-Windows refusal of the production facade")
def test_production_shortcut_verbs_are_platform_unsupported_off_windows(tmp_path: Path) -> None:
    """Off Windows the facade refuses before the adapter's own file checks, which would say NativeCallError.

    An existing file would be "already exists" for new_shortcut and a missing one "does not exist"
    for get_shortcut, were the adapter reached.
    """
    existing = tmp_path / "Tool.lnk"
    existing.write_bytes(b"not a shortcut")
    missing = tmp_path / "Missing.lnk"
    facade = build_ps()
    with pytest.raises(PlatformUnsupportedError, match="Windows"):
        facade.new_shortcut(existing, target="C:\\Windows\\notepad.exe")
    with pytest.raises(PlatformUnsupportedError, match="Windows"):
        facade.get_shortcut(missing)
    with pytest.raises(PlatformUnsupportedError, match="Windows"):
        facade.get_shortcut(existing)
    assert existing.read_bytes() == b"not a shortcut"


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="writes and reads a real .lnk through IShellLinkW")
def test_production_shortcut_round_trip_from_a_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative .lnk path is resolved against the working directory, written, and read back."""
    monkeypatch.chdir(tmp_path)
    facade = build_ps()
    created = facade.new_shortcut("Tool.lnk", target="%SystemRoot%\\notepad.exe", arguments=["a b"])
    assert (tmp_path / "Tool.lnk").is_file()
    assert created.path == str(tmp_path / "Tool.lnk")
    assert created.target.lower().endswith("\\notepad.exe")
    assert created.arguments == '"a b"'
    assert facade.get_shortcut("Tool.lnk") == created
    with pytest.raises(NativeCallError, match="already exists"):
        facade.new_shortcut("Tool.lnk", target="%SystemRoot%\\notepad.exe")


@pytest.mark.os_agnostic
def test_the_package_exports_the_known_folder_and_shortcut_types() -> None:
    """KnownFolder and ShortcutInfo are public; ShortcutSpec stays internal (no facade method takes it)."""
    assert pwshpy.KnownFolder is KnownFolder
    assert pwshpy.ShortcutInfo is ShortcutInfo
    assert {"KnownFolder", "ShortcutInfo"} <= set(pwshpy.__all__)
    assert "ShortcutSpec" not in pwshpy.__all__
