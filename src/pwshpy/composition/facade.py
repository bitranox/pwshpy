"""The ``ps`` facade — the public entry point wiring adapters to the pipeline.

``ps`` is where library and CLI meet: both call the same facade, so they never
diverge.  native sources return a :class:`~pwshpy.domain.pipeline.Pipeline`;
``ps.run`` reaches .NET (hosts PowerShell in-process) behind the ``[full]`` extra.

Contents:
    * :class:`Ps` — the facade binding a process source + PowerShell runner.
    * :data:`ps` — the default production facade (``from pwshpy import ps``).
    * :func:`build_ps` — construct a facade from explicit adapters (for tests).
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..adapters.native import NativeAclController as _NativeAclController
from ..adapters.native import NativeCredentialStore as _NativeCredentialStore
from ..adapters.native import NativeEventLogController as _NativeEventLogController
from ..adapters.native import NativeFileSystem as _NativeFileSystem
from ..adapters.native import NativeLocalAccountController as _NativeLocalAccountController
from ..adapters.native import NativeProcessControl as _NativeProcessControl
from ..adapters.native import NativeRegistryController as _NativeRegistryController
from ..adapters.native import NativeScheduledTaskController as _NativeScheduledTaskController
from ..adapters.native import NativeServiceController as _NativeServiceController
from ..adapters.native import PosixAclController as _PosixAclController
from ..adapters.native import PosixLocalAccountController as _PosixLocalAccountController
from ..adapters.native import SecretServiceCredentialStore as _SecretServiceCredentialStore
from ..adapters.native import SystemdScheduledTaskController as _SystemdScheduledTaskController
from ..adapters.native import SystemdServiceController as _SystemdServiceController
from ..adapters.native import download_file as _download_file
from ..adapters.native import elevate as _elevate
from ..adapters.native import get_computer_info as _get_computer_info
from ..adapters.native import get_uptime as _get_uptime
from ..adapters.native import invoke_rest_method as _invoke_rest_method
from ..adapters.native import invoke_web_request as _invoke_web_request
from ..adapters.native import is_elevated as _is_elevated
from ..adapters.native import iter_acl as _iter_acl
from ..adapters.native import iter_cim as _iter_cim
from ..adapters.native import iter_connections as _iter_connections
from ..adapters.native import iter_disks as _iter_disks
from ..adapters.native import iter_env as _iter_env
from ..adapters.native import iter_event_log as _iter_event_log
from ..adapters.native import iter_hotfixes as _iter_hotfixes
from ..adapters.native import iter_local_groups as _iter_local_groups
from ..adapters.native import iter_local_users as _iter_local_users
from ..adapters.native import iter_net_adapters as _iter_net_adapters
from ..adapters.native import iter_net_ip_addresses as _iter_net_ip_addresses
from ..adapters.native import iter_net_udp_endpoints as _iter_net_udp_endpoints
from ..adapters.native import iter_processes as _iter_processes
from ..adapters.native import iter_registry_keys as _iter_registry_keys
from ..adapters.native import iter_registry_values as _iter_registry_values
from ..adapters.native import iter_scheduled_tasks as _iter_scheduled_tasks
from ..adapters.native import iter_services as _iter_services
from ..adapters.native import journald_iter_event_log as _journald_iter_event_log
from ..adapters.native import pack_script as _pack_script
from ..adapters.native import posix_iter_acl as _posix_iter_acl
from ..adapters.native import posix_iter_local_groups as _posix_iter_local_groups
from ..adapters.native import posix_iter_local_users as _posix_iter_local_users
from ..adapters.native import prompt_credential as _prompt_credential
from ..adapters.native import resolve as _resolve
from ..adapters.native import run_process as _run_process
from ..adapters.native import systemd_iter_services as _systemd_iter_services
from ..adapters.native import systemd_iter_timers as _systemd_iter_timers
from ..adapters.native import test_connection as _test_connection
from ..adapters.native import unpack_script as _unpack_script
from ..adapters.native import write_records as _write_records
from ..adapters.native import write_text as _write_text
from ..adapters.native import write_text_stream as _write_text_stream
from ..adapters.powershell import get_command as _ps_get_command
from ..adapters.powershell import invoke as _ps_invoke
from ..adapters.powershell import run as _ps_run
from ..application.ports import (
    AclController,
    AclSource,
    CimSource,
    CmdletRunner,
    CommandIntrospector,
    ComputerInfoSource,
    ConnectionTester,
    CredentialPrompter,
    CredentialStore,
    DiskSource,
    DnsResolver,
    ElevationCheck,
    Elevator,
    EnvironmentSource,
    EventLogController,
    EventLogSource,
    FileDownloader,
    FileSystem,
    HotfixSource,
    LocalAccountController,
    LocalGroupSource,
    LocalUserSource,
    NetAdapterSource,
    NetConnectionSource,
    NetIpAddressSource,
    NetUdpEndpointSource,
    PowerShellRunner,
    ProcessControl,
    ProcessRunner,
    ProcessSource,
    RecordWriter,
    RegistryController,
    RegistryKeySource,
    RegistryValueSource,
    RestInvoker,
    ScheduledTaskController,
    ScheduledTaskSource,
    ScriptPacker,
    ScriptUnpacker,
    ServiceController,
    ServiceSource,
    TextStreamWriter,
    TextWriter,
    UptimeSource,
    WebRequester,
)
from ..domain.enums import AceType, FileItemType, RegistryValueType, ServiceStartType
from ..domain.errors import ElevationRequiredError
from ..domain.packing import DEFAULT_PACK_OPTIONS, PackOptions
from ..domain.pipeline import Pipeline
from ..domain.records import (
    AclEntry,
    CimInstance,
    CommandInfo,
    ComputerInfo,
    ConnectionTest,
    Credential,
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
    PSRecord,
    RegistryKey,
    RegistryValue,
    ScheduledTaskInfo,
    ServiceInfo,
    SystemUptime,
    WebResponse,
)

#: Platform switch for the local-accounts backend. Stored as an opaque bool so the type
#: checker does not statically prune the "other OS" branch (and flag its import unused).
_ON_WINDOWS: bool = sys.platform == "win32"


class Ps:
    """Fluent facade over both backends, returning the same typed pipeline.

    Example:
        >>> facade = build_ps()
        >>> facade.get_process().take(1).to_list()  # doctest: +ELLIPSIS
        [ProcessInfo(...)]
    """

    def __init__(
        self,
        *,
        process_source: ProcessSource,
        connection_source: NetConnectionSource,
        net_adapter_source: NetAdapterSource,
        net_ip_address_source: NetIpAddressSource,
        net_udp_endpoint_source: NetUdpEndpointSource,
        disk_source: DiskSource,
        uptime_source: UptimeSource,
        dns_resolver: DnsResolver,
        connection_tester: ConnectionTester,
        environment_source: EnvironmentSource,
        registry_value_source: RegistryValueSource,
        registry_key_source: RegistryKeySource,
        registry_controller: RegistryController,
        service_source: ServiceSource,
        service_controller: ServiceController,
        event_log_source: EventLogSource,
        event_log_controller: EventLogController,
        cim_source: CimSource,
        scheduled_task_source: ScheduledTaskSource,
        scheduled_task_controller: ScheduledTaskController,
        local_user_source: LocalUserSource,
        local_group_source: LocalGroupSource,
        local_account_controller: LocalAccountController,
        acl_source: AclSource,
        acl_controller: AclController,
        ps_runner: PowerShellRunner,
        cmdlet_runner: CmdletRunner,
        command_introspector: CommandIntrospector,
        process_runner: ProcessRunner,
        text_writer: TextWriter,
        text_stream_writer: TextStreamWriter,
        record_writer: RecordWriter,
        credential_prompter: CredentialPrompter,
        credential_store: CredentialStore,
        file_system: FileSystem,
        web_requester: WebRequester,
        rest_invoker: RestInvoker,
        file_downloader: FileDownloader,
        process_control: ProcessControl,
        hotfix_source: HotfixSource,
        computer_info_source: ComputerInfoSource,
        elevation_check: ElevationCheck,
        elevator: Elevator,
        script_packer: ScriptPacker,
        script_unpacker: ScriptUnpacker,
    ) -> None:
        self._process_source = process_source
        self._connection_source = connection_source
        self._net_adapter_source = net_adapter_source
        self._net_ip_address_source = net_ip_address_source
        self._net_udp_endpoint_source = net_udp_endpoint_source
        self._disk_source = disk_source
        self._uptime_source = uptime_source
        self._dns_resolver = dns_resolver
        self._connection_tester = connection_tester
        self._environment_source = environment_source
        self._registry_value_source = registry_value_source
        self._registry_key_source = registry_key_source
        self._registry_controller = registry_controller
        self._service_source = service_source
        self._service_controller = service_controller
        self._event_log_source = event_log_source
        self._event_log_controller = event_log_controller
        self._cim_source = cim_source
        self._scheduled_task_source = scheduled_task_source
        self._scheduled_task_controller = scheduled_task_controller
        self._local_user_source = local_user_source
        self._local_group_source = local_group_source
        self._local_account_controller = local_account_controller
        self._acl_source = acl_source
        self._acl_controller = acl_controller
        self._ps_runner = ps_runner
        self._cmdlet_runner = cmdlet_runner
        self._command_introspector = command_introspector
        self._process_runner = process_runner
        self._script_packer = script_packer
        self._script_unpacker = script_unpacker
        self._text_writer = text_writer
        self._text_stream_writer = text_stream_writer
        self._record_writer = record_writer
        self._credential_prompter = credential_prompter
        self._credential_store = credential_store
        self._file_system = file_system
        self._web_requester = web_requester
        self._rest_invoker = rest_invoker
        self._file_downloader = file_downloader
        self._process_control = process_control
        self._hotfix_source = hotfix_source
        self._computer_info_source = computer_info_source
        self._elevation_check = elevation_check
        self._elevator = elevator

    def get_process(self) -> Pipeline[ProcessInfo]:
        """Return a lazy pipeline over the running processes (native).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_process(), Pipeline)
            True
        """
        return Pipeline(self._process_source())

    def get_net_tcp_connection(self) -> Pipeline[NetConnection]:
        """Return a lazy pipeline over the open network connections (native).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_net_tcp_connection(), Pipeline)
            True
        """
        return Pipeline(self._connection_source())

    def get_net_adapter(self) -> Pipeline[NetAdapter]:
        """Return a lazy pipeline over the network interfaces (native, portable; like Get-NetAdapter).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_net_adapter(), Pipeline)
            True
        """
        return Pipeline(self._net_adapter_source())

    def get_net_ip_address(self) -> Pipeline[NetIpAddress]:
        """Return a lazy pipeline over the bound IP addresses (native, portable; like Get-NetIPAddress).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_net_ip_address(), Pipeline)
            True
        """
        return Pipeline(self._net_ip_address_source())

    def get_net_udp_endpoint(self) -> Pipeline[NetConnection]:
        """Return a lazy pipeline over the UDP sockets (native, portable; like Get-NetUDPEndpoint).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_net_udp_endpoint(), Pipeline)
            True
        """
        return Pipeline(self._net_udp_endpoint_source())

    def get_volume(self) -> Pipeline[DiskUsage]:
        """Return a lazy pipeline over the mounted filesystems (native).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_volume(), Pipeline)
            True
        """
        return Pipeline(self._disk_source())

    def get_uptime(self) -> SystemUptime:
        """Return the system boot time and elapsed uptime (native).

        Example:
            >>> from pwshpy.domain.records import SystemUptime
            >>> isinstance(build_ps().get_uptime(), SystemUptime)
            True
        """
        return self._uptime_source()

    def resolve_dns_name(self, name: str, *, timeout: float | None = None) -> Pipeline[DnsRecord]:
        """Return a lazy pipeline over the addresses ``name`` resolves to (native).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().resolve_dns_name("localhost"), Pipeline)
            True
        """
        return Pipeline(self._dns_resolver(name, timeout=timeout))

    def test_connection(self, host: str, *, port: int = 443, timeout: float = 5.0) -> ConnectionTest:
        """Probe TCP reachability of ``host:port`` within ``timeout`` seconds (native).

        Example:
            >>> from pwshpy.domain.records import ConnectionTest
            >>> isinstance(build_ps().test_connection("127.0.0.1", port=1), ConnectionTest)
            True
        """
        return self._connection_tester(host, port=port, timeout=timeout)

    def environment(self) -> Pipeline[EnvVar]:
        """Return a lazy pipeline over the process environment variables (native).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().environment(), Pipeline)
            True
        """
        return Pipeline(self._environment_source())

    def get_item_property(self, key: str) -> Pipeline[RegistryValue]:
        """Return a lazy pipeline over the values under registry ``key`` (native, Windows).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_item_property("HKLM/SOFTWARE"), Pipeline)
            True
        """
        return Pipeline(self._registry_value_source(key))

    def registry_keys(self, key: str) -> Pipeline[RegistryKey]:
        """Return a lazy pipeline over the immediate subkeys of registry ``key`` (native, Windows).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().registry_keys("HKLM/SOFTWARE"), Pipeline)
            True
        """
        return Pipeline(self._registry_key_source(key))

    def set_item_property(
        self, key: str, name: str, data: str | int | list[str] | None, value_type: RegistryValueType
    ) -> RegistryValue:
        """Create or overwrite a registry value; return the written record (native, **mutating**).

        Example:
            >>> callable(build_ps().set_item_property)
            True
        """
        return self._registry_controller.set_value(key, name, data, value_type)

    def remove_item_property(self, key: str, name: str) -> None:
        """Delete a registry value (native, **mutating**).

        Example:
            >>> callable(build_ps().remove_item_property)
            True
        """
        self._registry_controller.remove_value(key, name)

    def new_registry_key(self, key: str) -> RegistryKey:
        """Create a registry key (with missing parents); return it (native, **mutating**).

        Example:
            >>> callable(build_ps().new_registry_key)
            True
        """
        return self._registry_controller.create_key(key)

    def remove_registry_key(self, key: str, *, recursive: bool = False) -> None:
        """Delete a registry key (``recursive`` removes subkeys too) (native, **mutating**).

        Example:
            >>> callable(build_ps().remove_registry_key)
            True
        """
        self._registry_controller.remove_key(key, recursive=recursive)

    def get_service(self) -> Pipeline[ServiceInfo]:
        """Return a lazy pipeline over the services (native, portable).

        win32service on Windows, systemd (D-Bus) on Linux - the same ``ServiceInfo`` records.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_service(), Pipeline)
            True
        """
        return Pipeline(self._service_source())

    def start_service(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        """Start a service; return its post-start state (native, portable, **mutating**).

        win32service on Windows, systemd (D-Bus, needs root) on Linux.

        Idempotent - starting an already-running service is a no-op.

        Example:
            >>> callable(build_ps().start_service)
            True
        """
        return self._service_controller.start(name, timeout=timeout)

    def stop_service(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        """Stop a service; return its post-stop state (native, portable, **mutating**).

        win32service on Windows, systemd (D-Bus, needs root) on Linux.

        Idempotent - stopping an already-stopped service is a no-op.

        Example:
            >>> callable(build_ps().stop_service)
            True
        """
        return self._service_controller.stop(name, timeout=timeout)

    def restart_service(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        """Restart a service; return its post-restart state (native, portable, **mutating**; systemd on Linux).

        Example:
            >>> callable(build_ps().restart_service)
            True
        """
        return self._service_controller.restart(name, timeout=timeout)

    def set_service(self, name: str, start_type: ServiceStartType) -> ServiceInfo:
        """Change a service's startup type; return its new config (native, portable, **mutating**).

        win32service on Windows; systemd enable/disable on Linux (Automatic or Disabled only).

        Example:
            >>> callable(build_ps().set_service)
            True
        """
        return self._service_controller.set_startup(name, start_type)

    def get_win_event(self, log_name: str) -> Pipeline[EventLogEntry]:
        """Return a lazy, STREAMING pipeline over the system event log (native, portable).

        win32evtlog on Windows, journald on Linux (``log_name`` is a Windows channel
        like ``"System"``, or a systemd unit like ``"sshd"``). Reads newest-first and
        streams, so ``.take(n)`` stays memory-bounded even on a log with millions of
        entries.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_win_event("System"), Pipeline)
            True
        """
        return Pipeline(self._event_log_source(log_name))

    def clear_event_log(self, log_name: str, *, backup_path: str | None = None) -> None:
        """Clear a Windows event log, optionally backing it up first (native, **mutating**).

        Example:
            >>> callable(build_ps().clear_event_log)
            True
        """
        self._event_log_controller.clear(log_name, backup_path=backup_path)

    def get_cim_instance(
        self, class_name: str, *, where: str | None = None, namespace: str = "root/cimv2"
    ) -> Pipeline[CimInstance]:
        """Return a lazy, STREAMING pipeline over CIM/WMI instances (native, Windows; like Get-CimInstance).

        ``where`` is an optional WQL filter fragment; the stream is forward-only,
        so ``.take(n)`` stays memory-bounded even on an unbounded class.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_cim_instance("Win32_OperatingSystem"), Pipeline)
            True
        """
        return Pipeline(self._cim_source(class_name, where=where, namespace=namespace))

    def get_scheduled_task(self, folder_path: str = "\\") -> Pipeline[ScheduledTaskInfo]:
        """Return a lazy, STREAMING pipeline over scheduled tasks (native, portable).

        Recurses the Task Scheduler folder tree under ``folder_path`` on Windows (like
        Get-ScheduledTask); on Linux lists the systemd ``.timer`` units (``folder_path``
        ignored). Yields one record at a time.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_scheduled_task(), Pipeline)
            True
        """
        return Pipeline(self._scheduled_task_source(folder_path))

    def register_scheduled_task(
        self, task_path: str, *, program: str, arguments: str = "", description: str = ""
    ) -> ScheduledTaskInfo:
        """Register a run-on-demand task (program + args); return it (native, **mutating**).

        Example:
            >>> callable(build_ps().register_scheduled_task)
            True
        """
        return self._scheduled_task_controller.register(
            task_path, program=program, arguments=arguments, description=description
        )

    def unregister_scheduled_task(self, task_path: str) -> None:
        """Delete a scheduled task (native, **mutating**).

        Example:
            >>> callable(build_ps().unregister_scheduled_task)
            True
        """
        self._scheduled_task_controller.unregister(task_path)

    def enable_scheduled_task(self, task_path: str) -> ScheduledTaskInfo:
        """Enable a scheduled task; return its new state (native, **mutating**).

        Example:
            >>> callable(build_ps().enable_scheduled_task)
            True
        """
        return self._scheduled_task_controller.enable(task_path)

    def disable_scheduled_task(self, task_path: str) -> ScheduledTaskInfo:
        """Disable a scheduled task; return its new state (native, **mutating**).

        Example:
            >>> callable(build_ps().disable_scheduled_task)
            True
        """
        return self._scheduled_task_controller.disable(task_path)

    def start_scheduled_task(self, task_path: str) -> None:
        """Start a scheduled task now (native, **mutating**).

        Example:
            >>> callable(build_ps().start_scheduled_task)
            True
        """
        self._scheduled_task_controller.run(task_path)

    def stop_scheduled_task(self, task_path: str) -> None:
        """Stop a running scheduled task (native, **mutating**).

        Example:
            >>> callable(build_ps().stop_scheduled_task)
            True
        """
        self._scheduled_task_controller.stop(task_path)

    def get_local_user(self) -> Pipeline[LocalUser]:
        """Return a lazy pipeline over the local user accounts (native, portable).

        win32net on Windows, stdlib ``pwd`` on Linux/macOS. Records are identified by a
        stable id (SID on Windows, uid on POSIX); filter with e.g.
        ``ps.get_local_user().where(lambda u: not u.enabled)``.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_local_user(), Pipeline)
            True
        """
        return Pipeline(self._local_user_source())

    def get_local_group(self) -> Pipeline[LocalGroup]:
        """Return a lazy pipeline over the local groups (native, portable).

        win32net on Windows, stdlib ``grp`` on Linux/macOS.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_local_group(), Pipeline)
            True
        """
        return Pipeline(self._local_group_source())

    def new_local_user(
        self,
        name: str,
        *,
        password: str = "",
        full_name: str = "",
        description: str = "",
        disabled: bool = False,
        timeout: float = 30.0,
    ) -> LocalUser:
        """Create a local user; return the created record (native, **mutating**).

        On Linux the shadow-utils command is bounded by ``timeout`` seconds.

        Example:
            >>> callable(build_ps().new_local_user)
            True
        """
        return self._local_account_controller.new_user(
            name,
            password=password,
            full_name=full_name,
            description=description,
            disabled=disabled,
            timeout=timeout,
        )

    def remove_local_user(self, name: str, *, timeout: float = 30.0) -> None:
        """Delete a local user (native, **mutating**; Linux bounds the command by ``timeout`` s).

        Example:
            >>> callable(build_ps().remove_local_user)
            True
        """
        self._local_account_controller.remove_user(name, timeout=timeout)

    def enable_local_user(self, name: str, *, timeout: float = 30.0) -> LocalUser:
        """Enable a local user; return its new state (native, **mutating**; Linux bounds by ``timeout`` s).

        Example:
            >>> callable(build_ps().enable_local_user)
            True
        """
        return self._local_account_controller.set_user_enabled(name, enabled=True, timeout=timeout)

    def disable_local_user(self, name: str, *, timeout: float = 30.0) -> LocalUser:
        """Disable a local user; return its new state (native, **mutating**; Linux bounds by ``timeout`` s).

        Example:
            >>> callable(build_ps().disable_local_user)
            True
        """
        return self._local_account_controller.set_user_enabled(name, enabled=False, timeout=timeout)

    def new_local_group(self, name: str, *, description: str = "", timeout: float = 30.0) -> LocalGroup:
        """Create a local group; return the created record (native, **mutating**; Linux bounds by ``timeout`` s).

        Example:
            >>> callable(build_ps().new_local_group)
            True
        """
        return self._local_account_controller.new_group(name, description=description, timeout=timeout)

    def remove_local_group(self, name: str, *, timeout: float = 30.0) -> None:
        """Delete a local group (native, **mutating**; Linux bounds the command by ``timeout`` s).

        Example:
            >>> callable(build_ps().remove_local_group)
            True
        """
        self._local_account_controller.remove_group(name, timeout=timeout)

    def add_local_group_member(self, group: str, member: str, *, timeout: float = 30.0) -> None:
        """Add a user/group to a local group (native, **mutating**; Linux bounds by ``timeout`` s).

        Example:
            >>> callable(build_ps().add_local_group_member)
            True
        """
        self._local_account_controller.add_group_member(group, member, timeout=timeout)

    def remove_local_group_member(self, group: str, member: str, *, timeout: float = 30.0) -> None:
        """Remove a member from a local group (native, **mutating**; Linux bounds by ``timeout`` s).

        Example:
            >>> callable(build_ps().remove_local_group_member)
            True
        """
        self._local_account_controller.remove_group_member(group, member, timeout=timeout)

    def get_acl(self, path: str) -> Pipeline[AclEntry]:
        """Return a lazy pipeline over a filesystem path's ACL entries (native, portable).

        One AclEntry per entry - a Windows DACL ACE, or a POSIX ACL entry (owner / named user /
        owning group / named group / mask / other, in ``kind``). Filter with e.g.
        ``ps.get_acl(path).where(lambda e: e.access_type == AceType.DENY)``.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_acl("C:/Windows"), Pipeline)
            True
        """
        return Pipeline(self._acl_source(path))

    def add_acl_ace(self, path: str, trustee: str, rights: int, *, access_type: AceType = AceType.ALLOW) -> None:
        """Grant a trustee ``rights`` on a path's ACL (native, portable, **mutating**).

        On Windows ``trustee`` is a SID string or name and ``rights`` a Win32 access mask (allow
        or deny). On Linux ``trustee`` is ``user:NAME`` / ``group:NAME`` (bare = user), ``rights``
        uses the low three bits as POSIX ``rwx``, and only allow entries exist (deny is rejected).

        Example:
            >>> callable(build_ps().add_acl_ace)
            True
        """
        self._acl_controller.add_ace(path, trustee, rights, access_type=access_type)

    def remove_acl_ace(self, path: str, trustee: str, *, access_type: AceType | None = None) -> None:
        """Remove a trustee's entries from a path's ACL (native, portable, **mutating**).

        Example:
            >>> callable(build_ps().remove_acl_ace)
            True
        """
        self._acl_controller.remove_ace(path, trustee, access_type=access_type)

    def set_owner(self, path: str, owner: str) -> None:
        """Set the owner of a filesystem path (native, portable, **mutating**; ``chown`` on POSIX).

        Example:
            >>> callable(build_ps().set_owner)
            True
        """
        self._acl_controller.set_owner(path, owner)

    def run(self, script: str, *, timeout: float | None = None) -> list[Any]:
        """Execute a PowerShell script in the hosted engine in-process (.NET).

        Returns one marshaled object per output item - a wrapped primitive as its
        plain Python value, a structured object as a
        :class:`~pwshpy.domain.records.PSObjectRecord`.  Raises
        :class:`~pwshpy.domain.errors.FeatureUnavailableError` when the ``[full]``
        extra or the .NET 10 / PowerShell 7.6 host is absent, and
        :class:`~pwshpy.domain.errors.PowerShellError` on a script error or timeout.

        Example:
            >>> build_ps().run("$PSVersionTable.PSVersion.Major")  # doctest: +SKIP
            [7]
        """
        return self._ps_runner(script, timeout=timeout)

    def cmdlet(self, name: str, *args: Any, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Run a single cmdlet with **safely bound** parameters in-process (.NET).

        Parameters are bound as discrete values (never string-interpolated into a
        script), so a value can never break out into executable PowerShell - use this,
        not :meth:`run` with an f-string, whenever a parameter comes from data.  Pass
        PowerShell parameter names as keywords (``ps.cmdlet("Get-ChildItem", Path="C:/",
        Recurse=True)``).  Returns the full :class:`~pwshpy.domain.records.PSInvocationResult`
        (output plus the error/warning/verbose/debug/information streams); unlike
        :meth:`run`, it does NOT raise on the error stream.

        Example:
            >>> callable(build_ps().cmdlet)
            True
        """
        return self._cmdlet_runner(name, *args, timeout=timeout, **params)

    def get_command(self, name: str) -> CommandInfo:
        """Introspect a command's parameter metadata as typed data (.NET).

        Answers "what parameters does this cmdlet take, and which are mandatory?"
        from the SDK's ``CommandInfo`` - real discoverability instead of guessing at
        PowerShell's inconsistent cmdlet surface.  Pairs with :meth:`cmdlet`: read the
        parameter names here, bind them safely there.

        Example:
            >>> callable(build_ps().get_command)
            True
        """
        return self._command_introspector(name)

    # --- Module-cmdlet convenience wrappers (.NET) -------------------------------------------
    # ActiveDirectory / Exchange / Azure cmdlets have no native binding - they live in
    # their PowerShell modules. These thin wrappers delegate to :meth:`cmdlet` with SAFE parameter
    # binding, so they need the ``[full]`` extra AND the respective module installed; without
    # ``[full]`` they raise ``FeatureUnavailableError`` (same guard as every .NET call). Any other
    # module cmdlet is reachable directly with ``ps.cmdlet("Verb-Noun", **params)``.

    def get_ad_user(self, *, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Query Active Directory users (.NET; Get-ADUser, needs [full] + the ActiveDirectory module).

        Example:
            >>> callable(build_ps().get_ad_user)
            True
        """
        return self.cmdlet("Get-ADUser", timeout=timeout, **params)

    def get_ad_group(self, *, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Query Active Directory groups (.NET; Get-ADGroup, needs [full] + ActiveDirectory).

        Example:
            >>> callable(build_ps().get_ad_group)
            True
        """
        return self.cmdlet("Get-ADGroup", timeout=timeout, **params)

    def get_ad_computer(self, *, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Query Active Directory computers (.NET; Get-ADComputer, needs [full] + ActiveDirectory).

        Example:
            >>> callable(build_ps().get_ad_computer)
            True
        """
        return self.cmdlet("Get-ADComputer", timeout=timeout, **params)

    def get_mailbox(self, *, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Query Exchange mailboxes (.NET; Get-Mailbox, needs [full] + the Exchange module).

        Example:
            >>> callable(build_ps().get_mailbox)
            True
        """
        return self.cmdlet("Get-Mailbox", timeout=timeout, **params)

    def get_az_vm(self, *, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Query Azure virtual machines (.NET; Get-AzVM, needs [full] + the Az.Compute module).

        Example:
            >>> callable(build_ps().get_az_vm)
            True
        """
        return self.cmdlet("Get-AzVM", timeout=timeout, **params)

    def get_az_resource_group(self, *, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Query Azure resource groups (.NET; Get-AzResourceGroup, needs [full] + Az.Resources).

        Example:
            >>> callable(build_ps().get_az_resource_group)
            True
        """
        return self.cmdlet("Get-AzResourceGroup", timeout=timeout, **params)

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> ProcessResult:
        """Run an external program from an argv list; return a typed result (native, portable).

        ``subprocess`` done right - no shell, so no quoting/``--%`` hell; the
        ``exit_code`` is ALWAYS set (unlike ``$LASTEXITCODE`` in a pipeline); and
        ``stderr`` is plain data, never mistaken for a terminating error.  Call
        ``.check()`` on the result to raise on a nonzero exit.

        Example:
            >>> import sys
            >>> build_ps().exec([sys.executable, "-c", "print(1)"]).stdout.strip()
            '1'
        """
        return self._process_runner(argv, cwd=cwd, timeout=timeout, env=env, input_text=input_text)

    def pack_script(
        self, entry: str | Path, dest: str | Path | None = None, *, options: PackOptions = DEFAULT_PACK_OPTIONS
    ) -> PackedScript:
        """Pack a Python script and its local modules into a self-extracting ``.ps1`` (portable).

        The artefact is one file: it unpacks itself into a per-user cache, provisions ``uv``
        if the machine has none, runs the script, and exits with the script's own exit code.
        Third-party dependencies come from the entry's PEP 723 block or ``options.with_packages`` -
        never guessed from an import name.  Runs on Windows PowerShell 5.1 and pwsh 7 alike.

        Example:
            >>> callable(build_ps().pack_script)
            True
        """
        return self._script_packer(entry, dest, options=options)

    def unpack_script(self, source: str | Path, dest: str | Path, *, force: bool = False) -> PackedScript:
        """Restore the sources a packed ``.ps1`` carries, ready to edit and pack again (portable).

        The inverse of :meth:`pack_script`.  The generated ``__main__`` shim is machinery, not
        source, so it is left out of the restored tree.

        Example:
            >>> callable(build_ps().unpack_script)
            True
        """
        return self._script_unpacker(source, dest, force=force)

    def write_text(
        self, path: str | Path, text: str, *, encoding: str = "utf-8", newline: str = "\n", bom: bool = False
    ) -> Path:
        """Write text to a file with a predictable encoding/BOM/newline (native, portable).

        Default is UTF-8, **no BOM**, LF newlines - the same bytes on every OS and
        Python version, unlike PowerShell's version- and host-dependent ``Out-File``.

        Example:
            >>> callable(build_ps().write_text)
            True
        """
        return self._text_writer(path, text, encoding=encoding, newline=newline, bom=bom)

    def write_text_stream(
        self,
        path: str | Path,
        chunks: Iterable[str],
        *,
        encoding: str = "utf-8",
        newline: str = "\n",
        bom: bool = False,
    ) -> Path:
        """Stream text chunks to a file with the same predictable encoding as :meth:`write_text`.

        **Memory-bounded** (writes one chunk at a time, never joining), so piping a huge
        or unbounded source - e.g. ``ps.write_text_stream(path, sys.stdin)`` - stays flat
        in memory, the same discipline as :meth:`write_records`.

        Example:
            >>> callable(build_ps().write_text_stream)
            True
        """
        return self._text_stream_writer(path, chunks, encoding=encoding, newline=newline, bom=bom)

    def write_records(self, path: str | Path, records: Iterable[PSRecord], *, jsonl: bool = True) -> Path:
        """Write typed records to a file as UTF-8 JSON/JSONL, never a BOM (native, portable).

        Pairs with any pipeline: ``ps.write_records("procs.jsonl", ps.get_process())``.

        Example:
            >>> callable(build_ps().write_records)
            True
        """
        return self._record_writer(path, records, jsonl=jsonl)

    def get_child_item(self, path: str, *, recurse: bool = False) -> Pipeline[FileSystemItem]:
        """List a directory's entries as typed records (native, portable; like Get-ChildItem).

        ``recurse`` walks the whole tree, lazily.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_child_item("."), Pipeline)
            True
        """
        return Pipeline(self._file_system.get_child_item(path, recurse=recurse))

    def get_item(self, path: str) -> FileSystemItem:
        """Return one file/directory as a typed record (native, portable; like Get-Item).

        Example:
            >>> callable(build_ps().get_item)
            True
        """
        return self._file_system.get_item(path)

    def get_content(self, path: str, *, encoding: str = "utf-8") -> str:
        """Return a file's whole text content (native, portable; like ``Get-Content -Raw``).

        Materializes the file; for a large/unbounded file use :meth:`get_content_lines`.

        Example:
            >>> callable(build_ps().get_content)
            True
        """
        return self._file_system.get_content(path, encoding=encoding)

    def get_content_lines(self, path: str, *, encoding: str = "utf-8") -> Pipeline[str]:
        """Return a lazy, **memory-bounded** pipeline over a file's lines (like ``Get-Content``).

        Streams one line at a time, so ``ps.get_content_lines(huge).take(10)`` reads only
        the first lines - the same laziness as the record pipelines.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_content_lines(__file__), Pipeline)
            True
        """
        return Pipeline(self._file_system.get_content_lines(path, encoding=encoding))

    def test_path(self, path: str) -> bool:
        """Return whether ``path`` exists (native, portable; like Test-Path).

        Example:
            >>> isinstance(build_ps().test_path("."), bool)
            True
        """
        return self._file_system.test_path(path)

    def new_item(self, path: str, *, item_type: FileItemType = FileItemType.FILE) -> FileSystemItem:
        """Create a file or directory (per ``item_type``); return it (native, **mutating**).

        Example:
            >>> callable(build_ps().new_item)
            True
        """
        return self._file_system.new_item(path, item_type=item_type)

    def copy_item(self, source: str, destination: str, *, recurse: bool = False) -> None:
        """Copy a file, or a directory tree when ``recurse`` (native, **mutating**; like Copy-Item).

        Example:
            >>> callable(build_ps().copy_item)
            True
        """
        self._file_system.copy_item(source, destination, recurse=recurse)

    def move_item(self, source: str, destination: str) -> None:
        """Move or rename a file/directory (native, **mutating**; like Move-Item).

        Example:
            >>> callable(build_ps().move_item)
            True
        """
        self._file_system.move_item(source, destination)

    def remove_item(self, path: str, *, recurse: bool = False) -> None:
        """Delete a file, or a directory (``recurse`` for its contents) (native, **mutating**).

        Example:
            >>> callable(build_ps().remove_item)
            True
        """
        self._file_system.remove_item(path, recurse=recurse)

    def invoke_web_request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: str | None = None,
        timeout: float = 30.0,
    ) -> WebResponse:
        """Perform an HTTP request; return a typed WebResponse (native, portable; like Invoke-WebRequest).

        Example:
            >>> callable(build_ps().invoke_web_request)
            True
        """
        return self._web_requester(url, method=method, headers=headers, body=body, timeout=timeout)

    def invoke_rest_method(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        timeout: float = 30.0,
    ) -> Any:
        """Perform an HTTP request; return the parsed JSON (native, portable; like Invoke-RestMethod).

        Example:
            >>> callable(build_ps().invoke_rest_method)
            True
        """
        return self._rest_invoker(url, method=method, headers=headers, json_body=json_body, timeout=timeout)

    def download_file(self, url: str, dest: str | Path, *, chunk_size: int = 65536, timeout: float = 30.0) -> Path:
        """Stream an HTTP download to ``dest``, **memory-bounded** (native, portable).

        Unlike :meth:`invoke_web_request` (which returns the body as data), this copies the
        response to a file in fixed-size chunks, so a huge download never buffers in memory.

        Example:
            >>> callable(build_ps().download_file)
            True
        """
        return self._file_downloader(url, dest, chunk_size=chunk_size, timeout=timeout)

    def stop_process(self, pid: int, *, force: bool = False) -> None:
        """Terminate a process by PID (``force`` hard-kills) (native, portable, **mutating**; like Stop-Process).

        Example:
            >>> callable(build_ps().stop_process)
            True
        """
        self._process_control.stop_process(pid, force=force)

    def wait_process(self, pid: int, *, timeout: float | None = None) -> int | None:
        """Wait for a process to exit; return its exit code (native, portable; like Wait-Process).

        Example:
            >>> callable(build_ps().wait_process)
            True
        """
        return self._process_control.wait_process(pid, timeout=timeout)

    def restart_computer(self, *, delay_seconds: int = 0, force: bool = True) -> None:
        """Restart the computer (native, Windows, **destructive**; like Restart-Computer).

        Example:
            >>> callable(build_ps().restart_computer)
            True
        """
        self._process_control.restart_computer(delay_seconds=delay_seconds, force=force)

    def stop_computer(self, *, delay_seconds: int = 0, force: bool = True) -> None:
        """Shut the computer down (native, Windows, **destructive**; like Stop-Computer).

        Example:
            >>> callable(build_ps().stop_computer)
            True
        """
        self._process_control.stop_computer(delay_seconds=delay_seconds, force=force)

    def get_hotfix(self) -> Pipeline[Hotfix]:
        """Return a lazy pipeline over installed Windows updates (native, Windows; like Get-Hotfix).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_hotfix(), Pipeline)
            True
        """
        return Pipeline(self._hotfix_source())

    def get_computer_info(self) -> ComputerInfo:
        """Return a summary of the local computer (native, portable; like Get-ComputerInfo).

        Example:
            >>> from pwshpy.domain.records import ComputerInfo
            >>> isinstance(build_ps().get_computer_info(), ComputerInfo)
            True
        """
        return self._computer_info_source()

    def get_credential(
        self, username: str | None = None, *, prompt: str = "Password: ", target: str = ""
    ) -> Credential:
        """Interactively read a credential without echoing the password (native, portable).

        The typed equivalent of ``Get-Credential``; the returned
        :class:`~pwshpy.domain.records.Credential` masks its secret in repr/JSON
        (retrieve it with ``.secret.get_secret_value()``).

        Example:
            >>> callable(build_ps().get_credential)
            True
        """
        return self._credential_prompter(username, prompt=prompt, target=target)

    def save_credential(self, target: str, username: str, secret: str) -> Credential:
        """Store a credential in the OS credential vault (native, portable, **mutating**).

        Windows Credential Manager on Windows, the desktop keyring (Secret Service) on
        Linux. The unattended-friendly alternative to a DPAPI file (which will not
        decrypt under another account).

        Example:
            >>> callable(build_ps().save_credential)
            True
        """
        return self._credential_store.save(target, username, secret)

    def load_credential(self, target: str) -> Credential | None:
        """Read a stored credential by target; ``None`` if absent (native, portable).

        Example:
            >>> callable(build_ps().load_credential)
            True
        """
        return self._credential_store.load(target)

    def delete_credential(self, target: str) -> None:
        """Delete a stored credential by target (native, portable, **mutating**).

        Example:
            >>> callable(build_ps().delete_credential)
            True
        """
        self._credential_store.delete(target)

    def is_elevated(self) -> bool:
        """Return whether this process has administrative (elevated) rights (native, portable).

        Cross-platform: Windows token elevation, or POSIX ``euid == 0`` - none of
        PowerShell's ``WindowsPrincipal``/``IsInRole`` boilerplate.

        Example:
            >>> isinstance(build_ps().is_elevated(), bool)
            True
        """
        return self._elevation_check()

    def require_elevation(self) -> None:
        """Raise :class:`ElevationRequiredError` unless this process is elevated (native).

        The honest, catchable equivalent of ``#Requires -RunAsAdministrator`` -
        call it at the top of an operation that needs admin rights.

        Example:
            >>> callable(build_ps().require_elevation)
            True
        """
        if not self._elevation_check():
            raise ElevationRequiredError(
                "This operation requires administrative privileges. Relaunch elevated (ps.elevate())."
            )

    def elevate(
        self,
        argv: Sequence[str] | None = None,
        *,
        executable: str | None = None,
        cwd: str | None = None,
        wait: bool = True,
    ) -> int | None:
        """Relaunch the current process elevated, forwarding argv and cwd (native, portable).

        A no-op returning ``0`` when already elevated. Windows uses UAC (and, unlike
        PowerShell's ``Start-Process -Verb RunAs``, the elevated child keeps the current
        working directory - no ``System32`` surprise); POSIX (Linux/macOS) re-execs the
        command under ``sudo``. ``argv`` is forwarded in both cases.

        Example:
            >>> callable(build_ps().elevate)
            True
        """
        return self._elevator(argv, executable=executable, cwd=cwd, wait=wait)


def build_ps() -> Ps:
    """Construct a facade wired with the production adapters.

    Example:
        >>> isinstance(build_ps(), Ps)
        True
    """
    return Ps(
        process_source=_iter_processes,
        connection_source=_iter_connections,
        net_adapter_source=_iter_net_adapters,
        net_ip_address_source=_iter_net_ip_addresses,
        net_udp_endpoint_source=_iter_net_udp_endpoints,
        disk_source=_iter_disks,
        uptime_source=_get_uptime,
        dns_resolver=_resolve,
        connection_tester=_test_connection,
        environment_source=_iter_env,
        registry_value_source=_iter_registry_values,
        registry_key_source=_iter_registry_keys,
        registry_controller=_NativeRegistryController(),
        service_source=_iter_services if _ON_WINDOWS else _systemd_iter_services,
        service_controller=_NativeServiceController() if _ON_WINDOWS else _SystemdServiceController(),
        event_log_controller=_NativeEventLogController(),
        # Local accounts are portable: win32net on Windows, shadow-utils (useradd/...) on Linux.
        local_account_controller=(_NativeLocalAccountController() if _ON_WINDOWS else _PosixLocalAccountController()),
        # ACLs are portable: win32security DACLs on Windows, POSIX ACLs (xattr + chown) on Linux.
        acl_controller=_NativeAclController() if _ON_WINDOWS else _PosixAclController(),
        # Scheduled tasks are portable: Task Scheduler on Windows, systemd timers on Linux (macOS raises).
        scheduled_task_controller=(
            _NativeScheduledTaskController() if _ON_WINDOWS else _SystemdScheduledTaskController()
        ),
        # Event log is portable: win32evtlog on Windows, journald on Linux (macOS raises).
        event_log_source=_iter_event_log if _ON_WINDOWS else _journald_iter_event_log,
        cim_source=_iter_cim,
        scheduled_task_source=_iter_scheduled_tasks if _ON_WINDOWS else _systemd_iter_timers,
        # Local accounts are portable: win32net on Windows, stdlib pwd/grp on POSIX (Linux/macOS).
        local_user_source=_iter_local_users if _ON_WINDOWS else _posix_iter_local_users,
        local_group_source=_iter_local_groups if _ON_WINDOWS else _posix_iter_local_groups,
        acl_source=_iter_acl if _ON_WINDOWS else _posix_iter_acl,
        ps_runner=_ps_run,
        cmdlet_runner=_ps_invoke,
        command_introspector=_ps_get_command,
        process_runner=_run_process,
        text_writer=_write_text,
        text_stream_writer=_write_text_stream,
        record_writer=_write_records,
        credential_prompter=_prompt_credential,
        # Credential store is portable: Windows Credential Manager on Windows, the freedesktop
        # Secret Service (desktop keyring) on Linux (macOS has no Secret Service -> clear error).
        credential_store=_NativeCredentialStore() if _ON_WINDOWS else _SecretServiceCredentialStore(),
        file_system=_NativeFileSystem(),
        web_requester=_invoke_web_request,
        rest_invoker=_invoke_rest_method,
        file_downloader=_download_file,
        process_control=_NativeProcessControl(),
        hotfix_source=_iter_hotfixes,
        computer_info_source=_get_computer_info,
        elevation_check=_is_elevated,
        elevator=_elevate,
        script_packer=_pack_script,
        script_unpacker=_unpack_script,
    )


#: Default production facade for ergonomic ``from pwshpy import ps`` usage.
ps = build_ps()


__all__ = ["Ps", "build_ps", "ps"]
