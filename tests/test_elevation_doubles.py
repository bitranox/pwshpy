"""Tests of the shared elevation test doubles themselves (tests/elevation_doubles.py).

A planted interpreter that fails to record its start makes every "the planted python never ran"
assertion pass vacuously, so the recorder is checked against interpreters that are known to make a
naive recorder miss.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from elevation_doubles import write_planted_venv

#: Debian and Ubuntu ship a ``sitecustomize.py`` in the system Python's own library directory; it is
#: found before one in a venv's site-packages, so a venv built from such a Python must not rely on one.
_DISTRO_PYTHONS = sorted(
    f"/usr/bin/python3.{minor}"
    for minor in range(8, 20)
    if Path(f"/usr/bin/python3.{minor}").is_file() and Path(f"/usr/lib/python3.{minor}/sitecustomize.py").is_file()
)


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


@pytest.mark.os_agnostic
def test_a_planted_venv_records_a_start_under_isolated_mode(tmp_path: Path) -> None:
    """The planted venv's python records an isolated (``-I``) start, the way uv queries it."""
    marker = tmp_path / "ran.txt"
    root = write_planted_venv(tmp_path / ".venv", marker)
    subprocess.run([str(_venv_python(root)), "-I", "-c", "pass"], check=True, timeout=60)  # noqa: S603
    assert marker.is_file()


@pytest.mark.os_linux
@pytest.mark.skipif(not _DISTRO_PYTHONS, reason="needs a distro Python that ships its own sitecustomize.py")
@pytest.mark.parametrize("interpreter", _DISTRO_PYTHONS or ["none"])
def test_a_venv_built_from_a_distro_python_still_records_its_start(tmp_path: Path, interpreter: str) -> None:
    """[93]: a distro sitecustomize.py shadows a venv one, so the recorder must not be a sitecustomize."""
    marker = tmp_path / "ran.txt"
    root = write_planted_venv(tmp_path / ".venv", marker, interpreter=interpreter)
    subprocess.run([str(_venv_python(root)), "-I", "-c", "pass"], check=True, timeout=60)  # noqa: S603
    assert marker.is_file(), f"a venv built from {interpreter} started without recording it"
