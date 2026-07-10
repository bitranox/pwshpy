"""Registry oracle: pwshpy vs a REAL PowerShell on Windows - dial in exact behaviour.

The hermetic ``fake_winreg`` tests prove the logic deterministically in CI; this
test proves the *contract* against Windows itself: for the same key, pwshpy's
typed records must carry the same value names, the same kinds (mapped onto our
``REG_*``), and the same scalar data that real PowerShell reports.

Marked ``local_only`` + ``os_windows`` so ``make test`` (CI) skips it; run it on
a real Windows host with ``make testintegration``.  It is read-only and safe.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from pwshpy.adapters.native.registry import iter_registry_keys, iter_registry_values
from pwshpy.domain.enums import RegistryValueType

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; this guards a stray run
    pytest.skip("the registry pwsh oracle is Windows-only", allow_module_level=True)

# A stable, always-present, world-readable key.
_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
_PARENT = r"SOFTWARE"

# PowerShell RegistryValueKind name -> our RegistryValueType.
_KIND_TO_TYPE: dict[str, RegistryValueType] = {
    "String": RegistryValueType.REG_SZ,
    "ExpandString": RegistryValueType.REG_EXPAND_SZ,
    "Binary": RegistryValueType.REG_BINARY,
    "DWord": RegistryValueType.REG_DWORD,
    "MultiString": RegistryValueType.REG_MULTI_SZ,
    "QWord": RegistryValueType.REG_QWORD,
    "None": RegistryValueType.REG_NONE,
}

# GetValue() expands REG_EXPAND_SZ and returns arrays for binary/multi, so only
# compare raw data for these scalar kinds (names + kinds are compared for all).
_DATA_COMPARABLE = {RegistryValueType.REG_SZ, RegistryValueType.REG_DWORD, RegistryValueType.REG_QWORD}


def _pwsh_exe() -> str:
    candidate = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    return str(candidate) if candidate.exists() else "powershell"


def _run_ps_json(script: str) -> object:
    """Run a PowerShell script and parse its ``ConvertTo-Json`` output."""
    completed = subprocess.run(  # noqa: S603 - fixed pwsh argv with a constant script; test oracle only
        [_pwsh_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_values_match_real_powershell() -> None:
    """Value names, kinds and scalar data from pwshpy match real PowerShell."""
    script = (
        f"$k=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('{_KEY}');"
        "$k.GetValueNames() | ForEach-Object {"
        " [pscustomobject]@{ Name=$_; Kind=$k.GetValueKind($_).ToString(); Value=$k.GetValue($_) }"
        " } | ConvertTo-Json -Compress -AsArray"
    )
    ps_rows = cast("list[dict[str, Any]]", _run_ps_json(script))
    ps_by_name: dict[str, dict[str, Any]] = {str(row["Name"]): row for row in ps_rows}

    ours = {v.name: v for v in iter_registry_values("HKLM/" + _KEY.replace("\\", "/"))}

    assert set(ours) == set(ps_by_name), "pwshpy and PowerShell report the same value names"
    for name, ps_row in ps_by_name.items():
        expected_type = _KIND_TO_TYPE[str(ps_row["Kind"])]
        assert ours[name].type is expected_type, f"{name}: PowerShell kind {ps_row['Kind']!r}"
        if expected_type in _DATA_COMPARABLE:
            assert ours[name].data == ps_row["Value"], f"{name}: data mismatch"


def test_subkeys_match_real_powershell() -> None:
    """Immediate subkey names from pwshpy match real PowerShell's enumeration."""
    script = (
        f"$k=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('{_PARENT}');"
        "$k.GetSubKeyNames() | ConvertTo-Json -Compress -AsArray"
    )
    ps_names = {str(name) for name in cast("list[Any]", _run_ps_json(script))}

    ours = {k.name for k in iter_registry_keys("HKLM/" + _PARENT)}
    assert ours == ps_names
