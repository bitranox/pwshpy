"""The WIM oracle's row mapping: how a ``Get-WindowsImage`` row becomes the tuple pwshpy must equal.

The oracle itself runs only on Windows with a real image; the mapping is pure, so it is pinned
here on every OS. The point is that the one documented difference stays NARROW: only the
all-zero row (architecture 0 with Version ``0.0.0.0``) means "no architecture in the XML".
"""

from __future__ import annotations

import pytest
from wim_oracle_rows import ARCH_BY_NUMBER, NO_ARCHITECTURE_VERSION, expected_from_get_windowsimage

from pwshpy import ImageArchitecture


def _row(*, arch: int, version: str, build: int = 0, sp_build: int = 0) -> dict[str, object]:
    return {"index": 1, "name": "img", "build": build, "sp_build": sp_build, "arch": arch, "version": version}


@pytest.mark.os_agnostic
def test_the_all_zero_row_is_an_image_with_no_architecture() -> None:
    """Measured on image 1 of an x64 install.esd: arch 0 and 0.0.0.0, pwshpy reports Unknown and ''."""
    assert expected_from_get_windowsimage(_row(arch=0, version=NO_ARCHITECTURE_VERSION)) == (
        1,
        "img",
        0,
        0,
        "Unknown",
        "",
    )


@pytest.mark.os_agnostic
def test_a_real_x86_image_stays_x86() -> None:
    """Arch 0 with a real version is PROCESSOR_ARCHITECTURE_INTEL: pwshpy saying Unknown must not match."""
    row = _row(arch=0, version="10.0.19041.1", build=19041, sp_build=1)
    expected = expected_from_get_windowsimage(row)
    assert expected == (1, "img", 19041, 1, "x86", "10.0.19041.1")
    assert expected != (1, "img", 19041, 1, "Unknown", "")


@pytest.mark.os_agnostic
def test_a_zero_version_alone_does_not_hide_an_architecture() -> None:
    """Only arch 0 AND 0.0.0.0 together is the no-architecture row; an x64 0.0.0.0 stays x64."""
    row = _row(arch=9, version=NO_ARCHITECTURE_VERSION)
    assert expected_from_get_windowsimage(row) == (1, "img", 0, 0, "x64", NO_ARCHITECTURE_VERSION)


@pytest.mark.os_agnostic
def test_a_measured_x64_row_maps_to_the_record_spelling() -> None:
    """Images 2-10 of the measured install.esd: arch 9, Version 10.0.26100.4349."""
    row = _row(arch=9, version="10.0.26100.4349", build=26100, sp_build=4349)
    assert expected_from_get_windowsimage(row) == (1, "img", 26100, 4349, "x64", "10.0.26100.4349")


@pytest.mark.os_agnostic
def test_every_oracle_spelling_is_an_image_architecture_value() -> None:
    """The oracle compares against ``architecture.value``, so each spelling must be one."""
    values = {member.value for member in ImageArchitecture}
    assert set(ARCH_BY_NUMBER.values()) <= values
