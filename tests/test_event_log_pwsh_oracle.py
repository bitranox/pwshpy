"""Event-log oracle: pwshpy vs a real Get-WinEvent on Windows - dial in exact behaviour.

Locale-safe by construction: compares the NUMERIC ``Level`` (Get-WinEvent's
``LevelDisplayName`` is localized) and the ``UserId`` SID, never a translated
string.  ``local_only`` + ``os_windows``; read-only, safe.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from pwshpy import ps
from pwshpy.adapters.native.marshal import to_event_level

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; guards a stray run
    pytest.skip("the event-log pwsh oracle is Windows-only", allow_module_level=True)


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


def test_system_log_matches_get_winevent() -> None:
    """For the events both reads share, pwshpy matches Get-WinEvent id / level / provider / user SID."""
    ours = {e.record_id: e for e in ps.get_win_event("System").take(40).to_list()}
    rows = cast(
        "list[dict[str, Any]]",
        _run_ps_json(
            "Get-WinEvent -LogName System -MaxEvents 40 | Select-Object RecordId,Id,"
            "@{n='LevelInt';e={[int]$_.Level}},ProviderName,@{n='UserId';e={[string]$_.UserId}} "
            "| ConvertTo-Json -Compress -AsArray"
        ),
    )
    theirs = {int(row["RecordId"]): row for row in rows}
    common = set(ours) & set(theirs)
    assert len(common) >= 5, "the two newest-first reads overlap on most events"
    for rid in common:
        mine = ours[rid]
        gs = theirs[rid]
        assert mine.event_id == int(gs["Id"]), f"rid {rid} event_id"
        # Compare on the NUMERIC level (LevelDisplayName is localized), mapped identically.
        assert mine.level is to_event_level(int(gs["LevelInt"])), f"rid {rid} level"
        assert mine.provider_name == str(gs["ProviderName"]), f"rid {rid} provider"
        gs_user = gs.get("UserId")
        assert (mine.user_id or "") == (str(gs_user) if gs_user else ""), f"rid {rid} user SID"
