"""Services oracle: pwshpy vs a real Get-Service on Windows - dial in exact behaviour.

``local_only`` + ``os_windows``: ``make test`` skips it, ``make testintegration``
runs it on a real Windows host.  Read-only, so safe.  Compares the full service
name set, then every field for a curated set of stable, always-present services.
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
    pytest.skip("the services pwsh oracle is Windows-only", allow_module_level=True)

# Stable, always-present services (status may be Running or Stopped, but pwshpy
# and Get-Service are read near-simultaneously so they agree).
_STABLE = ["Winmgmt", "EventLog", "Dnscache", "Schedule", "LanmanServer", "Spooler"]


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


def _required_set(raw: Any) -> set[str]:
    """Normalize Get-Service's ServicesDependedOn (list / scalar / null) to a set."""
    if raw is None:
        return set()
    if isinstance(raw, list):
        return {str(x) for x in cast("list[Any]", raw)}
    return {str(raw)}


def test_service_name_set_matches_get_service() -> None:
    """pwshpy enumerates exactly the same services as Get-Service."""
    ours = {s.name for s in ps.get_service().to_list()}
    names = cast(
        "list[Any]",
        _run_ps_json("Get-Service | Select-Object -ExpandProperty Name | ConvertTo-Json -Compress -AsArray"),
    )
    assert ours == {str(n) for n in names}


def test_stable_services_fields_match_get_service() -> None:
    """For well-known services, every pwshpy field matches Get-Service."""
    ours = {s.name: s for s in ps.get_service().to_list()}
    present = [name for name in _STABLE if name in ours]
    assert present, "at least some well-known services must exist"

    script = (
        "Get-Service " + ",".join(present) + " | Select-Object Name,DisplayName,"
        "@{n='Status';e={$_.Status.ToString()}},@{n='StartType';e={$_.StartType.ToString()}},"
        "@{n='ServiceType';e={$_.ServiceType.ToString()}},CanStop,CanPauseAndContinue,"
        "@{n='Required';e={@($_.ServicesDependedOn.Name)}} | ConvertTo-Json -Compress -AsArray"
    )
    rows = cast("list[dict[str, Any]]", _run_ps_json(script))
    theirs = {str(row["Name"]): row for row in rows}

    for name in present:
        mine = ours[name]
        gs = theirs[name]
        assert mine.display_name == gs["DisplayName"], f"{name} display_name"
        assert mine.status.value == gs["Status"], f"{name} status"
        if mine.start_type is not None:
            assert mine.start_type.value == gs["StartType"], f"{name} start_type"
        assert mine.can_stop == bool(gs["CanStop"]), f"{name} can_stop"
        assert mine.can_pause_continue == bool(gs["CanPauseAndContinue"]), f"{name} can_pause_continue"
        # Get-Service ServiceType is a flags combo; pwshpy reports the primary kind (always set on Windows).
        assert mine.service_type is not None, f"{name} service_type is set on Windows"
        assert mine.service_type.value in str(gs["ServiceType"]), f"{name} service_type"
        assert set(mine.required_services) == _required_set(gs["Required"]), f"{name} required_services"
