"""ACL oracle: pwshpy vs a real Get-Acl on Windows.

Keyed on trustee SID (stable, locale-independent).  Get-Acl's IdentityReference
cannot translate every principal to a SID (app-package accounts come back null),
so the oracle asserts that every SID Get-Acl *can* translate is present in
pwshpy's ACE set - pwshpy captures the rest too.  ``local_only`` + ``os_windows``.
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
    pytest.skip("the ACL pwsh oracle is Windows-only", allow_module_level=True)

_TARGET = r"C:\Windows"


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


def test_dir_acl_matches_get_acl() -> None:
    """Every translatable Get-Acl ACE (SID / type / rights) is present in pwshpy's ACL."""
    ours = {(e.trustee_sid, e.access_type.value, e.rights) for e in ps.get_acl(_TARGET)}
    rows = cast(
        "list[dict[str, Any]]",
        _run_ps_json(
            "(Get-Acl 'C:\\Windows').Access | ForEach-Object { "
            "$sid = try { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } "
            "catch { $null }; "
            "[pscustomobject]@{SID=$sid; Type=$_.AccessControlType.ToString(); Rights=[int]$_.FileSystemRights} } "
            "| ConvertTo-Json -Compress -AsArray"
        ),
    )
    theirs = [(str(r["SID"]), str(r["Type"]), int(r["Rights"])) for r in rows if r["SID"]]
    assert theirs, "Get-Acl should translate at least some principals to SIDs"
    for entry in theirs:
        assert entry in ours, f"Get-Acl ACE missing from pwshpy: {entry}"


def test_wellknown_administrators_present_in_windows_acl() -> None:
    """The Administrators SID appears in the C:\\Windows DACL regardless of the localized name."""
    admins = ps.get_acl(_TARGET).where(lambda e: e.trustee_sid == WellKnownSid.ADMINISTRATORS).to_list()
    assert admins
