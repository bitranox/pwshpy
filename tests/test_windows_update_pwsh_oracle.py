"""Windows Update oracle: pwshpy's search vs the same COM search driven from PowerShell.

Both sides search ``IsInstalled=1 and Type='Software'``: the installed set is stable between
the two reads, where the not-installed set can change while a scan runs.  Compared by
``UpdateID``, which is locale-independent (titles and category names are localized).

``local_only`` + ``os_windows`` + ``integration``: needs a Windows host whose Windows Update
agent can be searched, and a real search can take minutes.  Read-only: only ``Search`` is
called, never Download or Install.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from pwshpy import ps

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows, pytest.mark.integration]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; guards a stray run
    pytest.skip("the Windows Update pwsh oracle is Windows-only", allow_module_level=True)

_CRITERIA = "IsInstalled=1 and Type='Software'"
# Generous: one online search was measured at minutes on a slow agent; the bound is a backstop.
_SEARCH_TIMEOUT = 900.0

# Single-quoted PowerShell literal ('' is an escaped quote), so no double quote has to survive
# the Windows command-line quoting on the way to pwsh.
_PWSH_SCRIPT = (
    "$s = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher(); "
    "@($s.Search('IsInstalled=1 and Type=''Software''').Updates | ForEach-Object { $_.Identity.UpdateID }) "
    "| ConvertTo-Json -Compress -AsArray"
)


def _pwsh_exe() -> str:
    candidate = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    if not candidate.exists():
        pytest.skip("the oracle needs pwsh 7 (ConvertTo-Json -AsArray)")
    return str(candidate)


def test_installed_update_ids_match_the_com_search_from_pwsh() -> None:
    """pwshpy's installed-update set equals PowerShell's, by UpdateID; each record is filled in."""
    started = time.monotonic()
    ours = ps.get_windows_update(criteria=_CRITERIA, timeout=_SEARCH_TIMEOUT).to_list()
    ours_seconds = time.monotonic() - started

    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603 - fixed pwsh argv with a constant script; test oracle only
        [_pwsh_exe(), "-NoProfile", "-NonInteractive", "-Command", _PWSH_SCRIPT],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=_SEARCH_TIMEOUT,
    )
    theirs_seconds = time.monotonic() - started
    theirs = {str(update_id) for update_id in cast("list[object]", json.loads(completed.stdout or "[]"))}

    # Printed so a -s run records the scale and the two durations.
    print(f"pwshpy: {len(ours)} updates in {ours_seconds:.1f}s; pwsh: {len(theirs)} in {theirs_seconds:.1f}s")
    assert {update.update_id for update in ours} == theirs
    assert all(update.is_installed for update in ours)
    assert all(update.title and update.update_id for update in ours)
