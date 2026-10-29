"""Oracle: the portable read commands vs real PowerShell on Windows.

Every non-destructive pwshpy command is pinned against its PowerShell
equivalent so pwshpy *behaves the same* as PowerShell (the whole point of a
"Pythonic PowerShell").  These are ``local_only`` + ``os_windows`` (read-only,
safe): ``make test`` skips them, ``make testintegration`` runs them on a real
Windows host.

Live state (process/connection lists, uptime) shifts between the two reads, so
each test compares a stable or tolerant subset - the current PID, the shared
drive letters, a listening port, boot time within a tolerance - not the full
volatile set.  Known representational deltas are handled explicitly: psutil's
``name`` keeps ``.exe`` (stripped to match ``Get-Process`` - see
``marshal.to_process_name``); os.environ upper-cases variable names on Windows
(compared case-insensitively); psutil reports a mountpoint (``C:\\``) where
``Get-Volume`` reports a drive letter (``C``); two pseudo-processes carry a
different name in each: PID 0, the idle process (psutil ``System Idle Process`` vs
Get-Process ``Idle``), and the kernel's memory compression store (psutil
``MemCompression`` vs Get-Process ``Memory Compression``), both excluded from the
name check. pwshpy keeps psutil's names (documented on ``ps.get_process``). And the
pwsh 7 child the env oracle reads rewrites its own PATH, PSModulePath and PATHEXT
at startup, so those three are compared against that rewrite (``pwsh_child_env``),
not against the raw value.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from pwsh_child_env import expected_child_module_path, expected_child_path, expected_child_pathext

from pwshpy import ps

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; guards a stray run
    pytest.skip("the portable pwsh oracle is Windows-only", allow_module_level=True)


_PWSH7 = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")


def _pwsh_exe() -> str:
    return str(_PWSH7) if _PWSH7.exists() else "powershell"


def _run_ps_json(script: str) -> object:
    """Run a PowerShell script and parse its ``ConvertTo-Json`` output."""
    completed = subprocess.run(  # noqa: S603 - fixed pwsh argv with a constant script; test oracle only
        [_pwsh_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


#: Pseudo-processes whose name differs by design, as (psutil name, Get-Process name),
#: lower-cased. Matched by both names, not by PID: the memory compression store gets a
#: different PID on every boot.
_KNOWN_NAME_PAIRS = frozenset({("memcompression", "memory compression")})


def _name_mismatches(ours: dict[int, str], theirs: dict[int, str]) -> dict[int, tuple[str, str]]:
    """Return the shared PIDs whose names differ, minus PID 0 and the known pseudo-process pairs."""
    common = (set(ours) & set(theirs)) - {0}
    pairs = {pid: (ours[pid], theirs[pid]) for pid in common}
    return {
        pid: pair
        for pid, pair in pairs.items()
        if pair[0].lower() != pair[1].lower() and (pair[0].lower(), pair[1].lower()) not in _KNOWN_NAME_PAIRS
    }


def test_processes_match_get_process() -> None:
    """Process names from pwshpy equal Get-Process for the PIDs both snapshots share."""
    ours = {p.pid: p.name for p in ps.get_process().to_list()}
    ps_procs = cast(
        "list[dict[str, Any]]",
        _run_ps_json("Get-Process | Select-Object Id,ProcessName | ConvertTo-Json -Compress -AsArray"),
    )
    theirs = {int(x["Id"]): str(x["ProcessName"]) for x in ps_procs}

    assert os.getpid() in ours, "pwshpy sees the running test process"
    assert os.getpid() in theirs, "Get-Process sees the running test process"

    # PID 0 is the synthetic idle process: psutil names it "System Idle Process",
    # Get-Process names it "Idle" - both valid for a pseudo-process, so exclude it.
    common = (set(ours) & set(theirs)) - {0}
    assert len(common) >= 10, "the two snapshots overlap on most processes"
    mismatches = _name_mismatches(ours, theirs)
    assert not mismatches, f"pwshpy vs Get-Process name mismatches: {list(mismatches.items())[:5]}"


def test_disks_fstype_matches_get_volume() -> None:
    """For each shared drive letter, pwshpy's fstype equals Get-Volume's FileSystem."""
    ours = {
        d.mountpoint[0].upper(): d
        for d in ps.get_volume().to_list()
        if len(d.mountpoint) >= 2 and d.mountpoint[1] == ":"
    }
    vols = cast(
        "list[dict[str, Any]]",
        _run_ps_json(
            "Get-Volume | Where-Object DriveLetter | Select-Object DriveLetter,FileSystem "
            "| ConvertTo-Json -Compress -AsArray"
        ),
    )
    assert "C" in ours, "the system drive C: is enumerated"
    for vol in vols:
        letter = str(vol["DriveLetter"]).upper()
        file_system = vol.get("FileSystem")
        if letter in ours and file_system:  # skip unformatted / unlettered volumes
            assert ours[letter].fstype.upper() == str(file_system).upper(), f"drive {letter} fstype"


#: Run in the oracle child: its env plus what it needs to know about its own startup rewrite.
_ENV_ORACLE_SCRIPT = (
    "$mi = [System.Management.Automation.ModuleIntrinsics]; "
    "$scope = [System.Management.Automation.ModuleIntrinsics+PSModulePathScope]; "
    "[pscustomobject]@{ "
    "ExeHome = [System.IO.Path]::GetDirectoryName([Environment]::ProcessPath); "
    "ModuleInserts = @($mi::GetPSModulePath($scope::User), $mi::GetPSModulePath($scope::Machine), "
    "$mi::GetPSModulePath($scope::Builtin)); "
    "Env = @(Get-ChildItem env: | Select-Object Name,Value) "
    "} | ConvertTo-Json -Compress -Depth 3"
)


def test_env_matches_get_childitem_env() -> None:
    """Env var names (case-insensitive) and values match Get-ChildItem env: in a pwsh 7 child.

    Every variable is compared exactly. PATH, PSModulePath and PATHEXT are compared against the
    value the child derives from ours at its startup (``pwsh_child_env``), not against ours: that
    rewrite is a no-op when pytest runs under pwsh 7 and adds pwsh's entries when it runs under
    Windows PowerShell 5.1 or cmd, which is what made this test pass or fail by launching shell
    alone. PATHEXT gains only pwsh's own ``.CPL``, so any other PATHEXT difference still fails.
    """
    if not _PWSH7.exists():
        pytest.skip("needs pwsh 7: the expected PATH/PSModulePath/PATHEXT rewrite is pwsh 7's own")
    ours = {e.name.upper(): e.value for e in ps.environment().to_list()}
    oracle = cast("dict[str, Any]", _run_ps_json(_ENV_ORACLE_SCRIPT))
    theirs = {str(e["Name"]).upper(): str(e["Value"]) for e in cast("list[dict[str, Any]]", oracle["Env"])}

    expected = dict(ours)
    if "PATH" in ours:
        expected["PATH"] = expected_child_path(ours["PATH"], str(oracle["ExeHome"]))
    if "PSMODULEPATH" in ours:
        inserts = [str(p) for p in cast("list[Any]", oracle["ModuleInserts"]) if p]
        expected["PSMODULEPATH"] = expected_child_module_path(ours["PSMODULEPATH"], inserts)
    # Set even when ours lacks it: pwsh creates PATHEXT as ".CPL" when it inherits none.
    expected["PATHEXT"] = expected_child_pathext(ours.get("PATHEXT"))
    assert set(expected) == set(theirs), f"env name sets differ: {sorted(set(expected) ^ set(theirs))[:8]}"
    mismatches = {k: (expected[k], theirs[k]) for k in expected if expected[k] != theirs[k]}
    assert not mismatches, f"env value mismatches (expected, Get-ChildItem): {list(mismatches.items())[:3]}"


def test_resolve_localhost_matches() -> None:
    """localhost resolution addresses match Resolve-DnsName."""
    ours = sorted(rec.address for rec in ps.resolve_dns_name("localhost").to_list())
    addresses = cast(
        "list[Any]",
        _run_ps_json(
            "Resolve-DnsName localhost -ErrorAction SilentlyContinue "
            "| Select-Object -ExpandProperty IPAddress | ConvertTo-Json -Compress -AsArray"
        ),
    )
    assert ours == sorted(str(a) for a in addresses)


def test_listening_ports_cover_get_nettcpconnection() -> None:
    """pwshpy's TCP listeners include Get-NetTCPConnection's (allowing a couple races)."""
    ours = {
        c.local_port
        for c in ps.get_net_tcp_connection().to_list()
        if c.status.value == "LISTEN" and c.protocol.value == "tcp"
    }
    ports = cast(
        "list[Any]",
        _run_ps_json(
            "Get-NetTCPConnection -State Listen | Select-Object -ExpandProperty LocalPort -Unique "
            "| ConvertTo-Json -Compress -AsArray"
        ),
    )
    theirs = {int(p) for p in ports}
    assert theirs.issubset(ours) or len(theirs & ours) >= len(theirs) - 2, f"listeners diverge: {sorted(theirs - ours)}"


def test_uptime_matches_within_tolerance() -> None:
    """pwshpy get_uptime matches (Now - LastBootUpTime) within a few seconds."""
    ours = ps.get_uptime().uptime_seconds
    theirs = cast(
        "float",
        _run_ps_json(
            "[int64]((Get-Date).ToUniversalTime() - "
            "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime()).TotalSeconds "
            "| ConvertTo-Json"
        ),
    )
    assert abs(ours - float(theirs)) <= 10.0


def test_test_connection_matches_test_connection_cmdlet() -> None:
    """TCP reachability of a listening port matches Test-Connection -TcpPort -Quiet."""
    ours = ps.test_connection("127.0.0.1", port=135, timeout=3.0).reachable
    theirs = cast(
        "bool",
        _run_ps_json("(Test-Connection -TargetName 127.0.0.1 -TcpPort 135 -TimeoutSeconds 3 -Quiet) | ConvertTo-Json"),
    )
    assert ours == bool(theirs)
