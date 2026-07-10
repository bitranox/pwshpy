"""Scheduled-tasks oracle: pwshpy vs a real Get-ScheduledTask on Windows.

Compares the task set (folder path + name) and the stable ``Enabled`` flag - both
locale-independent.  ``State`` is dynamic (a task may run between reads) so it is
not asserted.  ``local_only`` + ``os_windows``; read-only, safe.
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
    pytest.skip("the scheduled-tasks pwsh oracle is Windows-only", allow_module_level=True)


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


def test_task_set_and_enabled_match_get_scheduledtask() -> None:
    """pwshpy enumerates the same tasks as Get-ScheduledTask, with the same Enabled flag."""
    ours = {task.task_path + task.task_name: task for task in ps.get_scheduled_task().to_list()}
    rows = cast(
        "list[dict[str, Any]]",
        _run_ps_json(
            "Get-ScheduledTask | Select-Object TaskName,TaskPath,@{n='Enabled';e={[bool]$_.Settings.Enabled}} "
            "| ConvertTo-Json -Compress -AsArray"
        ),
    )
    theirs = {str(row["TaskPath"]) + str(row["TaskName"]): row for row in rows}

    common = set(ours) & set(theirs)
    # The two enumerations run moments apart; tolerate a tiny drift from a task appearing/vanishing.
    assert len(common) >= len(theirs) - 3
    for key in common:
        assert ours[key].enabled == bool(theirs[key]["Enabled"]), f"{key} enabled"
