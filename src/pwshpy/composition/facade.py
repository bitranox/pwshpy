"""The ``ps`` facade - the public entry point wiring adapters to the pipeline.

``ps`` is where library and CLI meet: both call the same facade, so they never
diverge.  native sources return a :class:`~pwshpy.domain.pipeline.Pipeline`;
``ps.run`` reaches .NET (hosts PowerShell in-process) behind the ``[full]`` extra.

Contents:
    * :class:`Ps` - the facade binding a process source + PowerShell runner.
    * :data:`ps` - the default production facade (``from pwshpy import ps``).
    * :func:`build_ps` - construct a facade from explicit adapters (for tests).
"""

from __future__ import annotations

import ntpath
import os
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
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
from ..adapters.native import NativeShortcutController as _NativeShortcutController
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
from ..adapters.native import iter_wim_images as _iter_wim_images
from ..adapters.native import journald_iter_event_log as _journald_iter_event_log
from ..adapters.native import pack_script as _pack_script
from ..adapters.native import posix_current_identity as _posix_current_identity
from ..adapters.native import posix_iter_acl as _posix_iter_acl
from ..adapters.native import posix_iter_local_groups as _posix_iter_local_groups
from ..adapters.native import posix_iter_local_users as _posix_iter_local_users
from ..adapters.native import posix_known_folder as _posix_known_folder
from ..adapters.native import prompt_credential as _prompt_credential
from ..adapters.native import resolve as _resolve
from ..adapters.native import run_process as _run_process
from ..adapters.native import search_updates as _search_updates
from ..adapters.native import systemd_iter_services as _systemd_iter_services
from ..adapters.native import systemd_iter_timers as _systemd_iter_timers
from ..adapters.native import test_connection as _test_connection
from ..adapters.native import unpack_script as _unpack_script
from ..adapters.native import windows_current_identity as _windows_current_identity
from ..adapters.native import windows_known_folder as _windows_known_folder
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
    CurrentIdentitySource,
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
    KnownFolderSource,
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
    ShortcutController,
    TextStreamWriter,
    TextWriter,
    UptimeSource,
    WebRequester,
    WindowsImageSource,
    WindowsUpdateSource,
)
from ..domain.enums import AceType, FileItemType, KnownFolder, RegistryValueType, ServiceStartType
from ..domain.errors import ElevationRequiredError, NativeCallError, PlatformUnsupportedError
from ..domain.packing import DEFAULT_PACK_OPTIONS, PackOptions
from ..domain.pipeline import Pipeline
from ..domain.records import (
    AclEntry,
    CimInstance,
    CommandInfo,
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
    PSRecord,
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
from ..domain.scheduling import TaskPrincipal, TaskSettings, TaskTrigger
from ..domain.shortcuts import ShortcutSpec

#: Platform switch for the local-accounts backend. Stored as an opaque bool so the type
#: checker does not statically prune the "other OS" branch (and flag its import unused).
_ON_WINDOWS: bool = sys.platform == "win32"

#: Windows Update's own default query: software updates that are not installed yet.
_DEFAULT_WU_CRITERIA = "IsInstalled=0 and Type='Software'"

#: The longest timeout accepted, 2**31-1 ms.  On Windows the child is awaited with a DWORD
#: millisecond wait: 0xFFFFFFFF ms means INFINITE and more raises OverflowError mid-call, and
#: Windows socket timeouts top out at 2**31-1 ms, so the signed ceiling is safe for any wait
#: the timeout may reach.  Refused at the call instead of failing (or never firing) later.
_MAX_WAIT_SECONDS = 2147483


def _check_wait_limit(timeout: float) -> None:
    """Refuse a timeout outside ``(0, _MAX_WAIT_SECONDS]``; NaN fails both comparisons, so it is refused too.

    Raises:
        ValueError: ``timeout`` is not a positive number of seconds within the waitable range.

    Example:
        >>> _check_wait_limit(1.5)
        >>> _check_wait_limit(-1)
        Traceback (most recent call last):
        ...
        ValueError: timeout must be in (0, 2147483] seconds, got -1
    """
    if not 0 < timeout <= _MAX_WAIT_SECONDS:
        raise ValueError(f"timeout must be in (0, {_MAX_WAIT_SECONDS}] seconds, got {timeout!r}")


def _validated_settings(settings: TaskSettings | None) -> TaskSettings | None:
    """Re-run :class:`TaskSettings` validation on a caller's settings before anything is written.

    A record's read-back ``settings`` is built with ``model_construct`` (validation skipped), so a
    copy-a-task flow could hand ``register`` a value no ``TaskSettings(...)`` call would accept: a
    fractional or above-cap limit from a hand-written unit or task XML. Validating a dump of it
    raises the same ``ValidationError`` the constructor would.

    Raises:
        pydantic.ValidationError: A field breaks a :class:`TaskSettings` rule.

    Example:
        >>> from datetime import timedelta
        >>> _validated_settings(None) is None
        True
        >>> from pydantic import ValidationError
        >>> try:
        ...     _validated_settings(TaskSettings.model_construct(execution_time_limit=timedelta(seconds=0.5)))
        ... except ValidationError as exc:
        ...     print(exc.errors()[0]["msg"])
        Value error, execution_time_limit must be whole seconds
    """
    if settings is None:
        return None
    return TaskSettings.model_validate(settings.model_dump())


def _only_image(images: Iterable[WindowsImage], index: int, path: Path) -> Iterator[WindowsImage]:
    """Yield the image numbered ``index``, then stop; raise when the file holds none.

    WIM indices are unique, so reading past the match could only surface damage in
    images the caller did not ask for; the source is therefore not advanced again.

    Args:
        images: The file's images, in file order.
        index: The 1-based image index to keep.
        path: The file, named in the error.

    Yields:
        The one matching image.

    Raises:
        NativeCallError: No image of the file carries ``index``.

    Example:
        >>> [i.name for i in _only_image([WindowsImage(index=1, name="Pro")], 1, Path("x.wim"))]
        ['Pro']
    """
    for image in images:
        if image.index == index:
            yield image
            return
    raise NativeCallError(f"{path}: no image with index {index}")


def _absolute_windows_path(path: str | os.PathLike[str]) -> str:
    r"""Return ``path`` made absolute and normalised the way Windows resolves it.

    :func:`ntpath.abspath` is :func:`os.path.abspath` on Windows, the only OS that reaches a
    shortcut controller in production; naming it keeps the Windows rules (drive letters,
    backslashes, ``..``) when a test drives the facade on another OS.

    Example:
        >>> _absolute_windows_path("C:/Users/me/../Public/Tool.lnk")
        'C:\\Users\\Public\\Tool.lnk'
    """
    return ntpath.abspath(os.fspath(path))


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
        windows_image_source: WindowsImageSource,
        windows_update_source: WindowsUpdateSource,
        computer_info_source: ComputerInfoSource,
        current_identity_source: CurrentIdentitySource,
        known_folder_source: KnownFolderSource,
        shortcut_controller: ShortcutController | None,
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
        self._windows_image_source = windows_image_source
        self._windows_update_source = windows_update_source
        self._computer_info_source = computer_info_source
        self._current_identity_source = current_identity_source
        self._known_folder_source = known_folder_source
        # None where the OS has no shortcuts (anything but Windows): both verbs refuse up front.
        self._shortcut_controller = shortcut_controller
        self._elevation_check = elevation_check
        self._elevator = elevator

    def get_process(self) -> Pipeline[ProcessInfo]:
        """Return a lazy pipeline over the running processes (native, portable; like Get-Process).

        psutil on every OS. A name is the one psutil reports, which differs from
        ``Get-Process`` for two Windows pseudo-processes: ``MemCompression`` (``Get-Process``:
        ``Memory Compression``) and PID 0, ``System Idle Process`` (``Get-Process``: ``Idle``).
        Match a process by ``pid`` when it matters.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`ProcessInfo` records.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_process(), Pipeline)
            True
        """
        return Pipeline(self._process_source())

    def get_net_tcp_connection(self) -> Pipeline[NetConnection]:
        """Return a lazy pipeline over the open TCP connections (native; like Get-NetTCPConnection).

        TCP only, IPv4 and IPv6 (psutil's ``tcp`` listing); UDP sockets are on
        :meth:`get_net_udp_endpoint`. A socket owned by another user can carry ``pid=None``.
        Where listing needs elevation (macOS non-root) the pipeline is empty rather than an error.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`NetConnection` records.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_net_tcp_connection(), Pipeline)
            True
        """
        return Pipeline(self._connection_source())

    def get_net_adapter(self) -> Pipeline[NetAdapter]:
        """Return a lazy pipeline over the network interfaces (native, portable; like Get-NetAdapter).

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`NetAdapter` records, one
            per interface psutil reports (``speed_mbps`` is None where psutil reports 0).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_net_adapter(), Pipeline)
            True
        """
        return Pipeline(self._net_adapter_source())

    def get_net_ip_address(self) -> Pipeline[NetIpAddress]:
        """Return a lazy pipeline over the bound IP addresses (native, portable; like Get-NetIPAddress).

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`NetIpAddress` records,
            IPv4 and IPv6 only (link-layer addresses are on :meth:`get_net_adapter`).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_net_ip_address(), Pipeline)
            True
        """
        return Pipeline(self._net_ip_address_source())

    def get_net_udp_endpoint(self) -> Pipeline[NetConnection]:
        """Return a lazy pipeline over the UDP sockets (native, portable; like Get-NetUDPEndpoint).

        UDP is connectionless, so ``status`` is always ``NONE`` and the remote fields are None.
        Where listing needs elevation (macOS non-root) the pipeline is empty rather than an error.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`NetConnection` records.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_net_udp_endpoint(), Pipeline)
            True
        """
        return Pipeline(self._net_udp_endpoint_source())

    def get_volume(self) -> Pipeline[DiskUsage]:
        """Return a lazy pipeline over the mounted filesystems (native).

        A partition whose usage cannot be read (an empty removable drive, a permission error)
        is skipped rather than ending the listing.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`DiskUsage` records.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_volume(), Pipeline)
            True
        """
        return Pipeline(self._disk_source())

    def get_uptime(self) -> SystemUptime:
        """Return the system boot time and elapsed uptime (native).

        Returns:
            A :class:`SystemUptime` with ``boot_time`` (timezone-aware UTC) and
            ``uptime_seconds`` (never negative).

        Example:
            >>> from pwshpy.domain.records import SystemUptime
            >>> isinstance(build_ps().get_uptime(), SystemUptime)
            True
        """
        return self._uptime_source()

    def resolve_dns_name(self, name: str, *, timeout: float | None = None) -> Pipeline[DnsRecord]:
        """Return a lazy pipeline over the addresses ``name`` resolves to (native).

        The lookup runs when the pipeline is first iterated, not by this call.

        Args:
            name: The host name to resolve.
            timeout: Seconds to wait for the lookup; None (default) leaves it to the OS
                resolver's own timeout. A timed-out lookup's thread is abandoned and ends when
                the OS resolver returns.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`DnsRecord` records, one
            per unique (family, address) pair.

        Raises:
            OperationTimeoutError: ``timeout`` elapsed first (raised on iteration). A
                ``NativeCallError`` subclass, so catch it first to tell it apart.
            NativeCallError: The name could not be resolved (raised on iteration).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().resolve_dns_name("localhost"), Pipeline)
            True
        """
        return Pipeline(self._dns_resolver(name, timeout=timeout))

    def test_connection(self, host: str, *, port: int = 443, timeout: float = 5.0) -> ConnectionTest:
        """Probe TCP reachability of ``host:port`` within ``timeout`` seconds (native).

        A failed connect is a result, not an error: the record says ``reachable=False`` and
        carries the error text.

        Args:
            host: The host name or address to connect to.
            port: The TCP port (default 443).
            timeout: Seconds to wait for the connection (default 5.0; a negative value is
                treated as 0).

        Returns:
            A :class:`ConnectionTest` with ``reachable``, ``latency_ms`` (None when
            unreachable) and ``error`` (None when reachable).

        Example:
            >>> from pwshpy.domain.records import ConnectionTest
            >>> isinstance(build_ps().test_connection("127.0.0.1", port=1), ConnectionTest)
            True
        """
        return self._connection_tester(host, port=port, timeout=timeout)

    def environment(self) -> Pipeline[EnvVar]:
        """Return a lazy pipeline over the process environment variables (native).

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`EnvVar` records, read
            from ``os.environ`` when iterated.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().environment(), Pipeline)
            True
        """
        return Pipeline(self._environment_source())

    def get_item_property(self, key: str) -> Pipeline[RegistryValue]:
        """Return a lazy pipeline over the values under registry ``key`` (native, Windows).

        Nothing is read until the pipeline is iterated, so every error below is raised then.

        Args:
            key: A hive-prefixed key path (``"HKLM/SOFTWARE/..."``); ``/`` and ``\\``
                separators and short or long hive names are accepted.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`RegistryValue` records.

        Raises:
            PlatformUnsupportedError: ``lib_registry`` is not installed (it is a Windows-only
                dependency).
            NativeCallError: The hive is unknown, or the registry call failed (a
                :class:`NativeAccessDeniedError` when access is denied).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_item_property("HKLM/SOFTWARE"), Pipeline)
            True
        """
        return Pipeline(self._registry_value_source(key))

    def registry_keys(self, key: str) -> Pipeline[RegistryKey]:
        """Return a lazy pipeline over the immediate subkeys of registry ``key`` (native, Windows).

        Nothing is read until the pipeline is iterated, so every error below is raised then.

        Args:
            key: A hive-prefixed key path (``"HKLM/SOFTWARE/..."``); ``/`` and ``\\``
                separators and short or long hive names are accepted.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`RegistryKey` records.

        Raises:
            PlatformUnsupportedError: ``lib_registry`` is not installed (it is a Windows-only
                dependency).
            NativeCallError: The hive is unknown, or the registry call failed (a
                :class:`NativeAccessDeniedError` when access is denied).

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

        Args:
            key: A hive-prefixed key path (``"HKLM/SOFTWARE/..."``); ``/`` and ``\\``
                separators and short or long hive names are accepted.
            name: The value name.
            data: The data to store, shaped for ``value_type`` (a ``str``, an ``int``, a
                ``list[str]`` for a multi-string, or None).
            value_type: The registry type to write the value as.

        Returns:
            A :class:`RegistryValue` built from the arguments (not read back from the registry).

        Raises:
            PlatformUnsupportedError: ``lib_registry`` is not installed (it is a Windows-only
                dependency).
            NativeCallError: The hive is unknown, or the registry call failed (a
                :class:`NativeAccessDeniedError` when access is denied).

        Example:
            >>> callable(build_ps().set_item_property)
            True
        """
        return self._registry_controller.set_value(key, name, data, value_type)

    def remove_item_property(self, key: str, name: str) -> None:
        """Delete a registry value (native, **mutating**).

        Args:
            key: A hive-prefixed key path (``"HKLM/SOFTWARE/..."``); ``/`` and ``\\``
                separators and short or long hive names are accepted.
            name: The value name to delete.

        Raises:
            PlatformUnsupportedError: ``lib_registry`` is not installed (it is a Windows-only
                dependency).
            NativeCallError: The hive is unknown, or the registry call failed (a
                :class:`NativeAccessDeniedError` when access is denied).

        Example:
            >>> callable(build_ps().remove_item_property)
            True
        """
        self._registry_controller.remove_value(key, name)

    def new_registry_key(self, key: str) -> RegistryKey:
        """Create a registry key (with missing parents); return it (native, **mutating**).

        Args:
            key: A hive-prefixed key path (``"HKLM/SOFTWARE/..."``); ``/`` and ``\\``
                separators and short or long hive names are accepted.

        Returns:
            A :class:`RegistryKey` naming the created key (built from ``key``, not read back).

        Raises:
            PlatformUnsupportedError: ``lib_registry`` is not installed (it is a Windows-only
                dependency).
            NativeCallError: The hive is unknown, or the registry call failed (a
                :class:`NativeAccessDeniedError` when access is denied).

        Example:
            >>> callable(build_ps().new_registry_key)
            True
        """
        return self._registry_controller.create_key(key)

    def remove_registry_key(self, key: str, *, recursive: bool = False) -> None:
        """Delete a registry key (``recursive`` removes subkeys too) (native, **mutating**).

        Args:
            key: A hive-prefixed key path (``"HKLM/SOFTWARE/..."``); ``/`` and ``\\``
                separators and short or long hive names are accepted.
            recursive: Also delete the key's subkeys; False (default) deletes the key only.

        Raises:
            PlatformUnsupportedError: ``lib_registry`` is not installed (it is a Windows-only
                dependency).
            NativeCallError: The hive is unknown, or the registry call failed (a
                :class:`NativeAccessDeniedError` when access is denied).

        Example:
            >>> callable(build_ps().remove_registry_key)
            True
        """
        self._registry_controller.remove_key(key, recursive=recursive)

    def get_service(self, *, strict: bool = False) -> Pipeline[ServiceInfo]:
        """Return a lazy pipeline over the services (native, portable).

        win32service on Windows, systemd (D-Bus) on Linux - the same ``ServiceInfo`` records.

        Args:
            strict: What happens to a service that cannot be read. On Linux that is a refused
                unit-file listing (no unloaded service could be listed) or a refused ``LoadUnit``
                or ``LoadState`` read of one service file. False (default) skips it and logs a
                WARNING naming it, so a non-root caller still lists what it can read. True raises
                :class:`NativeCallError` naming the unit instead: use it when "not listed" must
                mean "does not exist", e.g. to check a service is gone. The pipeline is lazy, so
                the error is raised when iteration reaches the item. On Windows the listing drops
                no service (an unreadable configuration only leaves ``start_type`` None), so
                ``strict`` changes nothing there. A single field that cannot be read keeps its
                default in both modes on both OSes.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`ServiceInfo` records.

        Raises:
            NativeCallError: The service manager cannot be reached, or (with ``strict``) a
                service cannot be read - raised during iteration.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_service(strict=True), Pipeline)
            True
        """
        return Pipeline(self._service_source(strict=strict))

    def start_service(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        """Start a service; return its post-start state (native, portable, **mutating**).

        win32service on Windows, systemd (D-Bus, needs root) on Linux.

        Idempotent - starting an already-running service is a no-op. On Windows a service still
        stopping (``StopPending``) is first waited out, since the SCM cannot start it yet.

        Args:
            name: The service name (on Linux the unit name, ``"sshd.service"``).
            timeout: Seconds for the whole call: waiting out a pending stop, then for Running.

        Returns:
            The service's :class:`ServiceInfo` once it settled (``Running``; on Linux a
            ``Type=oneshot`` unit without ``RemainAfterExit`` reads ``Stopped`` once its job ran).

        Raises:
            OperationTimeoutError: The service was still not Running after ``timeout`` seconds -
                the start WAS issued, unless the service was still in a pending stop, which then
                did not end in time and got no start; ``.timeout`` holds the limit (CLI exit 110).
            NativeCallError: The start was refused or the service could not be opened.

        Example:
            >>> callable(build_ps().start_service)
            True
        """
        return self._service_controller.start(name, timeout=timeout)

    def stop_service(self, name: str, *, timeout: float = 30.0, wait: bool = True) -> ServiceInfo:
        """Stop a service; return its post-stop state (native, portable, **mutating**).

        win32service on Windows, systemd (D-Bus, needs root) on Linux.

        Idempotent - stopping an already-stopped service is a no-op. On Windows a service still
        starting (``StartPending``) is first waited out, in both forms, since the SCM refuses a
        stop until the start has settled.

        Args:
            name: The service name (on Linux the unit name, ``"sshd.service"``).
            timeout: Seconds for the whole call: waiting out a pending start, then (``wait=True``)
                for Stopped.
            wait: ``True`` polls until Stopped; ``False`` issues the stop and returns the state
                right after it, typically ``StopPending``, for a service that takes long to stop.

        Returns:
            The service's :class:`ServiceInfo` after the stop (or right after the control).

        Raises:
            OperationTimeoutError: ``wait=True`` and the service was still not Stopped after
                ``timeout`` seconds - the stop WAS issued; or a pending start did not end within
                ``timeout`` seconds and the stop was NOT issued; ``.timeout`` holds the limit.
            NativeCallError: The stop was refused or the service could not be opened.

        Example:
            >>> callable(build_ps().stop_service)
            True
        """
        return self._service_controller.stop(name, timeout=timeout, wait=wait)

    def restart_service(self, name: str, *, timeout: float = 30.0) -> ServiceInfo:
        """Restart a service; return its post-restart state (native, portable, **mutating**; systemd on Linux).

        win32service on Windows (a stop, then a start), systemd ``RestartUnit`` on Linux.

        Args:
            name: The service name (on Linux the unit name, ``"sshd.service"``).
            timeout: Seconds to wait for the service to settle. On Linux it bounds the one
                ``RestartUnit`` job; on Windows the stop and the start each get their own
                ``timeout``, so the whole restart can take up to twice ``timeout``.

        Returns:
            The service's :class:`ServiceInfo` once it settled.

        Raises:
            OperationTimeoutError: The service did not settle within ``timeout`` seconds;
                ``.timeout`` holds the limit (CLI exit 110). On Linux the restart WAS issued. On
                Windows a timeout in the stop half sends no start, and one while the stop half
                still waits out a pending start sends no stop either; a timeout in the start half
                means the start WAS issued.
            NativeCallError: The restart was refused, the service could not be opened, or (on
                Linux) the unit settled at ``failed``.

        Example:
            >>> callable(build_ps().restart_service)
            True
        """
        return self._service_controller.restart(name, timeout=timeout)

    def set_service(self, name: str, start_type: ServiceStartType) -> ServiceInfo:
        """Change a service's startup type; return its new config (native, portable, **mutating**).

        win32service on Windows; systemd enable/disable on Linux (Automatic or Disabled only).

        Args:
            name: The service name (on Linux the unit name, ``"sshd.service"``).
            start_type: The startup type to set. On Linux ``AUTOMATIC`` enables the unit file and
                ``DISABLED`` disables it, each followed by a daemon reload; any other member is
                refused.

        Returns:
            The service's :class:`ServiceInfo` read back after the change.

        Raises:
            NativeCallError: The change was refused, the service could not be opened, or (on
                Linux) ``start_type`` is neither ``AUTOMATIC`` nor ``DISABLED``.

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

        Nothing is opened until the pipeline is iterated, so every error below is raised then.

        Args:
            log_name: On Windows the channel to read (``"System"``, ``"Setup"``,
                ``"Microsoft-Windows-.../Operational"``). On Linux a unit filter: a name without
                a dot gets ``.service`` appended, and ``"System"``, ``"*"`` or ``""`` read the
                whole journal. Per journalctl(1), root and members of ``systemd-journal``,
                ``adm`` or ``wheel`` read the whole system journal; anyone else reads only the
                entries their own journal files hold.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`EventLogEntry` records,
            newest first.

        Raises:
            PlatformUnsupportedError: Off Windows, the ``systemd-python`` package of the
                ``[journald]`` extra is not installed.
            NativeCallError: The log cannot be opened or read, or (Windows) an event record
                cannot be rendered.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_win_event("System"), Pipeline)
            True
        """
        return Pipeline(self._event_log_source(log_name))

    def clear_event_log(self, log_name: str, *, backup_path: str | None = None) -> None:
        """Clear a Windows event log, optionally backing it up first (native, **mutating**).

        Args:
            log_name: The channel to clear (``"Application"``, ``"System"``, ...).
            backup_path: A file to save the log to before it is cleared; None (default) clears
                without a backup.

        Raises:
            PlatformUnsupportedError: pywin32 is not installed (any OS but Windows).
            NativeCallError: ``EvtClearLog`` failed (a :class:`NativeAccessDeniedError` when
                access is denied).

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

        Nothing is checked or queried until the pipeline is iterated, so every error below is
        raised then.

        Args:
            class_name: The WMI class (``"Win32_Process"``); letters, digits and underscores
                only, not starting with a digit.
            where: A WQL condition placed verbatim after ``WHERE``; None (default) selects every
                instance. It is not escaped, so never build it from untrusted input.
            namespace: The WMI namespace to connect to (default ``"root/cimv2"``).

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`CimInstance` records.

        Raises:
            PlatformUnsupportedError: pywin32 is not installed (any OS but Windows).
            NativeCallError: ``class_name`` is not a valid class name, the namespace cannot be
                connected to, or the query failed (the message carries the HRESULT when one is
                readable).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_cim_instance("Win32_OperatingSystem"), Pipeline)
            True
        """
        return Pipeline(self._cim_source(class_name, where=where, namespace=namespace))

    def get_scheduled_task(
        self, folder_path: str = "\\", *, task_name: str | None = None, strict: bool = False
    ) -> Pipeline[ScheduledTaskInfo]:
        """Return a lazy, STREAMING pipeline over scheduled tasks (native, portable).

        Recurses the Task Scheduler folder tree under ``folder_path`` on Windows (like
        Get-ScheduledTask); on Linux lists the systemd ``.timer`` units (``folder_path``
        ignored). Yields one record at a time. Given ``task_name`` it lists nothing and answers
        for that one task.

        Args:
            folder_path: The Task Scheduler folder to recurse (Windows); the root by default.
                With ``task_name``, the folder holding the task; a trailing backslash (the form a
                record's ``task_path`` carries) is accepted. Ignored on Linux.
            task_name: Look up this one task in ``folder_path`` without walking the tree, so an
                unreadable task elsewhere cannot affect the answer; empty when it does not exist,
                under ``strict`` too. The task's own name only: an empty name or one with a folder
                part raises :class:`ConfigurationError`. Windows matches it without regard to case
                and the record carries the spelling passed here and in ``folder_path`` (the
                scheduler echoes the caller's spelling), so compare it case-insensitively with a
                listed one. Linux: the timer task, else an on-demand task pwshpy registered (a
                ``.service`` with no ``.timer`` counts only when its unit file carries pwshpy's
                ``X-PwshPy`` marker, so a system daemon such as ``ssh`` is no task; the listing,
                timers only, leaves such a task out); a ``.timer``/``.service`` suffix is
                optional, and ``job.service`` names the task ``job`` - its timer record when it
                has a timer. None (default) lists as before.
            strict: What happens to an item that cannot be read - on Windows a folder whose
                tasks or subfolders the caller may not list, or a protected or corrupt task; on
                Linux a refused unit-file listing or ``LoadUnit``. False (default) skips it and
                logs a WARNING naming it, so a non-elevated caller still lists what it can read.
                True raises :class:`NativeCallError` naming the folder, task or unit instead: use
                it when "not listed" must mean "does not exist", e.g. to check a task is gone.
                The pipeline is lazy, so the error is raised when iteration reaches the item.
                With ``task_name`` a task or folder that does not exist is an empty answer either
                way; every other failure follows this rule: on Windows any GetFolder or GetTask
                error other than not-found (0x80070002) - access denied on the folder, a name the
                scheduler refuses (0x8007007B), a scheduler error - and a found task that cannot
                be read; on Linux a refused ``LoadUnit`` or ``LoadState`` read, or a unit file
                whose ownership marker cannot be read.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`ScheduledTaskInfo` records.

        Raises:
            ConfigurationError: ``task_name`` is empty or names a folder as well as a task
                (Linux also: no systemd unit name) - raised during iteration.
            NativeCallError: The scheduler cannot be reached, ``folder_path`` cannot be opened
                (without ``task_name``), or (with ``strict``) an item cannot be read - raised
                during iteration.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_scheduled_task(strict=True), Pipeline)
            True
            >>> lookup = build_ps().get_scheduled_task(r"\\Microsoft\\Windows\\Defrag", task_name="ScheduledDefrag")
            >>> isinstance(lookup, Pipeline)  # lazy: nothing is read until it is iterated
            True
        """
        return Pipeline(self._scheduled_task_source(folder_path, strict=strict, task_name=task_name))

    def register_scheduled_task(
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
        """Register a task with an optional principal, triggers and settings; return it (native, **mutating**).

        Task Scheduler on Windows, a systemd ``.service`` (plus a ``.timer`` when triggered) on
        Linux. Registering over an existing task replaces it.

        Args:
            task_path: Task path (``\\Folder\\Name`` on Windows, the unit base name on Linux).
            program: Executable to run.
            arguments: Program arguments.
            description: Task description.
            principal: Who the task runs as (a SID on Windows, a decimal uid on Linux);
                None runs it as the interactive caller on Windows and as root on Linux.
            triggers: When it runs (:class:`OnceTrigger`, :class:`DailyTrigger`,
                :class:`BootTrigger`, :class:`LogonTrigger`); empty means on demand only.
            settings: Battery, time-limit, multiple-instances, hard-terminate and demand-start
                switches (:class:`TaskSettings`). None, like ``TaskSettings()`` or any field left
                None, writes nothing, so the OS default applies (a start on demand stays allowed).
                With ``allow_demand_start=False`` only the triggers start the task, and
                :meth:`start_scheduled_task` raises :class:`NativeCallError` and runs nothing: on
                Windows it wraps Task Scheduler's SCHED_E_START_ON_DEMAND (0x80041328), on Linux
                systemd's refusal. It is validated again here, so a record's read-back ``settings`` can be
                passed on. Linux refuses a value systemd has no analog for (stopping a run on
                battery, any multiple-instances policy but IgnoreNew, IgnoreNew with a daily
                trigger).

        Returns:
            The task as the scheduler reads it back (:class:`ScheduledTaskInfo`): its
            ``user_id``, ``run_level``, state, run times and effective ``settings``.

        Raises:
            PlatformUnsupportedError: The OS cannot honour the principal, a trigger or a setting
                (a uid on Windows, a SID or a logon trigger on Linux, a setting with no systemd
                analog).
            ConfigurationError: The principal or the task text is inconsistent (a logon type
                that does not fit the principal, a group SID, a newline in ``program``).
            pydantic.ValidationError: ``settings`` breaks a :class:`TaskSettings` rule (a
                negative, fractional or above-cap ``execution_time_limit``).
            NativeCallError: The scheduler refused the registration.

        Example:
            >>> callable(build_ps().register_scheduled_task)
            True
        """
        return self._scheduled_task_controller.register(
            task_path,
            program=program,
            arguments=arguments,
            description=description,
            principal=principal,
            triggers=triggers,
            settings=_validated_settings(settings),
        )

    def unregister_scheduled_task(self, task_path: str) -> None:
        """Delete a scheduled task (native, **mutating**).

        On Linux only the units pwshpy wrote are stopped and deleted, so a vendor unit of the
        same name is left running and enabled.

        Args:
            task_path: The task (``\\Folder\\Name`` on Windows, ``\\Name`` at the root; the unit
                base name on Linux).

        Raises:
            ConfigurationError: Linux: ``task_path`` is no valid systemd unit name.
            NativeCallError: The scheduler refused the deletion; on Linux also no such task
                (pwshpy wrote neither unit) or a unit file of that name that pwshpy did not
                write or cannot read.

        Example:
            >>> callable(build_ps().unregister_scheduled_task)
            True
        """
        self._scheduled_task_controller.unregister(task_path)

    def enable_scheduled_task(self, task_path: str) -> ScheduledTaskInfo:
        """Enable a scheduled task; return its new state (native, **mutating**).

        On Linux this enables the task's ``.timer`` and starts it, except a timer with a
        boot-relative part (or one whose monotonic timers cannot be read), which is enabled
        but only arms at the next boot.

        Args:
            task_path: The task (``\\Folder\\Name`` on Windows, ``\\Name`` at the root; the unit
                base name on Linux).

        Returns:
            The task's :class:`ScheduledTaskInfo` after the change.

        Raises:
            ConfigurationError: Linux: ``task_path`` is no valid systemd unit name, or a bare
                template.
            NativeCallError: The scheduler refused the change; on Linux also a task with no
                ``.timer`` (on demand only) or a static timer (no ``[Install]`` section).

        Example:
            >>> callable(build_ps().enable_scheduled_task)
            True
        """
        return self._scheduled_task_controller.enable(task_path)

    def disable_scheduled_task(self, task_path: str) -> ScheduledTaskInfo:
        """Disable a scheduled task; return its new state (native, **mutating**).

        On Linux this stops and disables the task's ``.timer``.

        Args:
            task_path: The task (``\\Folder\\Name`` on Windows, ``\\Name`` at the root; the unit
                base name on Linux).

        Returns:
            The task's :class:`ScheduledTaskInfo` after the change.

        Raises:
            ConfigurationError: Linux: ``task_path`` is no valid systemd unit name, or a bare
                template.
            NativeCallError: The scheduler refused the change; on Linux also a task with no
                ``.timer`` (on demand only) or a static timer (no ``[Install]`` section), refused
                before anything is stopped.

        Example:
            >>> callable(build_ps().disable_scheduled_task)
            True
        """
        return self._scheduled_task_controller.disable(task_path)

    def start_scheduled_task(self, task_path: str) -> None:
        """Start a scheduled task now (native, **mutating**).

        On Linux this starts the task's ``.service``.

        Args:
            task_path: The task (``\\Folder\\Name`` on Windows, ``\\Name`` at the root; the unit
                base name on Linux).

        Raises:
            ConfigurationError: Linux: ``task_path`` is no valid systemd unit name, or a bare
                template.
            NativeCallError: The scheduler refused the start, e.g. for a task registered with
                ``allow_demand_start=False``.

        Example:
            >>> callable(build_ps().start_scheduled_task)
            True
        """
        self._scheduled_task_controller.run(task_path)

    def stop_scheduled_task(self, task_path: str) -> None:
        """Stop a running scheduled task (native, **mutating**).

        On Linux this stops the task's ``.service``.

        Args:
            task_path: The task (``\\Folder\\Name`` on Windows, ``\\Name`` at the root; the unit
                base name on Linux).

        Raises:
            ConfigurationError: Linux: ``task_path`` is no valid systemd unit name, or a bare
                template.
            NativeCallError: The scheduler refused the stop or the task could not be opened.

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

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`LocalUser` records.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_local_user(), Pipeline)
            True
        """
        return Pipeline(self._local_user_source())

    def get_local_group(self) -> Pipeline[LocalGroup]:
        """Return a lazy pipeline over the local groups (native, portable).

        win32net on Windows, stdlib ``grp`` on Linux/macOS.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`LocalGroup` records.

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

        Win32 ``NetUserAdd`` on Windows; ``useradd`` (then ``chpasswd`` and ``usermod --lock``
        as needed) on Linux, which needs root.

        Args:
            name: The account name.
            password: The initial password; on Linux an empty one (default) sets none.
            full_name: The display name. Linux has one GECOS comment, which takes ``full_name``,
                or ``description`` when ``full_name`` is empty.
            description: The account description (on Linux see ``full_name``).
            disabled: Create the account disabled (on Linux its password is locked).
            timeout: Linux: seconds each shadow-utils command may run before it is killed
                (default 30.0). Ignored on Windows, where the win32net call is synchronous.

        Returns:
            The created :class:`LocalUser`: on Windows read back, on Linux read from ``pwd``
            with ``enabled`` set from ``disabled`` and an empty ``description``.

        Raises:
            OperationTimeoutError: Linux: a shadow-utils command outlived ``timeout``. A
                ``NativeCallError`` subclass, so catch it first to tell it apart.
            NativeCallError: The account could not be created; on Linux also a name that is not a
                valid POSIX account name (checked before any command runs), or a CR, LF (or, in the
                comment, a colon) that would corrupt the account entry.

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

        Win32 ``NetUserDel`` on Windows; ``userdel`` on Linux, which leaves the home directory.

        Args:
            name: The account name.
            timeout: Linux: seconds each shadow-utils command may run before it is killed
                (default 30.0). Ignored on Windows, where the win32net call is synchronous.

        Raises:
            OperationTimeoutError: Linux: a shadow-utils command outlived ``timeout``. A
                ``NativeCallError`` subclass, so catch it first to tell it apart.
            NativeCallError: The account could not be deleted; on Linux also a name that is not a
                valid POSIX account name (checked before any command runs).

        Example:
            >>> callable(build_ps().remove_local_user)
            True
        """
        self._local_account_controller.remove_user(name, timeout=timeout)

    def enable_local_user(self, name: str, *, timeout: float = 30.0) -> LocalUser:
        """Enable a local user; return its new state (native, **mutating**; Linux bounds by ``timeout`` s).

        On Windows this toggles ``UF_ACCOUNTDISABLE``; on Linux ``usermod`` unlocks the password.

        Args:
            name: The account name.
            timeout: Linux: seconds each shadow-utils command may run before it is killed
                (default 30.0). Ignored on Windows, where the win32net call is synchronous.

        Returns:
            The :class:`LocalUser`: on Windows read back, on Linux read from ``pwd`` with
            ``enabled`` set to the state just applied.

        Raises:
            OperationTimeoutError: Linux: a shadow-utils command outlived ``timeout``. A
                ``NativeCallError`` subclass, so catch it first to tell it apart.
            NativeCallError: The change was refused or the account does not exist; on Linux also a
                name that is not a valid POSIX account name (checked before any command runs).

        Example:
            >>> callable(build_ps().enable_local_user)
            True
        """
        return self._local_account_controller.set_user_enabled(name, enabled=True, timeout=timeout)

    def disable_local_user(self, name: str, *, timeout: float = 30.0) -> LocalUser:
        """Disable a local user; return its new state (native, **mutating**; Linux bounds by ``timeout`` s).

        On Windows this toggles ``UF_ACCOUNTDISABLE``; on Linux ``usermod`` locks the password.

        Args:
            name: The account name.
            timeout: Linux: seconds each shadow-utils command may run before it is killed
                (default 30.0). Ignored on Windows, where the win32net call is synchronous.

        Returns:
            The :class:`LocalUser`: on Windows read back, on Linux read from ``pwd`` with
            ``enabled`` set to the state just applied.

        Raises:
            OperationTimeoutError: Linux: a shadow-utils command outlived ``timeout``. A
                ``NativeCallError`` subclass, so catch it first to tell it apart.
            NativeCallError: The change was refused or the account does not exist; on Linux also a
                name that is not a valid POSIX account name (checked before any command runs).

        Example:
            >>> callable(build_ps().disable_local_user)
            True
        """
        return self._local_account_controller.set_user_enabled(name, enabled=False, timeout=timeout)

    def new_local_group(self, name: str, *, description: str = "", timeout: float = 30.0) -> LocalGroup:
        """Create a local group; return the created record (native, **mutating**; Linux bounds by ``timeout`` s).

        Win32 ``NetLocalGroupAdd`` on Windows; ``groupadd`` on Linux.

        Args:
            name: The group name.
            description: The group description; ignored on Linux, where groups have none.
            timeout: Linux: seconds each shadow-utils command may run before it is killed
                (default 30.0). Ignored on Windows, where the win32net call is synchronous.

        Returns:
            The created :class:`LocalGroup` (on Linux read from ``grp``, with an empty
            ``description``).

        Raises:
            OperationTimeoutError: Linux: a shadow-utils command outlived ``timeout``. A
                ``NativeCallError`` subclass, so catch it first to tell it apart.
            NativeCallError: The group could not be created; on Linux also a name that is not a
                valid POSIX account name (checked before any command runs).

        Example:
            >>> callable(build_ps().new_local_group)
            True
        """
        return self._local_account_controller.new_group(name, description=description, timeout=timeout)

    def remove_local_group(self, name: str, *, timeout: float = 30.0) -> None:
        """Delete a local group (native, **mutating**; Linux bounds the command by ``timeout`` s).

        Args:
            name: The group name.
            timeout: Linux: seconds each shadow-utils command may run before it is killed
                (default 30.0). Ignored on Windows, where the win32net call is synchronous.

        Raises:
            OperationTimeoutError: Linux: a shadow-utils command outlived ``timeout``. A
                ``NativeCallError`` subclass, so catch it first to tell it apart.
            NativeCallError: The group could not be deleted; on Linux also a name that is not a
                valid POSIX account name (checked before any command runs).

        Example:
            >>> callable(build_ps().remove_local_group)
            True
        """
        self._local_account_controller.remove_group(name, timeout=timeout)

    def add_local_group_member(self, group: str, member: str, *, timeout: float = 30.0) -> None:
        """Add a user/group to a local group (native, **mutating**; Linux bounds by ``timeout`` s).

        Win32 ``NetLocalGroupAddMembers`` on Windows; ``gpasswd --add`` on Linux.

        Args:
            group: The local group to add to.
            member: The user (or, on Windows, group) name to add.
            timeout: Linux: seconds each shadow-utils command may run before it is killed
                (default 30.0). Ignored on Windows, where the win32net call is synchronous.

        Raises:
            OperationTimeoutError: Linux: a shadow-utils command outlived ``timeout``. A
                ``NativeCallError`` subclass, so catch it first to tell it apart.
            NativeCallError: The member could not be added; on Linux also a name that is not a valid
                POSIX account name (checked before any command runs).

        Example:
            >>> callable(build_ps().add_local_group_member)
            True
        """
        self._local_account_controller.add_group_member(group, member, timeout=timeout)

    def remove_local_group_member(self, group: str, member: str, *, timeout: float = 30.0) -> None:
        """Remove a member from a local group (native, **mutating**; Linux bounds by ``timeout`` s).

        Win32 ``NetLocalGroupDelMembers`` on Windows; ``gpasswd --delete`` on Linux.

        Args:
            group: The local group to remove from.
            member: The member name to remove.
            timeout: Linux: seconds each shadow-utils command may run before it is killed
                (default 30.0). Ignored on Windows, where the win32net call is synchronous.

        Raises:
            OperationTimeoutError: Linux: a shadow-utils command outlived ``timeout``. A
                ``NativeCallError`` subclass, so catch it first to tell it apart.
            NativeCallError: The member could not be removed; on Linux also a name that is not a
                valid POSIX account name (checked before any command runs).

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

        On Windows a NULL DACL (an unprotected object) yields one Everyone/Allow entry, while
        an empty DACL yields none. On POSIX a file with no extended ACL yields its three base
        entries from the mode. Nothing is read until the pipeline is iterated.

        Args:
            path: The file or directory whose ACL to read.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`AclEntry` records.

        Raises:
            NativeCallError: The path cannot be read, or on Windows its security descriptor
                cannot (raised on iteration; a :class:`NativeAccessDeniedError` when access is
                denied). On POSIX an extended ACL that cannot be read falls back to the three
                mode-bit entries instead of raising.

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

        On Windows the ACE is appended to the DACL; on Linux an existing entry for the same
        trustee is replaced and the ACL mask is recomputed.

        Args:
            path: The file or directory to change.
            trustee: On Windows a SID string (``"S-1-..."``) or an account name; on Linux
                ``user:NAME`` or ``group:NAME`` (a bare name is a user; a numeric name is the id).
            rights: A Win32 access mask on Windows; on Linux only the low three bits count
                (``4`` read, ``2`` write, ``1`` execute).
            access_type: ``AceType.ALLOW`` (default) or ``AceType.DENY``; deny is Windows only.

        Raises:
            NativeCallError: The trustee cannot be resolved, the ACL cannot be read or written,
                or (Linux) ``access_type`` is ``DENY``; a :class:`NativeAccessDeniedError` when
                access is denied.

        Example:
            >>> callable(build_ps().add_acl_ace)
            True
        """
        self._acl_controller.add_ace(path, trustee, rights, access_type=access_type)

    def remove_acl_ace(self, path: str, trustee: str, *, access_type: AceType | None = None) -> None:
        """Remove a trustee's entries from a path's ACL (native, portable, **mutating**).

        On Linux the ACL mask is recomputed afterwards (and dropped when no named entry is
        left); POSIX has no deny entries, so ``access_type=AceType.DENY`` changes nothing there.

        Args:
            path: The file or directory to change.
            trustee: On Windows a SID string (``"S-1-..."``) or an account name; on Linux
                ``user:NAME`` or ``group:NAME`` (a bare name is a user; a numeric name is the id).
            access_type: Remove only the trustee's entries of this type; None (default) removes
                all of them.

        Raises:
            NativeCallError: The trustee cannot be resolved, or the ACL cannot be read or
                written (a :class:`NativeAccessDeniedError` when access is denied).

        Example:
            >>> callable(build_ps().remove_acl_ace)
            True
        """
        self._acl_controller.remove_ace(path, trustee, access_type=access_type)

    def set_owner(self, path: str, owner: str) -> None:
        """Set the owner of a filesystem path (native, portable, **mutating**; ``chown`` on POSIX).

        Making another principal the owner needs privilege (SeRestorePrivilege on Windows, root
        on POSIX). On POSIX the owning group is left unchanged.

        Args:
            path: The file or directory to change.
            owner: The new owner: on Windows a SID string (``"S-1-..."``) or an account name; on
                POSIX a user name or a numeric uid.

        Raises:
            NativeCallError: The owner cannot be resolved, or the owner cannot be set (a
                :class:`NativeAccessDeniedError` when access is denied).

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

        Args:
            script: The PowerShell source to run. It executes with the caller's privileges, so
                never build it from untrusted input (use :meth:`cmdlet` for data).
            timeout: Seconds before the host stops (cancels) the pipeline; None (default) waits
                without a limit.

        Returns:
            One marshaled item per output object.

        Raises:
            FeatureUnavailableError: The ``[full]`` extra or the .NET 10 / PowerShell 7.6 host is
                absent.
            PowerShellError: The script wrote to the error stream (the first error is the
                message), raised a terminating error, or exceeded ``timeout``.

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

        Args:
            name: The cmdlet or command name (``"Get-Service"``).
            *args: Positional arguments, bound in order with ``AddArgument``.
            timeout: Seconds before the host stops (cancels) the pipeline; None (default) waits
                without a limit.
            **params: Named parameters (PowerShell's own names), bound with ``AddParameter``; a
                ``bool`` binds to a ``[switch]`` and a list becomes an array.

        Returns:
            The :class:`~pwshpy.domain.records.PSInvocationResult`; check its ``had_errors`` and
            ``errors``.

        Raises:
            FeatureUnavailableError: The ``[full]`` extra or the .NET 10 / PowerShell 7.6 host is
                absent.
            PowerShellError: The invocation raised a terminating error or exceeded ``timeout``.

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

        Args:
            name: The command name; it is bound as a script parameter, never interpolated.

        Returns:
            A :class:`CommandInfo`: name, command type, module and each parameter's name, .NET
            type, mandatory flag and aliases.

        Raises:
            FeatureUnavailableError: The ``[full]`` extra or the .NET 10 / PowerShell 7.6 host is
                absent.
            PowerShellError: The command does not exist, or ``Get-Command`` failed.

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

        A thin wrapper over :meth:`cmdlet`.

        Args:
            timeout: Seconds before the host stops (cancels) the pipeline; None (default) waits
                without a limit.
            **params: ``Get-ADUser``'s parameters by their PowerShell names, bound safely.

        Returns:
            The :class:`~pwshpy.domain.records.PSInvocationResult` of ``Get-ADUser``.

        Raises:
            FeatureUnavailableError: The ``[full]`` extra or the .NET 10 / PowerShell 7.6 host is
                absent.
            PowerShellError: The invocation raised a terminating error or exceeded ``timeout``.

        Example:
            >>> callable(build_ps().get_ad_user)
            True
        """
        return self.cmdlet("Get-ADUser", timeout=timeout, **params)

    def get_ad_group(self, *, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Query Active Directory groups (.NET; Get-ADGroup, needs [full] + ActiveDirectory).

        A thin wrapper over :meth:`cmdlet`.

        Args:
            timeout: Seconds before the host stops (cancels) the pipeline; None (default) waits
                without a limit.
            **params: ``Get-ADGroup``'s parameters by their PowerShell names, bound safely.

        Returns:
            The :class:`~pwshpy.domain.records.PSInvocationResult` of ``Get-ADGroup``.

        Raises:
            FeatureUnavailableError: The ``[full]`` extra or the .NET 10 / PowerShell 7.6 host is
                absent.
            PowerShellError: The invocation raised a terminating error or exceeded ``timeout``.

        Example:
            >>> callable(build_ps().get_ad_group)
            True
        """
        return self.cmdlet("Get-ADGroup", timeout=timeout, **params)

    def get_ad_computer(self, *, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Query Active Directory computers (.NET; Get-ADComputer, needs [full] + ActiveDirectory).

        A thin wrapper over :meth:`cmdlet`.

        Args:
            timeout: Seconds before the host stops (cancels) the pipeline; None (default) waits
                without a limit.
            **params: ``Get-ADComputer``'s parameters by their PowerShell names, bound safely.

        Returns:
            The :class:`~pwshpy.domain.records.PSInvocationResult` of ``Get-ADComputer``.

        Raises:
            FeatureUnavailableError: The ``[full]`` extra or the .NET 10 / PowerShell 7.6 host is
                absent.
            PowerShellError: The invocation raised a terminating error or exceeded ``timeout``.

        Example:
            >>> callable(build_ps().get_ad_computer)
            True
        """
        return self.cmdlet("Get-ADComputer", timeout=timeout, **params)

    def get_mailbox(self, *, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Query Exchange mailboxes (.NET; Get-Mailbox, needs [full] + the Exchange module).

        A thin wrapper over :meth:`cmdlet`.

        Args:
            timeout: Seconds before the host stops (cancels) the pipeline; None (default) waits
                without a limit.
            **params: ``Get-Mailbox``'s parameters by their PowerShell names, bound safely.

        Returns:
            The :class:`~pwshpy.domain.records.PSInvocationResult` of ``Get-Mailbox``.

        Raises:
            FeatureUnavailableError: The ``[full]`` extra or the .NET 10 / PowerShell 7.6 host is
                absent.
            PowerShellError: The invocation raised a terminating error or exceeded ``timeout``.

        Example:
            >>> callable(build_ps().get_mailbox)
            True
        """
        return self.cmdlet("Get-Mailbox", timeout=timeout, **params)

    def get_az_vm(self, *, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Query Azure virtual machines (.NET; Get-AzVM, needs [full] + the Az.Compute module).

        A thin wrapper over :meth:`cmdlet`.

        Args:
            timeout: Seconds before the host stops (cancels) the pipeline; None (default) waits
                without a limit.
            **params: ``Get-AzVM``'s parameters by their PowerShell names, bound safely.

        Returns:
            The :class:`~pwshpy.domain.records.PSInvocationResult` of ``Get-AzVM``.

        Raises:
            FeatureUnavailableError: The ``[full]`` extra or the .NET 10 / PowerShell 7.6 host is
                absent.
            PowerShellError: The invocation raised a terminating error or exceeded ``timeout``.

        Example:
            >>> callable(build_ps().get_az_vm)
            True
        """
        return self.cmdlet("Get-AzVM", timeout=timeout, **params)

    def get_az_resource_group(self, *, timeout: float | None = None, **params: Any) -> PSInvocationResult:
        """Query Azure resource groups (.NET; Get-AzResourceGroup, needs [full] + Az.Resources).

        A thin wrapper over :meth:`cmdlet`.

        Args:
            timeout: Seconds before the host stops (cancels) the pipeline; None (default) waits
                without a limit.
            **params: ``Get-AzResourceGroup``'s parameters by their PowerShell names, bound safely.

        Returns:
            The :class:`~pwshpy.domain.records.PSInvocationResult` of ``Get-AzResourceGroup``.

        Raises:
            FeatureUnavailableError: The ``[full]`` extra or the .NET 10 / PowerShell 7.6 host is
                absent.
            PowerShellError: The invocation raised a terminating error or exceeded ``timeout``.

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
        wait_tree: bool = False,
    ) -> ProcessResult:
        """Run an external program from an argv list; return a typed result (native, portable).

        ``subprocess`` done right - no shell, so no quoting/``--%`` hell; the
        ``exit_code`` is ALWAYS set (unlike ``$LASTEXITCODE`` in a pipeline); and
        ``stderr`` is plain data, never mistaken for a terminating error.  Call
        ``.check()`` on the result to raise on a nonzero exit.

        ``wait_tree=True`` waits like ``Start-Process -Wait``: until the program AND every
        process it started have exited (a Win32 job object on Windows, a child subreaper on
        Linux, the process group on macOS - where a ``setsid`` grandchild is not covered),
        and ``timeout`` then covers the whole tree and kills it on expiry.

        Args:
            argv: The program and its arguments as a list, passed verbatim, never through a shell.
            cwd: The working directory for the program; None keeps the caller's.
            timeout: Seconds before the program (with ``wait_tree`` its whole tree) is killed, in
                ``(0, 2147483]``; None waits without a limit.
            env: The program's whole environment (replaces the caller's, never merged);
                None inherits the caller's.
            input_text: Text written to the program's stdin; None gives it no input.
            wait_tree: Also wait for every process the program started.

        Returns:
            A :class:`ProcessResult` with ``exit_code`` (the started program's own), ``stdout``,
            ``stderr`` and ``duration_s``; a nonzero exit is returned, not raised.

        Raises:
            OperationTimeoutError: ``timeout`` elapsed; the child (with ``wait_tree`` its
                whole process tree) was killed and the output it wrote is not returned. A
                ``NativeCallError`` subclass, so catch it first to tell it apart.
            NativeCallError: The executable was not found or could not be started.
            ValueError: ``timeout`` is outside ``(0, 2147483]`` (raised at the call, before the
                program starts).

        Example:
            >>> import sys
            >>> build_ps().exec([sys.executable, "-c", "print(1)"]).stdout.strip()
            '1'
        """
        if timeout is not None:
            _check_wait_limit(timeout)
        return self._process_runner(argv, cwd=cwd, timeout=timeout, env=env, input_text=input_text, wait_tree=wait_tree)

    def pack_script(
        self, entry: str | Path, dest: str | Path | None = None, *, options: PackOptions = DEFAULT_PACK_OPTIONS
    ) -> PackedScript:
        """Pack a Python script and its local modules into a self-extracting ``.ps1`` or ``.sh`` (portable).

        The artefact is one file: it unpacks itself into a per-user cache, provisions ``uv``
        if the machine has none, runs the script, and exits with the script's own exit code.
        Third-party dependencies come from the entry's PEP 723 block or ``options.with_packages`` -
        never guessed from an import name.  A ``.ps1`` runs on Windows PowerShell 5.1 and pwsh 7
        alike, a ``.sh`` under any POSIX ``/bin/sh``; ``options.format`` picks one
        (:class:`RunnerFormat`, by default from ``dest``'s suffix).

        Args:
            entry: The script to pack.
            dest: The runner to write; None writes ``<entry>.ps1`` beside the entry, or
                ``<entry>.sh`` when ``options.format`` is :attr:`RunnerFormat.SH`.
            options: How to pack (:class:`PackOptions`: ``include``, ``with_packages``,
                ``root``, ``force``, ``format``).

        Returns:
            The :class:`PackedScript` manifest of what was written.

        Raises:
            PackError: The entry is missing or unreadable, a module does not parse, a local
                import resolves outside the root, a file name cannot be packed, or ``dest``
                exists without ``force``.

        Example:
            >>> callable(build_ps().pack_script)
            True
        """
        return self._script_packer(entry, dest, options=options)

    def unpack_script(self, source: str | Path, dest: str | Path, *, force: bool = False) -> PackedScript:
        """Restore the sources a packed ``.ps1`` or ``.sh`` carries, ready to edit and pack again (portable).

        The inverse of :meth:`pack_script`; the format is sniffed from the payload (zip or
        tar), never told.  The generated ``__main__`` shim is machinery, not source, so it is
        left out of the restored tree.

        Args:
            source: The packed runner to read.
            dest: The directory to restore into.
            force: Overwrite files that already exist in ``dest``.

        Returns:
            The :class:`PackedScript` manifest of the restored files.

        Raises:
            PackError: ``source`` is not a pwshpy runner, its payload is damaged, or a file
                exists in ``dest`` without ``force``.

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

        Args:
            path: The file to write; an existing file is overwritten.
            text: The text to write.
            encoding: The text encoding (default ``"utf-8"``); a BOM is written only with
                ``bom``, whichever spelling of a UTF-16/32 codec is named.
            newline: The line terminator every CRLF, CR and LF is turned into (default ``"\\n"``;
                ``"\\r\\n"`` for CRLF).
            bom: Prepend the encoding's byte-order mark (default False).

        Returns:
            The :class:`~pathlib.Path` written.

        Raises:
            LookupError: ``encoding`` names no text encoding.
            UnicodeEncodeError: The text cannot be written in ``encoding``.
            OSError: The file cannot be written (not wrapped in a pwshpy error).

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

        Args:
            path: The file to write; it is opened (and truncated) before the first chunk.
            chunks: The text pieces, consumed lazily; a CRLF split across two chunks still
                becomes one line end.
            encoding: The text encoding (default ``"utf-8"``); a BOM is written only with
                ``bom``, whichever spelling of a UTF-16/32 codec is named.
            newline: The line terminator every CRLF, CR and LF is turned into (default ``"\\n"``;
                ``"\\r\\n"`` for CRLF).
            bom: Prepend the encoding's byte-order mark (default False).

        Returns:
            The :class:`~pathlib.Path` written.

        Raises:
            LookupError: ``encoding`` names no text encoding.
            UnicodeEncodeError: The text cannot be written in ``encoding``.
            OSError: The file cannot be written (not wrapped in a pwshpy error); the chunks
                before a failure stay written.

        Example:
            >>> callable(build_ps().write_text_stream)
            True
        """
        return self._text_stream_writer(path, chunks, encoding=encoding, newline=newline, bom=bom)

    def write_records(self, path: str | Path, records: Iterable[PSRecord], *, jsonl: bool = True) -> Path:
        """Write typed records to a file as UTF-8 JSON/JSONL, never a BOM (native, portable).

        Pairs with any pipeline: ``ps.write_records("procs.jsonl", ps.get_process())``. A field
        holding a byte that is not valid UTF-8 is written with U+FFFD for that byte and logged as
        one WARNING per record (``pwshpy.adapters.native.fileio``); every record is written.

        Args:
            path: The file to write; an existing file is overwritten.
            records: The records to serialize, consumed one at a time (a lazy pipeline is fine).
            jsonl: One JSON object per line (default); False writes a single JSON array.

        Returns:
            The :class:`~pathlib.Path` written.

        Raises:
            orjson.JSONEncodeError: A record holds a value orjson cannot write (an integer
                outside the 64-bit range); the records before it are written.
            OSError: The file cannot be written (not wrapped in a pwshpy error).

        Example:
            >>> callable(build_ps().write_records)
            True
        """
        return self._record_writer(path, records, jsonl=jsonl)

    def get_child_item(self, path: str, *, recurse: bool = False) -> Pipeline[FileSystemItem]:
        """List a directory's entries as typed records (native, portable; like Get-ChildItem).

        ``recurse`` walks the whole tree, lazily.

        Args:
            path: The directory to list.
            recurse: Walk every level below ``path``; False (default) lists its entries only.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`FileSystemItem` records
            (``size`` None for a directory).

        Raises:
            NativeCallError: The directory or an entry cannot be read (raised on iteration; a
                :class:`NativeAccessDeniedError` when access is denied).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_child_item("."), Pipeline)
            True
        """
        return Pipeline(self._file_system.get_child_item(path, recurse=recurse))

    def get_item(self, path: str) -> FileSystemItem:
        """Return one file/directory as a typed record (native, portable; like Get-Item).

        Args:
            path: The file or directory.

        Returns:
            Its :class:`FileSystemItem` (``size`` None for a directory).

        Raises:
            NativeCallError: ``path`` does not exist or cannot be read.

        Example:
            >>> callable(build_ps().get_item)
            True
        """
        return self._file_system.get_item(path)

    def get_content(self, path: str, *, encoding: str = "utf-8") -> str:
        """Return a file's whole text content (native, portable; like ``Get-Content -Raw``).

        Materializes the file; for a large/unbounded file use :meth:`get_content_lines`.

        Args:
            path: The file to read.
            encoding: The text encoding (default ``"utf-8"``).

        Returns:
            The whole text, newlines translated to ``"\\n"`` (Python's universal newlines).

        Raises:
            NativeCallError: The file cannot be read (a :class:`NativeAccessDeniedError` when
                access is denied).
            LookupError: ``encoding`` names no codec.
            UnicodeDecodeError: The file is not valid ``encoding``.

        Example:
            >>> callable(build_ps().get_content)
            True
        """
        return self._file_system.get_content(path, encoding=encoding)

    def get_content_lines(self, path: str, *, encoding: str = "utf-8") -> Pipeline[str]:
        """Return a lazy, **memory-bounded** pipeline over a file's lines (like ``Get-Content``).

        Streams one line at a time, so ``ps.get_content_lines(huge).take(10)`` reads only
        the first lines - the same laziness as the record pipelines.

        The file is opened by this call, so a missing or unreadable path raises here, not on
        iteration; it is closed when the lines run out.

        Args:
            path: The file to read.
            encoding: The text encoding (default ``"utf-8"``).

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of the lines, each with its line
            end (``"\\n"`` after Python's universal-newline translation).

        Raises:
            NativeCallError: The file cannot be opened, or a read fails during iteration.
            LookupError: ``encoding`` names no codec.
            UnicodeDecodeError: A line is not valid ``encoding`` (raised on iteration).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_content_lines(__file__), Pipeline)
            True
        """
        return Pipeline(self._file_system.get_content_lines(path, encoding=encoding))

    def test_path(self, path: str) -> bool:
        """Return whether ``path`` exists (native, portable; like Test-Path).

        Args:
            path: The path to check.

        Returns:
            True when it exists (a broken symlink reads False, as :meth:`pathlib.Path.exists`
            follows links).

        Example:
            >>> isinstance(build_ps().test_path("."), bool)
            True
        """
        return self._file_system.test_path(path)

    def new_item(self, path: str, *, item_type: FileItemType = FileItemType.FILE) -> FileSystemItem:
        """Create a file or directory (per ``item_type``); return it (native, **mutating**).

        Missing parent directories are created. An existing directory is kept; an existing
        file is kept too, with its modification time set to now.

        Args:
            path: The file or directory to create.
            item_type: ``FileItemType.FILE`` (default, an empty file) or
                ``FileItemType.DIRECTORY``.

        Returns:
            The created item's :class:`FileSystemItem`.

        Raises:
            NativeCallError: The item could not be created (a :class:`NativeAccessDeniedError`
                when access is denied).

        Example:
            >>> callable(build_ps().new_item)
            True
        """
        return self._file_system.new_item(path, item_type=item_type)

    def copy_item(self, source: str, destination: str, *, recurse: bool = False) -> None:
        """Copy a file, or a directory tree when ``recurse`` (native, **mutating**; like Copy-Item).

        A file is copied with its metadata (``shutil.copy2``); a directory with ``recurse`` is
        merged into ``destination`` (``shutil.copytree`` with ``dirs_exist_ok``).

        Args:
            source: The file or directory to copy.
            destination: The target path (for a file, an existing directory receives it).
            recurse: Copy a directory and everything below it; without it a directory is
                refused.

        Raises:
            NativeCallError: The copy failed, including a directory ``source`` without
                ``recurse``.

        Example:
            >>> callable(build_ps().copy_item)
            True
        """
        self._file_system.copy_item(source, destination, recurse=recurse)

    def move_item(self, source: str, destination: str) -> None:
        """Move or rename a file/directory (native, **mutating**; like Move-Item).

        Args:
            source: The file or directory to move.
            destination: The new path; an existing directory receives ``source`` inside it
                (``shutil.move``).

        Raises:
            NativeCallError: The move failed.

        Example:
            >>> callable(build_ps().move_item)
            True
        """
        self._file_system.move_item(source, destination)

    def remove_item(self, path: str, *, recurse: bool = False) -> None:
        """Delete a file, or a directory (``recurse`` for its contents) (native, **mutating**).

        Args:
            path: The file or directory to delete.
            recurse: Delete a directory with everything in it; without it only an empty
                directory is deleted.

        Raises:
            NativeCallError: The deletion failed (a missing path, a non-empty directory without
                ``recurse``, or a :class:`NativeAccessDeniedError` when access is denied).

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
        skip_http_error_check: bool = False,
    ) -> WebResponse:
        """Perform an HTTP request; return a typed WebResponse (native, portable; like Invoke-WebRequest).

        A status of 400 or above raises, as ``Invoke-WebRequest`` does, unless
        ``skip_http_error_check`` is True.

        Args:
            url: An ``http://`` or ``https://`` URL; any other scheme is refused.
            method: The HTTP method (default ``"GET"``).
            headers: Request headers; None (default) sends none of the caller's own.
            body: The request body, sent UTF-8 encoded; None (default) sends none.
            timeout: Seconds for the connection and each blocking read (default 30.0), passed
                to ``urllib``; running out raises ``OperationTimeoutError``.
            skip_http_error_check: True returns a 4xx or 5xx answer with its status and full
                body instead of raising (pwsh's ``-SkipHttpErrorCheck``); default False.

        Returns:
            A :class:`WebResponse` with ``status_code``, ``headers`` and ``text`` (the body
            decoded as UTF-8, undecodable bytes replaced).

        Raises:
            OperationTimeoutError: The connection or a read did not finish within ``timeout``.
            NativeCallError: The scheme is not http/https, the request failed below HTTP
                (DNS, connection, TLS), or the server answered 400 or above and
                ``skip_http_error_check`` is False; the message names the status and the
                start of the body (at most 200 characters, on one line).

        Example:
            >>> callable(build_ps().invoke_web_request)
            True
        """
        return self._web_requester(
            url,
            method=method,
            headers=headers,
            body=body,
            timeout=timeout,
            skip_http_error_check=skip_http_error_check,
        )

    def invoke_rest_method(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        timeout: float = 30.0,
        skip_http_error_check: bool = False,
    ) -> Any:
        """Perform an HTTP request; return the parsed JSON (native, portable; like Invoke-RestMethod).

        Built on :meth:`invoke_web_request`, so a status of 400 or above raises, as
        ``Invoke-RestMethod`` does, unless ``skip_http_error_check`` is True; then the error
        body is parsed like any other.

        Args:
            url: An ``http://`` or ``https://`` URL; any other scheme is refused.
            method: The HTTP method (default ``"GET"``).
            headers: Request headers; None (default) sends none of the caller's own.
            json_body: A value serialized as the JSON request body, with
                ``Content-Type: application/json`` unless ``headers`` sets one; None (default)
                sends no body.
            timeout: Seconds for the connection and each blocking read (default 30.0), passed
                to ``urllib``; running out raises ``OperationTimeoutError``.
            skip_http_error_check: True parses a 4xx or 5xx answer's body instead of raising
                (pwsh's ``-SkipHttpErrorCheck``); default False.

        Returns:
            The response body parsed as JSON.

        Raises:
            OperationTimeoutError: The connection or a read did not finish within ``timeout``.
            NativeCallError: The scheme is not http/https, the request failed below HTTP, the
                server answered 400 or above and ``skip_http_error_check`` is False, or the
                response body is not JSON.
            TypeError: ``json_body`` holds a value orjson cannot serialize
                (``orjson.JSONEncodeError``).

        Example:
            >>> callable(build_ps().invoke_rest_method)
            True
        """
        return self._rest_invoker(
            url,
            method=method,
            headers=headers,
            json_body=json_body,
            timeout=timeout,
            skip_http_error_check=skip_http_error_check,
        )

    def download_file(self, url: str, dest: str | Path, *, chunk_size: int = 65536, timeout: float = 30.0) -> Path:
        """Stream an HTTP download to ``dest``, **memory-bounded** (native, portable).

        Unlike :meth:`invoke_web_request` (which returns the body as data), this copies the
        response to a file in fixed-size chunks, so a huge download never buffers in memory.

        Args:
            url: An ``http://`` or ``https://`` URL; any other scheme is refused.
            dest: The file to write; an existing file is overwritten.
            chunk_size: Bytes copied per read (default 65536).
            timeout: Seconds for the connection and each blocking read (default 30.0), passed
                to ``urllib``; running out raises ``OperationTimeoutError``.

        Returns:
            ``dest`` as a :class:`~pathlib.Path`.

        Raises:
            OperationTimeoutError: The connection or a read did not finish within ``timeout``;
                ``dest`` is deleted.
            NativeCallError: The scheme is not http/https, the server answered 4xx or 5xx, or
                the transfer failed; ``dest`` is deleted in the last two cases.

        Example:
            >>> callable(build_ps().download_file)
            True
        """
        return self._file_downloader(url, dest, chunk_size=chunk_size, timeout=timeout)

    def stop_process(self, pid: int, *, force: bool = False) -> None:
        """Terminate a process by PID (``force`` hard-kills) (native, portable, **mutating**; like Stop-Process).

        Returns once the signal is sent; use :meth:`wait_process` to wait for the exit.

        Args:
            pid: The process id.
            force: Kill the process (``SIGKILL`` on POSIX) instead of asking it to end
                (``SIGTERM``); on Windows psutil terminates the process either way.

        Raises:
            NativeCallError: No process has ``pid``, or it could not be stopped (a
                :class:`NativeAccessDeniedError` when access is denied).

        Example:
            >>> callable(build_ps().stop_process)
            True
        """
        self._process_control.stop_process(pid, force=force)

    def wait_process(self, pid: int, *, timeout: float | None = None) -> int | None:
        """Wait for a process to exit; return its exit code (native, portable; like Wait-Process).

        A PID that is already gone counts as exited, so calling this right after
        :meth:`stop_process` cannot fail on the race.

        Args:
            pid: The process id.
            timeout: Seconds to wait; None (default) waits without a limit.

        Returns:
            The exit code, or None when the process was already gone, vanished during the wait,
            or psutil cannot report its code.

        Raises:
            OperationTimeoutError: The process was still running after ``timeout`` seconds;
                ``.timeout`` holds the limit.

        Example:
            >>> callable(build_ps().wait_process)
            True
        """
        return self._process_control.wait_process(pid, timeout=timeout)

    def restart_computer(self, *, delay_seconds: int = 0, force: bool = True) -> None:
        """Restart the computer (native, Windows, **destructive**; like Restart-Computer).

        Runs Windows ``shutdown /r``.

        Args:
            delay_seconds: Seconds before it happens, passed as ``shutdown /t`` (default 0).
            force: Close running applications without warning (``shutdown /f``; default True).

        Raises:
            PlatformUnsupportedError: Not Windows (use ``sudo`` on POSIX).
            NativeCallError: ``shutdown`` could not be started or exited nonzero.

        Example:
            >>> callable(build_ps().restart_computer)
            True
        """
        self._process_control.restart_computer(delay_seconds=delay_seconds, force=force)

    def stop_computer(self, *, delay_seconds: int = 0, force: bool = True) -> None:
        """Shut the computer down (native, Windows, **destructive**; like Stop-Computer).

        Runs Windows ``shutdown /s``.

        Args:
            delay_seconds: Seconds before it happens, passed as ``shutdown /t`` (default 0).
            force: Close running applications without warning (``shutdown /f``; default True).

        Raises:
            PlatformUnsupportedError: Not Windows (use ``sudo`` on POSIX).
            NativeCallError: ``shutdown`` could not be started or exited nonzero.

        Example:
            >>> callable(build_ps().stop_computer)
            True
        """
        self._process_control.stop_computer(delay_seconds=delay_seconds, force=force)

    def get_hotfix(self) -> Pipeline[Hotfix]:
        """Return a lazy pipeline over installed Windows updates (native, Windows; like Get-Hotfix).

        Read from ``Win32_QuickFixEngineering`` through the CIM source; nothing is queried until
        the pipeline is iterated, so every error below is raised then.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`Hotfix` records.

        Raises:
            PlatformUnsupportedError: pywin32 is not installed (any OS but Windows).
            NativeCallError: The WMI query failed.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_hotfix(), Pipeline)
            True
        """
        return Pipeline(self._hotfix_source())

    def get_windows_image(self, path: str | Path, *, index: int | None = None) -> Pipeline[WindowsImage]:
        """Return a lazy pipeline over the images of a WIM file (native, portable; like Get-WindowsImage).

        Reads the file's header and XML resource only, so it works on any OS with no DISM.
        Nothing is opened until the pipeline is consumed, so a file error, and a missing
        ``index``, are raised then, not by this call.

        Args:
            path: The ``.wim`` (or split ``.swm`` part) to read.
            index: Return only the image with this 1-based index, like ``-Index``; reading
                stops right after it. ``None`` lists every image.

        Returns:
            A lazy :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`WindowsImage` records,
            in file order. A file whose XML resource is compressed (resource flag 0x04) is
            refused, not decoded.

        Raises:
            ValueError: ``index`` is below 1 (raised by this call).
            NativeCallError: On consumption: the file holds no image ``index``, or it is
                unreadable or not a valid WIM.

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_windows_image("any.wim", index=1), Pipeline)
            True
        """
        if index is not None and index < 1:
            raise ValueError(f"index must be >= 1, got {index}")
        source = Path(path)
        images = self._windows_image_source(source)
        return Pipeline(images if index is None else _only_image(images, index, source))

    def get_windows_update(
        self, *, criteria: str = _DEFAULT_WU_CRITERIA, timeout: float = 300.0
    ) -> Pipeline[WindowsUpdate]:
        """Return a lazy pipeline over a Windows Update search (native, Windows, read-only).

        The search (``Microsoft.Update.Session``) runs in a child process that is killed once
        ``timeout`` seconds pass, because the COM call cannot be interrupted and can sit for
        25+ minutes.  Lazy only in when it starts: the search starts when the pipeline is
        first iterated, and it returns its whole result before the first record is yielded,
        so ``.take(n)`` does not shorten it.

        The child is started with the ``spawn`` method, which re-imports the calling script,
        so a script using this must guard its top level with ``if __name__ == "__main__":``;
        without the guard the child fails at bootstrap (a ``NativeCallError``, "worker exited
        without a result").

        Args:
            criteria: A WUA search criteria string, passed verbatim to
                ``IUpdateSearcher.Search`` (default ``"IsInstalled=0 and Type='Software'"``, the
                software updates not installed; ``"IsInstalled=1"`` lists the installed ones).
            timeout: Seconds before the search is abandoned, in ``(0, 2147483]``.

        Returns:
            A :class:`~pwshpy.domain.pipeline.Pipeline` of :class:`WindowsUpdate` records.

        Raises:
            ValueError: ``timeout`` is outside ``(0, 2147483]`` (raised at the call).
            PlatformUnsupportedError: Off Windows (raised on iteration).
            OperationTimeoutError: The search outlived ``timeout`` (raised on iteration). A
                ``NativeCallError`` subclass, so catch it first to tell it apart.
            NativeCallError: The Windows Update agent failed (its HRESULT is in the message).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().get_windows_update(), Pipeline)
            True
        """
        _check_wait_limit(timeout)
        return Pipeline(self._windows_update_source(criteria, timeout=timeout))

    def get_computer_info(self) -> ComputerInfo:
        """Return a summary of the local computer (native, portable; like Get-ComputerInfo).

        Hostname, OS, architecture, CPU count and memory come from the stdlib and psutil. On
        Windows the manufacturer and model are read from ``Win32_ComputerSystem``, best effort:
        a WMI failure leaves them empty instead of failing the call.

        Returns:
            A :class:`ComputerInfo` (``manufacturer`` and ``model`` empty off Windows).

        Example:
            >>> from pwshpy.domain.records import ComputerInfo
            >>> isinstance(build_ps().get_computer_info(), ComputerInfo)
            True
        """
        return self._computer_info_source()

    def get_current_identity(self) -> CurrentIdentity:
        """Return the account this process runs as, from its token (native, portable; like ``whoami /user``).

        Read from the process token on Windows (SID, integrity level, elevation) and from the
        effective uid on POSIX - never from ``USERNAME``/``USERDOMAIN``, which name the computer
        account inside a SYSTEM scheduled task.  Match on ``sid`` (a SID, or the uid as a
        string); the names are display only and localized for built-in accounts.

        Returns:
            A :class:`CurrentIdentity`: ``sid``, ``account``, ``is_system``, ``elevated`` and,
            on Windows, ``integrity_level``.

        Raises:
            NativeCallError: The process token (or the user database) could not be read.

        Example:
            >>> from pwshpy.domain.records import CurrentIdentity
            >>> isinstance(build_ps().get_current_identity(), CurrentIdentity)
            True
        """
        return self._current_identity_source()

    def get_known_folder(self, folder: KnownFolder | str) -> Path | None:
        """Return a well-known folder as pwsh's ``[Environment]::GetFolderPath`` names it (native, portable).

        Windows asks the shell (``SHGetKnownFolderPath``); Linux follows the XDG user dirs and
        macOS the CoreFoundation home, as .NET does there.  The answer is the folder pwsh
        reports on the same machine, or ``None`` where pwsh reports ``""``.

        Args:
            folder: A :class:`KnownFolder` member, or its value: the .NET ``SpecialFolder``
                name (``"Desktop"``, ``"MyDocuments"``, ...), matched exactly, case included.

        Returns:
            The folder's path, or ``None`` when it is missing or not readable.

        Raises:
            ValueError: ``folder`` is neither a member nor a member's value.
            PlatformUnsupportedError: ``KnownFolder.PROGRAMS`` on Linux or macOS, where .NET maps
                it to no folder.
            NativeCallError: Windows: ``SHGetKnownFolderPath`` failed with anything but "not
                found" (the message carries the HRESULT).

        Example:
            >>> found = build_ps().get_known_folder(KnownFolder.PROGRAM_DATA)
            >>> found is None or found.is_absolute()
            True
            >>> build_ps().get_known_folder("CommonApplicationData") == found
            True
        """
        return self._known_folder_source(KnownFolder(folder))

    def new_shortcut(
        self,
        path: str | os.PathLike[str],
        *,
        target: str,
        arguments: Sequence[str] = (),
        working_directory: str | None = None,
        icon: str | None = None,
        icon_index: int = 0,
        description: str | None = None,
        run_as_administrator: bool = False,
        force: bool = False,
    ) -> ShortcutInfo:
        r"""Write a Windows shortcut (``.lnk``) and return what Windows stored (native, Windows, **mutating**).

        Like WScript's ``CreateShortcut(...).Save()``, through ``IShellLinkW`` so non-ASCII text
        survives.  ``path`` is made absolute against the working directory first, so the
        259-character limit applies to the path Windows receives.  No directory is created.

        Args:
            path: The ``.lnk`` file to write; relative paths resolve against the working directory.
            target: What the shortcut runs or opens: an absolute Windows path, or one starting
                with an environment reference (``%SystemRoot%\notepad.exe``).
            arguments: The argv passed to ``target``, stored as one command line joined with
                :func:`subprocess.list2cmdline`; a bare ``str`` is refused, never split.
            working_directory: The directory ``target`` starts in.
            icon: The file the icon is taken from.
            icon_index: Which icon in ``icon`` (negative selects a resource id).
            description: The tooltip Explorer shows.
            run_as_administrator: Start ``target`` elevated when the shortcut is opened (one
                UAC prompt; Explorer's "Run as administrator" box, the link flag
                ``SLDF_RUNAS_USER``); a bool only.
            force: Overwrite an existing file at ``path``.

        Returns:
            The shortcut as Windows reads it back (``target`` with environment references expanded).

        Raises:
            PlatformUnsupportedError: Not Windows (raised before anything is checked or touched).
            ConfigurationError: A value breaks the shortcut rules (a relative target, a field too
                long, NUL/CR/LF in a field, a path not ending in ``.lnk``, a non-bool
                ``run_as_administrator``); nothing is written.
            NativeCallError: ``path`` exists and ``force`` is False, or the shell failed (e.g. a
                missing directory); the message names the HRESULT.  This includes a ``target``
                whose environment reference expands past 259 characters: its raw text passes
                the checks above, but Windows refuses it at ``SetPath`` with E_FAIL
                (0x80004005) and no file is written (measured with LongPathsEnabled=0).  No
                portable pre-check exists, because the expansion depends on the host.  When
                reading the saved shortcut back fails, the file at ``path`` may already be
                written.

        Example:
            A target that is not an absolute path is refused before anything is written (off
            Windows the call itself is refused):

            >>> from pwshpy.domain.errors import ConfigurationError
            >>> try:
            ...     build_ps().new_shortcut("C:\\Tool.lnk", target="notepad.exe")
            ... except (ConfigurationError, PlatformUnsupportedError):
            ...     print("refused")
            refused
        """
        controller = self._require_shortcuts("new_shortcut")
        spec = ShortcutSpec(
            path=_absolute_windows_path(path),
            target=target,
            arguments=arguments,
            working_directory=working_directory,
            icon=icon,
            icon_index=icon_index,
            description=description,
            run_as_administrator=run_as_administrator,
        )
        return controller.create(spec, force=force)

    def get_shortcut(self, path: str | os.PathLike[str]) -> ShortcutInfo:
        r"""Return the fields of a Windows shortcut (``.lnk``) as Windows reads them (native, Windows).

        Args:
            path: The ``.lnk`` file; relative paths resolve against the working directory.

        Returns:
            The shortcut; an unset field reads ``""`` (an unset icon index ``0``), and
            ``target`` comes back with environment references expanded.

        Raises:
            PlatformUnsupportedError: Not Windows (raised before the file is looked at).
            NativeCallError: ``path`` is missing, empty or no shortcut, or the shell failed (the
                message names the HRESULT).

        Example:
            A missing file is refused (off Windows the call itself is):

            >>> try:
            ...     build_ps().get_shortcut("C:\\pwshpy-missing\\Tool.lnk")
            ... except (NativeCallError, PlatformUnsupportedError):
            ...     print("refused")
            refused
        """
        return self._require_shortcuts("get_shortcut").read(_absolute_windows_path(path))

    def _require_shortcuts(self, verb: str) -> ShortcutController:
        """Return the shortcut controller, or refuse ``verb`` on an OS that has none."""
        if self._shortcut_controller is None:
            raise PlatformUnsupportedError(
                f"ps.{verb} needs Windows: shortcuts (.lnk) are a Windows shell feature, "
                f"not available on {sys.platform}"
            )
        return self._shortcut_controller

    def get_credential(
        self, username: str | None = None, *, prompt: str = "Password: ", target: str = ""
    ) -> Credential:
        """Interactively read a credential without echoing the password (native, portable).

        The typed equivalent of ``Get-Credential``; the returned
        :class:`~pwshpy.domain.records.Credential` masks its secret in repr/JSON
        (retrieve it with ``.secret.get_secret_value()``).

        Args:
            username: The account name; None (default) asks for it on stdin (``Username: ``).
            prompt: The password prompt text (default ``"Password: "``).
            target: A label stored on the record (e.g. the resource it unlocks); default ``""``.

        Returns:
            A :class:`~pwshpy.domain.records.Credential` with the secret masked.

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

        A credential already stored under ``target`` is overwritten.

        Args:
            target: The name the credential is stored and looked up under.
            username: The account name stored with it.
            secret: The password or token; it is masked on the returned record.

        Returns:
            The stored :class:`~pwshpy.domain.records.Credential` (built from the arguments).

        Raises:
            PlatformUnsupportedError: No credential vault on this OS (macOS, which has no
                Secret Service).
            NativeCallError: The vault call failed; on Linux also no session D-Bus, no Secret
                Service, no default collection, or a locked keyring (no headless unlock prompt).

        Example:
            >>> callable(build_ps().save_credential)
            True
        """
        return self._credential_store.save(target, username, secret)

    def load_credential(self, target: str) -> Credential | None:
        """Read a stored credential by target; ``None`` if absent (native, portable).

        Args:
            target: The name the credential was stored under.

        Returns:
            The :class:`~pwshpy.domain.records.Credential`, or None when nothing is stored under
            ``target``.

        Raises:
            PlatformUnsupportedError: No credential vault on this OS (macOS, which has no
                Secret Service).
            NativeCallError: The vault call failed; on Linux also no session D-Bus, no Secret
                Service, or a matching keyring item that is locked (no headless unlock prompt).

        Example:
            >>> callable(build_ps().load_credential)
            True
        """
        return self._credential_store.load(target)

    def delete_credential(self, target: str) -> None:
        """Delete a stored credential by target (native, portable, **mutating**).

        On Linux every keyring item pwshpy stored under ``target`` is deleted, and nothing
        stored is not an error.

        Args:
            target: The name the credential was stored under.

        Raises:
            PlatformUnsupportedError: No credential vault on this OS (macOS, which has no
                Secret Service).
            NativeCallError: The vault call failed; on Linux also no session D-Bus, no Secret
                Service, or a matching keyring item that is locked (no headless unlock prompt);
                on Windows also nothing stored under ``target``.

        Example:
            >>> callable(build_ps().delete_credential)
            True
        """
        self._credential_store.delete(target)

    def is_elevated(self) -> bool:
        """Return whether this process has administrative (elevated) rights (native, portable).

        Cross-platform: Windows token elevation, or POSIX ``euid == 0`` - none of
        PowerShell's ``WindowsPrincipal``/``IsInRole`` boilerplate.

        Returns:
            True when elevated: the token's elevation flag on Windows, ``euid == 0`` on POSIX.

        Raises:
            NativeCallError: Windows: the process token could not be read.

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
        working directory - no ``System32`` surprise); POSIX (Linux/macOS) asks for the
        password with ``sudo -v`` and then re-execs the command under ``sudo``. ``argv``
        is forwarded in both cases.

        Args:
            argv: The arguments for the relaunched program, after the executable. None, with
                no ``executable`` either, reproduces this invocation (a frozen or console-script
                executable, ``python -m package`` or ``python script.py``); with only
                ``executable`` given, the current ``sys.argv`` is passed to it.
            executable: The program to run elevated; None uses the detected one, or the current
                interpreter when only ``argv`` is given.
            cwd: The child's working directory; None (default) keeps the current one.
            wait: Wait for the child and return its exit code (default); False returns None
                right after the launch.

        Returns:
            ``0`` when already elevated (nothing is launched); the child's exit code when
            ``wait``; None when not ``wait``.

        Raises:
            ElevationDeclinedError: The UAC prompt was refused, or sudo did not grant root.
            NativeCallError: The relaunch could not be started.

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
        windows_image_source=_iter_wim_images,
        windows_update_source=_search_updates,
        computer_info_source=_get_computer_info,
        # The current identity is portable: the process token on Windows, the euid + pwd on POSIX.
        current_identity_source=_windows_current_identity if _ON_WINDOWS else _posix_current_identity,
        # Known folders are portable: SHGetKnownFolderPath on Windows, the .NET XDG/macOS rules elsewhere.
        known_folder_source=_windows_known_folder if _ON_WINDOWS else _posix_known_folder,
        # Shortcuts (.lnk) are Windows-only: no controller elsewhere, so the facade refuses up front.
        shortcut_controller=_NativeShortcutController() if _ON_WINDOWS else None,
        elevation_check=_is_elevated,
        elevator=_elevate,
        script_packer=_pack_script,
        script_unpacker=_unpack_script,
    )


#: Default production facade for ergonomic ``from pwshpy import ps`` usage.
ps = build_ps()


__all__ = ["Ps", "build_ps", "ps"]
