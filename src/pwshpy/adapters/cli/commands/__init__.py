"""CLI command implementations.

Collects all subcommand functions and re-exports them for registration
with the root CLI group.

Contents:
    * Info commands from :mod:`.info`
    * Config commands from :mod:`.config`
    * Logging commands from :mod:`.logging`
"""

from __future__ import annotations

from .acl import cli_acl
from .cim import cli_cim
from .config import cli_config, cli_config_deploy, cli_config_generate_examples
from .connections import cli_connections
from .credentials import CREDENTIAL_COMMANDS
from .disks import cli_disks
from .dns import cli_resolve
from .dotnet import DOTNET_COMMANDS
from .elevation import cli_is_elevated
from .environment import cli_env
from .event_log import cli_event_log
from .fileio import FILEIO_COMMANDS
from .filesystem import FILESYSTEM_COMMANDS
from .info import cli_fail, cli_info
from .local_accounts import cli_local_groups, cli_local_users
from .logging import cli_logdemo
from .mutating import MUTATING_COMMANDS
from .netcheck import cli_test_connection
from .networking import NETWORKING_COMMANDS
from .pack import PACK_COMMANDS
from .proc import PROC_COMMANDS
from .processes import cli_processes
from .registry import cli_get_item_property, cli_registry_keys
from .scheduled_tasks import cli_scheduled_tasks
from .services import cli_services
from .system import SYSTEM_COMMANDS
from .uptime import cli_uptime
from .web import WEB_COMMANDS

__all__ = [
    "FILESYSTEM_COMMANDS",
    "MUTATING_COMMANDS",
    "NETWORKING_COMMANDS",
    "PACK_COMMANDS",
    "CREDENTIAL_COMMANDS",
    "FILEIO_COMMANDS",
    "PROC_COMMANDS",
    "SYSTEM_COMMANDS",
    "DOTNET_COMMANDS",
    "WEB_COMMANDS",
    "cli_acl",
    "cli_cim",
    "cli_config",
    "cli_config_deploy",
    "cli_config_generate_examples",
    "cli_connections",
    "cli_disks",
    "cli_env",
    "cli_event_log",
    "cli_fail",
    "cli_info",
    "cli_is_elevated",
    "cli_local_groups",
    "cli_local_users",
    "cli_logdemo",
    "cli_processes",
    "cli_get_item_property",
    "cli_registry_keys",
    "cli_resolve",
    "cli_scheduled_tasks",
    "cli_services",
    "cli_test_connection",
    "cli_uptime",
]
