"""Windows Update oracle: pwshpy's search vs the same COM search driven from PowerShell.

Both sides search ``IsInstalled=1 and Type='Software'``: the installed set is stable between
the two reads, where the not-installed set can change while a scan runs.  Compared by
``UpdateID``, which is locale-independent (titles and category names are localized), and each
update's categories by ``CategoryID`` plus ``Type``, never by the category ``Name``.

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

# UpdateID -> ["<categoryid>|<type>", ...] for every installed update.  The GUID is lower-cased on
# this side too: pwshpy promises lower case, and the oracle must not inherit pwshpy's spelling.
_PWSH_CATEGORY_SCRIPT = (
    "$s = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher(); $m = @{}; "
    "foreach ($u in $s.Search('IsInstalled=1 and Type=''Software''').Updates) { "
    "$m[$u.Identity.UpdateID] = @($u.Categories | ForEach-Object { "
    "$_.CategoryID.ToLowerInvariant() + '|' + $_.Type }) }; "
    "$m | ConvertTo-Json -Compress -Depth 3"
)


def _pwsh_exe() -> str:
    candidate = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    if not candidate.exists():
        pytest.skip("the oracle needs pwsh 7 (ConvertTo-Json -AsArray)")
    return str(candidate)


def _run_pwsh(script: str) -> str:
    """Run ``script`` in pwsh 7 and return its stdout (a non-zero exit fails the test)."""
    completed = subprocess.run(  # noqa: S603 - fixed pwsh argv with a constant script; test oracle only
        [_pwsh_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=_SEARCH_TIMEOUT,
    )
    return completed.stdout


def test_installed_update_ids_match_the_com_search_from_pwsh() -> None:
    """pwshpy's installed-update set equals PowerShell's, by UpdateID; each record is filled in."""
    started = time.monotonic()
    ours = ps.get_windows_update(criteria=_CRITERIA, timeout=_SEARCH_TIMEOUT).to_list()
    ours_seconds = time.monotonic() - started

    started = time.monotonic()
    stdout = _run_pwsh(_PWSH_SCRIPT)
    theirs_seconds = time.monotonic() - started
    theirs = {str(update_id) for update_id in cast("list[object]", json.loads(stdout or "[]"))}

    # Printed so a -s run records the scale and the two durations.
    print(f"pwshpy: {len(ours)} updates in {ours_seconds:.1f}s; pwsh: {len(theirs)} in {theirs_seconds:.1f}s")
    assert {update.update_id for update in ours} == theirs
    assert all(update.is_installed for update in ours)
    assert all(update.title and update.update_id for update in ours)


def test_category_ids_and_types_match_the_com_search_from_pwsh() -> None:
    """Each installed update carries the same (CategoryID, Type) pairs PowerShell reads, in order."""
    ours = {
        update.update_id: [f"{category.category_id}|{category.type}" for category in update.category_details]
        for update in ps.get_windows_update(criteria=_CRITERIA, timeout=_SEARCH_TIMEOUT)
    }
    raw = cast("dict[str, list[object]]", json.loads(_run_pwsh(_PWSH_CATEGORY_SCRIPT) or "{}"))
    theirs = {str(update_id): [str(pair) for pair in pairs] for update_id, pairs in raw.items()}

    distinct = sorted({pair for update_pairs in theirs.values() for pair in update_pairs})
    # Printed so a -s run records which categories the oracle actually compared.
    print(f"{len(theirs)} updates, {len(distinct)} distinct categories: {distinct}")
    assert theirs, "no installed update to compare: the oracle would pass vacuously"
    assert ours == theirs


def test_cli_search_leaves_stderr_empty() -> None:
    """The CLI's search child releases its COM objects cleanly: exit 0 and nothing on stderr.

    A COM reference released after CoUninitialize makes pywin32 print "Win32 exception occurred
    releasing IUnknown" from the child, which shares the CLI's stderr.
    """
    completed = subprocess.run(  # noqa: S603 - this interpreter running pwshpy with fixed arguments
        [sys.executable, "-m", "pwshpy", "get_windows_update", "--criteria", _CRITERIA, "--timeout", "900", "--jsonl"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=_SEARCH_TIMEOUT + 60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip()
    assert completed.stderr == ""
