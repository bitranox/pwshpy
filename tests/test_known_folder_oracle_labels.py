"""The macOS known-folder oracle's "pwsh followed ..." label names the right home.

That label is the measurement a macOS CI failure carries (which home .NET used), and macOS runs
only in CI, so the labelling is pinned here on every OS with real ``tmp_path`` directories.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from known_folder_oracle_labels import home_followed


def _homes(tmp_path: Path) -> tuple[Path, Path]:
    decoy = tmp_path / "decoy"
    passwd = tmp_path / "passwd"
    for home in (decoy, passwd):
        (home / "Desktop").mkdir(parents=True)
    return decoy, passwd


@pytest.mark.os_agnostic
def test_answers_under_the_decoy_name_home(tmp_path: Path) -> None:
    decoy, passwd = _homes(tmp_path)

    label = home_followed([decoy / "Desktop", None], {"HOME": decoy, "the passwd entry": passwd})

    assert label == f"HOME ({decoy})"


@pytest.mark.os_agnostic
def test_answers_under_the_passwd_home_name_the_passwd_entry(tmp_path: Path) -> None:
    decoy, passwd = _homes(tmp_path)

    label = home_followed([passwd / "Desktop"], {"HOME": decoy, "the passwd entry": passwd})

    assert label == f"the passwd entry ({passwd})"


@pytest.mark.os_agnostic
def test_answers_under_no_candidate_name_neither(tmp_path: Path) -> None:
    decoy, passwd = _homes(tmp_path)
    fixed = tmp_path / "fixed"

    label = home_followed(
        [tmp_path / "elsewhere" / "Desktop", None],
        {"CFFIXED_USER_HOME": fixed, "the passwd entry": passwd, "HOME": decoy},
    )

    assert label == "neither CFFIXED_USER_HOME nor the passwd entry nor HOME"


@pytest.mark.os_agnostic
def test_the_deepest_home_wins_when_one_candidate_lies_inside_another(tmp_path: Path) -> None:
    passwd = tmp_path / "passwd"
    fixed = passwd / "scratch" / "fixed"
    (fixed / "Desktop").mkdir(parents=True)

    label = home_followed([fixed / "Desktop"], {"CFFIXED_USER_HOME": fixed, "the passwd entry": passwd})

    assert label == f"CFFIXED_USER_HOME ({fixed})"


@pytest.mark.os_agnostic
def test_two_candidates_naming_one_directory_are_both_named(tmp_path: Path) -> None:
    _decoy, passwd = _homes(tmp_path)

    label = home_followed([passwd / "Desktop"], {"the passwd entry": passwd, "HOME": passwd})

    assert label == f"the passwd entry ({passwd}) or HOME ({passwd})"


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="a directory symlink needs a privilege on Windows")
def test_a_symlinked_spelling_is_matched_and_marked(tmp_path: Path) -> None:
    decoy, passwd = _homes(tmp_path)
    link = tmp_path / "link-to-decoy"
    link.symlink_to(decoy, target_is_directory=True)

    label = home_followed([decoy / "Desktop"], {"HOME": link, "the passwd entry": passwd})

    assert label == f"HOME ({decoy.resolve()}) (same directory, different spelling)"
