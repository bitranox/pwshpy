"""Map a ``Get-WindowsImage`` row to the tuple a ``WindowsImage`` record must equal (WIM oracle helper).

Pure, so ``test_wim_oracle_rows`` pins it on every OS while the oracle itself
(``test_wim_pwsh_oracle``) runs only on Windows against a real image.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeAlias

__all__ = ["ARCH_BY_NUMBER", "NO_ARCHITECTURE_VERSION", "ImageKey", "expected_from_get_windowsimage"]

# PROCESSOR_ARCHITECTURE_* numbers, spelled the way ImageArchitecture spells them.
ARCH_BY_NUMBER: dict[int, str] = {0: "x86", 5: "ARM", 6: "ia64", 9: "x64", 12: "ARM64"}

# What Get-WindowsImage prints for an image whose XML has no WINDOWS element (measured on
# image 1, "Windows Setup Media", of an x64 install.esd, 2026-10-23): Architecture 0 and
# Version 0.0.0.0. The 0 is a default, not a number read from the file.
NO_ARCHITECTURE_VERSION = "0.0.0.0"  # noqa: S104 - a Windows image version string, never a bind address
_NO_ARCHITECTURE = (0, NO_ARCHITECTURE_VERSION)

# The compared fields, in this order: index, name, build, SP build, architecture, version.
ImageKey: TypeAlias = tuple[int, str, int, int, str | None, str]


def expected_from_get_windowsimage(row: Mapping[str, object]) -> ImageKey:
    """Return the record fields pwshpy must report for one ``Get-WindowsImage -Index`` row.

    pwshpy reports ``Unknown`` and an empty version for an image with no architecture in its
    XML (a 0 there would be PROCESSOR_ARCHITECTURE_INTEL, a guessed x86). Only the row where
    BOTH the architecture is 0 and the version is 0.0.0.0 is mapped to that: a real x86
    image also has architecture 0, but carries a real version, so it still compares as
    ``x86`` and a pwshpy ``Unknown`` for it fails the oracle.

    Args:
        row: One row of the oracle script's JSON (index, name, build, sp_build, arch, version).

    Returns:
        ``(index, name, build, sp_build, architecture, version)``; architecture is ``None``
        for a number ImageArchitecture does not name.

    Examples:
        >>> expected_from_get_windowsimage(
        ...     {"index": 1, "name": "m", "build": 0, "sp_build": 0, "arch": 0, "version": "0.0.0.0"}
        ... )
        (1, 'm', 0, 0, 'Unknown', '')
    """
    arch = int(str(row["arch"]))
    version = str(row["version"])
    key = (int(str(row["index"])), str(row["name"]), int(str(row["build"])), int(str(row["sp_build"])))
    if (arch, version) == _NO_ARCHITECTURE:
        return (*key, "Unknown", "")
    return (*key, ARCH_BY_NUMBER.get(arch), version)
