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
``Get-Volume`` reports a drive letter (``C``); and PID 0 is the idle process
(psutil ``System Idle Process`` vs Get-Process ``Idle``), excluded from the
name check as a pseudo-process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from pwshpy import ps

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; guards a stray run
    pytest.skip("the portable pwsh oracle is Windows-only", allow_module_level=True)


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
    mismatches = {pid: (ours[pid], theirs[pid]) for pid in common if ours[pid].lower() != theirs[pid].lower()}
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


def test_env_matches_get_childitem_env() -> None:
    """Env var names (case-insensitive) and their values match Get-ChildItem env:."""
    ours = {e.name.upper(): e.value for e in ps.environment().to_list()}
    ps_env = cast(
        "list[dict[str, Any]]",
        _run_ps_json("Get-ChildItem env: | Select-Object Name,Value | ConvertTo-Json -Compress -AsArray"),
    )
    theirs = {str(e["Name"]).upper(): str(e["Value"]) for e in ps_env}

    assert set(ours) == set(theirs), f"env name sets differ: {sorted(set(ours) ^ set(theirs))[:8]}"
    mismatches = {k: (ours[k], theirs[k]) for k in ours if ours[k] != theirs[k]}
    assert not mismatches, f"env value mismatches: {list(mismatches.items())[:3]}"


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
    """pwshpy uptime matches (Now - LastBootUpTime) within a few seconds."""
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
