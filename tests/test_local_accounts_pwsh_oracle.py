"""Local-accounts oracle: pwshpy vs real Get-LocalUser / Get-LocalGroup on Windows.

Keyed on the SID (stable, locale-independent).  The SAM name is compared only
pwshpy-vs-pwsh at runtime (both localize identically on the same box), never
against a hardcoded literal.  ``local_only`` + ``os_windows``; read-only, safe.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from pwshpy import ps
from pwshpy.domain.enums import WellKnownSid

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; guards a stray run
    pytest.skip("the local-accounts pwsh oracle is Windows-only", allow_module_level=True)


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


def test_local_users_match_get_localuser() -> None:
    """pwshpy enumerates the same users as Get-LocalUser, matching name and enabled by SID."""
    ours = {user.sid: user for user in ps.get_local_user().to_list()}
    rows = cast(
        "list[dict[str, Any]]",
        _run_ps_json(
            "Get-LocalUser | Select-Object Name,Enabled,@{n='SID';e={$_.SID.Value}} | ConvertTo-Json -Compress -AsArray"
        ),
    )
    theirs = {str(row["SID"]): row for row in rows}
    assert set(ours) == set(theirs)
    for sid, user in ours.items():
        assert user.name == theirs[sid]["Name"], f"{sid} name"
        assert user.enabled == bool(theirs[sid]["Enabled"]), f"{sid} enabled"


def test_local_groups_match_get_localgroup() -> None:
    """pwshpy enumerates the same groups as Get-LocalGroup (compared by SID)."""
    ours = {group.sid for group in ps.get_local_group().to_list()}
    rows = cast(
        "list[dict[str, Any]]",
        _run_ps_json("Get-LocalGroup | Select-Object @{n='SID';e={$_.SID.Value}} | ConvertTo-Json -Compress -AsArray"),
    )
    theirs = {str(row["SID"]) for row in rows}
    assert ours == theirs


def test_wellknown_administrators_sid_resolves_to_one_group() -> None:
    """The well-known Administrators SID resolves regardless of the localized group name."""
    admins = ps.get_local_group().where(lambda g: g.sid == WellKnownSid.ADMINISTRATORS).to_list()
    assert len(admins) == 1
