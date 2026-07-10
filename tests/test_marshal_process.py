"""Process-name marshaling: Get-Process ``.exe``-stripping semantics (pure, os-agnostic)."""

from __future__ import annotations

import pytest

from pwshpy.adapters.native.marshal import to_process_name


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("raw", "strip_exe", "expected"),
    [
        ("python.exe", True, "python"),
        ("python.EXE", True, "python"),  # trailing .exe is case-insensitive
        ("python.exe", False, "python.exe"),  # off Windows the name is verbatim
        ("bash", True, "bash"),  # no suffix -> unchanged
        ("foo.bar.exe", True, "foo.bar"),  # only the trailing .exe is removed
        ("", True, ""),
        (None, True, ""),
    ],
)
def test_process_name_normalization(raw: str | None, strip_exe: bool, expected: str) -> None:
    """A trailing .exe is stripped only when strip_exe is set; names are never None."""
    assert to_process_name(raw, strip_exe=strip_exe) == expected
