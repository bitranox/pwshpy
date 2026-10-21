"""WIM oracle: pwshpy's pure WIM reader vs a real ``Get-WindowsImage`` on Windows.

Point ``PWSHPY_TEST_WIM`` at a real ``.wim`` (a ``boot.wim`` from an install ISO is small
enough). Locale-safe: compares only fields no host locale can change - index, name (the
image's own XML text), build, SP build and the architecture NUMBER, which DISM reports
as PROCESSOR_ARCHITECTURE_* and this test maps to the spelling the record carries.
``local_only`` + ``os_windows``; ``Get-WindowsImage`` needs an elevated session but only
reads the image, so the test is non-mutating.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from pwshpy import ps

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; guards a stray run
    pytest.skip("the WIM pwsh oracle is Windows-only", allow_module_level=True)

_WIM = os.environ.get("PWSHPY_TEST_WIM", "")
if not _WIM:  # pragma: no cover - needs a real image on the test box
    pytest.skip("set PWSHPY_TEST_WIM to a real .wim to run the WIM oracle", allow_module_level=True)

# PROCESSOR_ARCHITECTURE_* numbers, spelled the way ImageArchitecture spells them.
_ARCH = {0: "x86", 5: "ARM", 6: "ia64", 9: "x64", 12: "ARM64"}

# A per-index call is needed: the list form of Get-WindowsImage leaves Architecture and the
# version fields empty. -InputObject @(...) keeps a one-image result a JSON array on both
# Windows PowerShell 5.1 (no -AsArray) and PowerShell 7.
_SCRIPT = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "$p = $env:PWSHPY_TEST_WIM; "
    "$rows = @(Get-WindowsImage -ImagePath $p | ForEach-Object { "
    "$i = Get-WindowsImage -ImagePath $p -Index $_.ImageIndex; "
    "[pscustomobject]@{ index = [int]$i.ImageIndex; name = [string]$i.ImageName; "
    "build = [int]$i.Build; sp_build = [int]$i.SPBuild; arch = [int]$i.Architecture } }); "
    "ConvertTo-Json -Compress -InputObject $rows"
)


def _pwsh_exe() -> str:
    candidate = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    return str(candidate) if candidate.exists() else "powershell"


def _get_windows_image() -> list[dict[str, Any]]:
    completed = subprocess.run(  # noqa: S603 - fixed pwsh argv with a constant script; test oracle only
        [_pwsh_exe(), "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return cast("list[dict[str, Any]]", json.loads(completed.stdout))


def test_images_match_get_windowsimage() -> None:
    """Every image pwshpy reads matches Get-WindowsImage on the locale-independent fields."""
    theirs = sorted(
        (int(row["index"]), str(row["name"]), int(row["build"]), int(row["sp_build"]), _ARCH.get(int(row["arch"])))
        for row in _get_windows_image()
    )
    ours = sorted(
        (image.index, image.name, image.build, image.sp_build, image.architecture.value)
        for image in ps.get_windows_image(_WIM)
    )
    assert theirs, "Get-WindowsImage listed no images; the oracle would compare nothing"
    assert ours == theirs


def test_index_filter_matches_get_windowsimage_index() -> None:
    """``index=`` selects the same single image as ``Get-WindowsImage -Index``."""
    first = _get_windows_image()[0]
    selected = ps.get_windows_image(_WIM, index=int(first["index"])).to_list()
    assert [(image.index, image.name) for image in selected] == [(int(first["index"]), str(first["name"]))]
