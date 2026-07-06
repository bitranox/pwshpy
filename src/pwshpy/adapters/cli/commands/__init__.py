"""CLI command implementations.

Collects all subcommand functions and re-exports them for registration
with the root CLI group.

Contents:
    * Info commands from :mod:`.info`
    * Config commands from :mod:`.config`
    * Logging commands from :mod:`.logging`
"""

from __future__ import annotations

from .config import cli_config, cli_config_deploy, cli_config_generate_examples
from .connections import cli_connections
from .disks import cli_disks
from .dns import cli_resolve
from .environment import cli_env
from .info import cli_fail, cli_info
from .logging import cli_logdemo
from .netcheck import cli_test_connection
from .processes import cli_processes
from .uptime import cli_uptime

__all__ = [
    "cli_config",
    "cli_config_deploy",
    "cli_config_generate_examples",
    "cli_connections",
    "cli_disks",
    "cli_env",
    "cli_fail",
    "cli_info",
    "cli_logdemo",
    "cli_processes",
    "cli_resolve",
    "cli_test_connection",
    "cli_uptime",
]
