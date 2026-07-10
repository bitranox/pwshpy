"""native system inventory - installed hotfixes and a computer summary.

``iter_hotfixes`` (Get-Hotfix) reuses the CIM path (Win32_QuickFixEngineering) and is
Windows-only.  ``get_computer_info`` (Get-ComputerInfo) is portable - hostname / OS /
arch / CPU / memory from the stdlib + psutil, plus manufacturer/model from WMI on
Windows.  (Get-EventLog, the classic API, is covered by ``get_win_event``.)

Contents:
    * :func:`iter_hotfixes` - one :class:`Hotfix` per installed update (Windows).
    * :func:`get_computer_info` - a single :class:`ComputerInfo` summary (portable).
"""

from __future__ import annotations

import os
import platform
import socket
import sys
from collections.abc import Iterator
from typing import Any

import psutil as _psutil_module  # pyright: ignore[reportMissingTypeStubs]

from ...domain.records import ComputerInfo, Hotfix
from .cim import iter_cim

_psutil: Any = _psutil_module


def iter_hotfixes() -> Iterator[Hotfix]:
    """Yield a :class:`Hotfix` for every installed Windows update (like Get-Hotfix).

    Windows-only: raises :class:`~pwshpy.domain.errors.PlatformUnsupportedError` off
    Windows (via the CIM backend).

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import Hotfix
        >>> sys.platform != "win32" or all(isinstance(h, Hotfix) for h in iter_hotfixes())
        True
    """
    for instance in iter_cim("Win32_QuickFixEngineering"):
        props = instance.properties
        yield Hotfix(
            hotfix_id=str(props.get("HotFixID") or ""),
            description=str(props.get("Description") or ""),
            installed_on=str(props.get("InstalledOn") or ""),
            installed_by=str(props.get("InstalledBy") or ""),
        )


def get_computer_info() -> ComputerInfo:
    """Return a :class:`ComputerInfo` summary of the local machine (like Get-ComputerInfo).

    Portable: hostname/OS/arch/CPU/memory come from the stdlib + psutil; on Windows the
    manufacturer/model are added from ``Win32_ComputerSystem`` (best-effort).

    Example:
        >>> from pwshpy.domain.records import ComputerInfo
        >>> isinstance(get_computer_info(), ComputerInfo)
        True
    """
    manufacturer = ""
    model = ""
    if sys.platform == "win32":
        try:
            for instance in iter_cim("Win32_ComputerSystem"):
                manufacturer = str(instance.properties.get("Manufacturer") or "")
                model = str(instance.properties.get("Model") or "")
                break
        except Exception:  # noqa: S110  # nosec B110 - WMI is best-effort; a failure just leaves the fields blank
            pass
    return ComputerInfo(
        hostname=socket.gethostname(),
        os_name=platform.system(),
        os_version=platform.version(),
        architecture=platform.machine(),
        cpu_count=os.cpu_count(),
        total_memory_bytes=int(_psutil.virtual_memory().total),
        manufacturer=manufacturer,
        model=model,
    )


__all__ = ["get_computer_info", "iter_hotfixes"]
