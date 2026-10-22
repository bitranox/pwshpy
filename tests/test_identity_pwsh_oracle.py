"""Current-identity oracle: pwshpy vs the real .NET identity and ``whoami`` on Windows.

The read-only checks compare ``ps.get_current_identity()`` against
``[Security.Principal.WindowsIdentity]::GetCurrent()`` (SID, IsSystem, the Administrator
role) and against ``whoami /user`` / ``whoami /groups`` in CSV form, keyed on the SID
column, which is the same on every OS language.  ``local_only`` + ``os_windows``.

The SYSTEM scheduled-task test is host-MUTATING (it registers and runs a task) and is
marked ``mutating``: it runs only on the disposable throwaway VM, never the dev box.
"""

from __future__ import annotations

import contextlib
import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from pwshpy import ps
from pwshpy.adapters.native.marshal import to_integrity_level
from pwshpy.composition import build_ps
from pwshpy.domain.enums import IntegrityLevel, WellKnownSid
from pwshpy.domain.errors import PwshPyError
from pwshpy.domain.scheduling import TaskPrincipal

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; guards a stray run
    pytest.skip("the current-identity oracle is Windows-only", allow_module_level=True)

_MANDATORY_LABEL = re.compile(r"S-1-16-([0-9]+)")


def _pwsh_exe() -> str:
    candidate = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    return str(candidate) if candidate.exists() else "powershell"


def _run_ps_json(script: str) -> Any:
    completed = subprocess.run(  # noqa: S603 - fixed pwsh argv with a constant script; test oracle only
        [_pwsh_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return json.loads(completed.stdout)


def _whoami_csv(*args: str) -> list[list[str]]:
    # whoami writes the console's OEM code page; only the SID columns are compared exactly.
    completed = subprocess.run(  # noqa: S603 - fixed whoami argv; test oracle only
        ["whoami", *args, "/fo", "csv", "/nh"],  # noqa: S607 - whoami from System32 via PATH
        capture_output=True,
        text=True,
        encoding="oem",
        errors="replace",
        check=True,
    )
    return [row for row in csv.reader(completed.stdout.splitlines()) if row]


def test_sid_and_system_flag_match_windows_identity() -> None:
    """The token SID and IsSystem equal .NET's WindowsIdentity.GetCurrent()."""
    theirs = _run_ps_json(
        "$i = [Security.Principal.WindowsIdentity]::GetCurrent();"
        " @{ sid = $i.User.Value; system = $i.IsSystem } | ConvertTo-Json -Compress"
    )
    ours = ps.get_current_identity()
    assert ours.sid == theirs["sid"]
    assert ours.is_system is bool(theirs["system"])


def test_elevation_matches_the_administrator_role() -> None:
    """``elevated`` equals IsInRole(Administrator).

    For a member of Administrators the filtered (non-elevated) token holds that group as
    deny-only, so IsInRole is true exactly when the token is elevated; for any other account
    both are false.
    """
    theirs = _run_ps_json(
        "([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
        ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) | ConvertTo-Json -Compress"
    )
    assert ps.get_current_identity().elevated is bool(theirs)


def test_account_and_sid_match_whoami_user() -> None:
    """``whoami /user`` names the same SID, and the same account (whoami lower-cases it)."""
    [[account, sid]] = _whoami_csv("/user")
    ours = ps.get_current_identity()
    assert ours.sid == sid
    assert ours.account.casefold() == account.casefold()


def test_integrity_level_matches_whoami_groups_label() -> None:
    """The mandatory label in ``whoami /groups`` (an S-1-16-<rid> row) gives the same level."""
    labels = [
        int(match.group(1)) for row in _whoami_csv("/groups") for match in map(_MANDATORY_LABEL.fullmatch, row) if match
    ]
    assert len(labels) == 1, labels
    level = ps.get_current_identity().integrity_level
    assert level is to_integrity_level(labels[0])
    assert level is not IntegrityLevel.UNTRUSTED


# --- Host-mutating: the throwaway VM only ------------------------------------------------------

# A root-level name: register_scheduled_task does not create a missing task folder.
_TASK = "\\pwshpy_test_x10_identity"

# Writes the record plus the environment's view of the user, then renames into place so the
# waiting test never reads a half-written file.  Any failure is written too, so the test
# reports it instead of waiting out its deadline.
_PROBE = """\
import json, os, sys
try:
    from pwshpy import ps
    payload = {"identity": ps.get_current_identity().to_dict()}
except Exception as exc:
    payload = {"error": f"{type(exc).__name__}: {exc}"}
payload["username"] = os.environ.get("USERNAME", "")
payload["userdomain"] = os.environ.get("USERDOMAIN", "")
with open(sys.argv[1] + ".tmp", "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
"""


@pytest.mark.mutating
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only scheduled-task mutation; skip off Windows")
def test_a_system_scheduled_task_reads_local_system(tmp_path: Path) -> None:
    """Inside a SYSTEM task the verb reads S-1-5-18, where USERNAME names the computer account."""
    probe = tmp_path / "identity_probe.py"
    result = tmp_path / "identity.json"
    probe.write_text(_PROBE, encoding="utf-8")
    facade = build_ps()
    with contextlib.suppress(PwshPyError):
        facade.unregister_scheduled_task(_TASK)
    try:
        facade.register_scheduled_task(
            _TASK,
            program=sys.executable,
            arguments=subprocess.list2cmdline([str(probe), str(result)]),
            description="pwshpy scratch: current identity under SYSTEM",
            principal=TaskPrincipal.system(),
        )
        facade.start_scheduled_task(_TASK)
        deadline = time.monotonic() + 120
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
    finally:
        with contextlib.suppress(PwshPyError):
            facade.unregister_scheduled_task(_TASK)
    assert result.exists(), "the SYSTEM task wrote no result within 120 s"
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert "error" not in payload, payload
    seen_as = f"USERNAME={payload['username']!r} USERDOMAIN={payload['userdomain']!r}"
    identity = payload["identity"]
    assert identity["sid"] == WellKnownSid.LOCAL_SYSTEM.value, seen_as
    assert identity["is_system"] is True, seen_as
    assert identity["elevated"] is True, seen_as
    assert identity["integrity_level"] == IntegrityLevel.SYSTEM.value, seen_as
