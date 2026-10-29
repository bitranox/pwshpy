"""[132] Listing a task folder by the path its records carry, on a real Task Scheduler.

Every record's ``task_path`` ends in a backslash (``\\Microsoft\\Windows\\Defrag\\``), and the real
GetFolder refuses that form for a non-root folder (ERROR_INVALID_NAME 0x8007007B, measured on
a Windows 11 VM), so passing a record's ``task_path`` back to the listing raised. Read-only:
``local_only`` + ``os_windows``. Compared with ``Get-ScheduledTask -TaskPath`` by task name.
"""

from __future__ import annotations

import json
import os
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


#: pwsh 7 and Windows PowerShell 5.1; the cmdlet oracle runs under each one installed.
_SHELLS = {
    "pwsh7": Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
    "powershell51": Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe",
}


def _cmdlet_task_names(shell: Path, task_path: str) -> set[str]:
    # ConvertTo-Json -AsArray exists only from PowerShell 6, so the array goes in through -InputObject,
    # which keeps a one-task folder a JSON array under 5.1 too. The console encoding is set to UTF-8
    # because 5.1 otherwise writes its pipe in the OEM code page.
    script = (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        f"ConvertTo-Json -Compress -InputObject @(Get-ScheduledTask -TaskPath '{task_path}' "
        f"| Where-Object TaskPath -eq '{task_path}' | Select-Object -ExpandProperty TaskName)"
    )
    completed = subprocess.run(  # noqa: S603 - fixed pwsh argv with a constant script; test oracle only
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return {str(name) for name in json.loads(completed.stdout)}


@pytest.mark.parametrize("shell_name", sorted(_SHELLS))
def test_a_folder_listed_by_its_records_task_path_matches_get_scheduledtask(shell_name: str) -> None:
    """The trailing-backslash form lists the folder, and its tasks match the cmdlet's under each shell."""
    shell = _SHELLS[shell_name]
    if not shell.exists():
        pytest.skip(f"{shell_name} is not installed")
    listed = ps.get_scheduled_task(_DEFRAG).to_list()
    assert listed, "the Defrag folder holds ScheduledDefrag on every Windows install"
    assert {info.task_path for info in listed} == {_DEFRAG}
    assert {info.task_name for info in listed} == _cmdlet_task_names(shell, _DEFRAG)


def test_the_trailing_backslash_and_the_bare_folder_list_the_same_tasks() -> None:
    """Both spellings of one folder name the same tasks."""
    with_slash = {info.task_name for info in ps.get_scheduled_task(_DEFRAG).to_list()}
    without = {info.task_name for info in ps.get_scheduled_task(_DEFRAG.rstrip("\\")).to_list()}
    assert with_slash == without
