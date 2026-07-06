"""Tier-A native adapters — typed facades over direct OS bindings.

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
"""

from __future__ import annotations

from .dns import resolve
from .environment import iter_env
from .netcheck import test_connection
from .psutil_disk import iter_disks
from .psutil_net import iter_connections
from .psutil_process import iter_processes
from .psutil_system import get_uptime

__all__ = [
    "get_uptime",
    "iter_connections",
    "iter_disks",
    "iter_env",
    "iter_processes",
    "resolve",
    "test_connection",
]
