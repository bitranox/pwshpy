"""[132] Listing a task folder by the path its records carry, on a real Task Scheduler.

Every record's ``task_path`` ends in a backslash (``\\Microsoft\\Windows\\Defrag\\``), and the real
GetFolder refuses that form for a non-root folder (ERROR_INVALID_NAME 0x8007007B, measured on
a Windows 11 VM), so passing a record's ``task_path`` back to the listing raised. Read-only:
``local_only`` + ``os_windows``. Compared with ``Get-ScheduledTask -TaskPath`` by task name.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pwshpy import ps

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; guards a stray run
    pytest.skip("the Task Scheduler is Windows-only", allow_module_level=True)

#: A folder every Windows client and server installs, holding ScheduledDefrag.
_DEFRAG = "\\Microsoft\\Windows\\Defrag\\"


def _pwsh_exe() -> str:
    candidate = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    return str(candidate) if candidate.exists() else "powershell"


def _cmdlet_task_names(task_path: str) -> set[str]:
    script = (
        f"@(Get-ScheduledTask -TaskPath '{task_path}' | Where-Object TaskPath -eq '{task_path}' "
        "| Select-Object -ExpandProperty TaskName) | ConvertTo-Json -AsArray"
    )
    completed = subprocess.run(  # noqa: S603 - fixed pwsh argv with a constant script; test oracle only
        [_pwsh_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return {str(name) for name in json.loads(completed.stdout)}


def test_a_folder_listed_by_its_records_task_path_matches_get_scheduledtask() -> None:
    """The trailing-backslash form lists the folder, and its tasks match the cmdlet's."""
    listed = ps.get_scheduled_task(_DEFRAG).to_list()
    assert listed, "the Defrag folder holds ScheduledDefrag on every Windows install"
    assert {info.task_path for info in listed} == {_DEFRAG}
    assert {info.task_name for info in listed} == _cmdlet_task_names(_DEFRAG)


def test_the_trailing_backslash_and_the_bare_folder_list_the_same_tasks() -> None:
    """Both spellings of one folder name the same tasks."""
    with_slash = {info.task_name for info in ps.get_scheduled_task(_DEFRAG).to_list()}
    without = {info.task_name for info in ps.get_scheduled_task(_DEFRAG.rstrip("\\")).to_list()}
    assert with_slash == without
