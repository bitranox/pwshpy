"""native adapters — typed facades over direct OS bindings.

Each module wraps one substrate (``psutil``, ``winreg``, ``win32*``, ``wmi``)
behind a fully-typed surface and marshals its values into domain records via
:mod:`.marshal`.  The portable ``psutil`` slice works on every OS; the win32/wmi
modules are Windows-only.

Contents:
    * :func:`iter_processes` — portable process source (psutil).
    * :func:`iter_connections` — portable network-connection source (psutil).
    * :func:`iter_disks` — portable disk-usage source (psutil).
    * :func:`get_uptime` — portable system uptime (psutil).
    * :func:`resolve` — portable DNS resolver (stdlib socket).
    * :func:`test_connection` — portable TCP reachability probe (stdlib socket).
    * :func:`iter_env` — portable environment-variable source (os.environ).
    * :func:`iter_registry_values` — Windows registry values (lib_registry, win32-gated).
    * :func:`iter_registry_keys` — Windows registry subkeys (lib_registry, win32-gated).
"""

from __future__ import annotations

from .acl import iter_acl
from .acl_control import NativeAclController
from .cim import iter_cim
from .credentials import NativeCredentialStore, prompt_credential
from .dns import resolve
from .elevation import elevate, is_elevated
from .environment import iter_env
from .event_log import iter_event_log
from .event_log_control import NativeEventLogController
from .fileio import write_records, write_text, write_text_stream
from .filesystem import NativeFileSystem
from .inventory import get_computer_info, iter_hotfixes
from .journald import iter_event_log as journald_iter_event_log
from .local_account_control import NativeLocalAccountController
from .local_accounts import iter_local_groups, iter_local_users
from .netcheck import test_connection
from .networking import iter_net_adapters, iter_net_ip_addresses, iter_net_udp_endpoints
from .packer import pack_script, unpack_script
from .posix_account_control import PosixLocalAccountController
from .posix_accounts import iter_local_groups as posix_iter_local_groups
from .posix_accounts import iter_local_users as posix_iter_local_users
from .posix_acl import PosixAclController
from .posix_acl import iter_acl as posix_iter_acl
from .process_control import NativeProcessControl
from .process_exec import run_process
from .psutil_disk import iter_disks
from .psutil_net import iter_connections
from .psutil_process import iter_processes
from .psutil_system import get_uptime
from .registry import iter_registry_keys, iter_registry_values
from .registry_control import NativeRegistryController
from .scheduled_task_control import NativeScheduledTaskController
from .scheduled_tasks import iter_scheduled_tasks
from .secret_service import SecretServiceCredentialStore
from .service_control import NativeServiceController
from .services import iter_services
from .systemd_services import SystemdServiceController
from .systemd_services import iter_services as systemd_iter_services
from .systemd_timers import SystemdScheduledTaskController
from .systemd_timers import iter_timers as systemd_iter_timers
from .web import download_file, invoke_rest_method, invoke_web_request

__all__ = [
    "NativeAclController",
    "PosixAclController",
    "posix_iter_acl",
    "NativeCredentialStore",
    "SecretServiceCredentialStore",
    "NativeEventLogController",
    "NativeFileSystem",
    "NativeProcessControl",
    "NativeLocalAccountController",
    "PosixLocalAccountController",
    "NativeRegistryController",
    "NativeScheduledTaskController",
    "NativeServiceController",
    "elevate",
    "get_computer_info",
    "get_uptime",
    "is_elevated",
    "iter_hotfixes",
    "journald_iter_event_log",
    "iter_acl",
    "iter_cim",
    "iter_connections",
    "iter_disks",
    "iter_env",
    "iter_event_log",
    "iter_local_groups",
    "iter_local_users",
    "iter_net_adapters",
    "iter_net_ip_addresses",
    "iter_net_udp_endpoints",
    "iter_processes",
    "iter_registry_keys",
    "iter_registry_values",
    "iter_scheduled_tasks",
    "download_file",
    "invoke_rest_method",
    "invoke_web_request",
    "iter_services",
    "SystemdServiceController",
    "systemd_iter_services",
    "SystemdScheduledTaskController",
    "systemd_iter_timers",
    "posix_iter_local_groups",
    "posix_iter_local_users",
    "prompt_credential",
    "pack_script",
    "resolve",
    "run_process",
    "unpack_script",
    "test_connection",
    "write_records",
    "write_text",
    "write_text_stream",
]
