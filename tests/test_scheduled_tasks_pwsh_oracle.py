"""Scheduled-tasks oracle: pwshpy vs a real Get-ScheduledTask on Windows.

Compares the task set (folder path + name), the stable ``Enabled`` flag and the [45]
settings - all locale-independent.  ``State`` is dynamic (a task may run between reads), so the listing
does not assert it; the by-name lookup reads one task right after the cmdlet and compares
its state by number.  ``local_only`` + ``os_windows``; read-only, safe.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from elevation_doubles import ps_literal

from pwshpy import ps
from pwshpy.domain.enums import TaskMultipleInstances

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


# Get-ScheduledTask's State is the CIM StateEnum, the same numbers as TASK_STATE (0 Unknown,
# 1 Disabled, 2 Queued, 3 Ready, 4 Running), so comparing [int]State is locale-independent.
_STATE_BY_NUMBER = {0: "Unknown", 1: "Disabled", 2: "Queued", 3: "Ready", 4: "Running"}


def test_lookup_by_name_matches_get_scheduledtask_for_that_task() -> None:
    """[50] ps.get_scheduled_task(path, task_name=name) answers like Get-ScheduledTask -TaskPath -TaskName.

    The task is the first one Get-ScheduledTask lists; a name that cannot exist is an empty answer,
    under strict too.
    """
    first = cast(
        "dict[str, Any]",
        _run_ps_json("Get-ScheduledTask | Select-Object -First 1 TaskPath,TaskName | ConvertTo-Json -Compress"),
    )
    path, name = str(first["TaskPath"]), str(first["TaskName"])
    theirs = cast(
        "dict[str, Any]",
        _run_ps_json(
            f"Get-ScheduledTask -TaskPath {ps_literal(path)} -TaskName {ps_literal(name)} "
            "| Select-Object TaskPath,TaskName,@{n='State';e={[int]$_.State}} | ConvertTo-Json -Compress"
        ),
    )
    (ours,) = ps.get_scheduled_task(path, task_name=name, strict=True).to_list()
    assert (ours.task_path, ours.task_name, ours.state.value) == (
        str(theirs["TaskPath"]),
        str(theirs["TaskName"]),
        _STATE_BY_NUMBER[int(theirs["State"])],
    )
    missing = f"pwshpy-no-such-task-{uuid.uuid4().hex}"
    assert ps.get_scheduled_task(path, task_name=missing, strict=True).to_list() == []


# The settings as Get-ScheduledTask reports them. ExecutionTimeLimit goes through .NET's own ISO 8601
# parser (XmlConvert), independent of pwshpy's; MultipleInstances is the CIM enum's NAME (the same
# words as TaskMultipleInstances' values, never localized). The CIM enum has no StopExisting
# member, so such a task reads "" (measured 2026-10-25): it is compared through that gap.
_SETTINGS_SCRIPT = (
    "Get-ScheduledTask | ForEach-Object { $s = $_.Settings; $limit = $null;"
    " if ($s.ExecutionTimeLimit) { try { $limit = [System.Xml.XmlConvert]::ToTimeSpan($s.ExecutionTimeLimit)"
    ".TotalSeconds } catch { $limit = $null } };"
    " [pscustomobject]@{ Key = $_.TaskPath + $_.TaskName; Disallow = [bool]$s.DisallowStartIfOnBatteries;"
    " Stop = [bool]$s.StopIfGoingOnBatteries; Kill = [bool]$s.AllowHardTerminate;"
    " Demand = [bool]$s.AllowDemandStart; Limit = $limit;"
    " Policy = [string]$s.MultipleInstances } } | ConvertTo-Json -Compress -AsArray"
)


def _expected_settings(row: dict[str, Any]) -> tuple[object, ...]:
    """One Get-ScheduledTask row as the (battery, stop, limit, policy, kill, demand) tuple pwshpy should read."""
    limit = row["Limit"]
    policy = str(row["Policy"])
    return (
        not row["Disallow"],
        bool(row["Stop"]),
        None if limit is None else timedelta(seconds=float(limit)),
        TaskMultipleInstances.STOP_EXISTING if policy == "" else TaskMultipleInstances(policy),
        bool(row["Kill"]),
        bool(row["Demand"]),
    )


def test_settings_match_get_scheduledtask() -> None:
    """[45] every task's effective settings read like Get-ScheduledTask's Settings, OS defaults included."""
    ours = {task.task_path + task.task_name: task for task in ps.get_scheduled_task().to_list()}
    rows = cast("list[dict[str, Any]]", _run_ps_json(_SETTINGS_SCRIPT))
    theirs = {str(row["Key"]): row for row in rows}

    common = set(ours) & set(theirs)
    assert len(common) >= len(theirs) - 3  # the two reads run moments apart
    for key in common:
        settings = ours[key].settings
        assert settings is not None, key
        got = (
            settings.allow_start_if_on_batteries,
            settings.stop_if_going_on_batteries,
            settings.execution_time_limit,
            settings.multiple_instances,
            settings.allow_hard_terminate,
            settings.allow_demand_start,
        )
        assert got == _expected_settings(theirs[key]), key
