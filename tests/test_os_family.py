"""The one parse step from a ``sys.platform`` string to the closed OS family set."""

from __future__ import annotations

import pytest

from pwshpy.adapters.native.os_family import OsFamily, os_family_of


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("platform", "family"),
    [
        ("win32", OsFamily.WINDOWS),
        ("linux", OsFamily.LINUX),
        ("linux2", OsFamily.LINUX),
        ("darwin", OsFamily.MACOS),
        ("freebsd14", OsFamily.OTHER),
        ("cygwin", OsFamily.OTHER),
        ("", OsFamily.OTHER),
    ],
)
def test_a_platform_string_parses_to_its_family(platform: str, family: OsFamily) -> None:
    assert os_family_of(platform) is family


@pytest.mark.os_agnostic
@pytest.mark.parametrize("member", list(OsFamily))
def test_a_member_passes_through_unchanged(member: OsFamily) -> None:
    assert os_family_of(member) is member


@pytest.mark.os_agnostic
@pytest.mark.parametrize("value", ["windows", "macos", "other"])
def test_a_member_value_spelled_as_a_plain_string_is_not_a_platform(value: str) -> None:
    """No ``sys.platform`` spells ``macos``: a plain string is always read as a platform value."""
    assert os_family_of(value) is OsFamily.OTHER
