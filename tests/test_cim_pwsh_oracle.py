"""CIM oracle: pwshpy vs a real Get-CimInstance on Windows - dial in exact behaviour.

Locale-safe: asserts only invariant properties (Version / BuildNumber / DeviceID),
never a localized one (Caption, OSArchitecture).  ``local_only`` + ``os_windows``;
read-only, safe.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from pwshpy import ps

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; guards a stray run
    pytest.skip("the CIM pwsh oracle is Windows-only", allow_module_level=True)


def _pwsh_exe() -> str:
    candidate = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    return str(candidate) if candidate.exists() else "powershell"


def _run_ps_json(script: str) -> object:
    completed = subprocess.run(  # noqa: S603 - fixed pwsh argv with a constant script; test oracle only
        [_pwsh_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_os_class_matches_get_ciminstance() -> None:
    """pwshpy's Win32_OperatingSystem invariant props match Get-CimInstance."""
    instance = ps.get_cim_instance("Win32_OperatingSystem").first()
    assert instance is not None
    gci = cast(
        "dict[str, Any]",
        _run_ps_json(
            "Get-CimInstance Win32_OperatingSystem | Select-Object Version,BuildNumber | ConvertTo-Json -Compress"
        ),
    )
    assert instance.properties.get("Version") == gci["Version"]
    assert str(instance.properties.get("BuildNumber")) == str(gci["BuildNumber"])


def test_where_filter_matches_get_ciminstance() -> None:
    """A WQL WHERE filter selects the same instances as Get-CimInstance -Filter."""
    ours = {c.properties.get("DeviceID") for c in ps.get_cim_instance("Win32_LogicalDisk", where="DriveType = 3")}
    rows = cast(
        "list[dict[str, Any]]",
        _run_ps_json(
            "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType = 3' | Select-Object DeviceID "
            "| ConvertTo-Json -Compress -AsArray"
        ),
    )
    theirs = {str(row["DeviceID"]) for row in rows}
    assert ours == theirs
