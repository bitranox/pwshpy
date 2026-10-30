"""Tests of the shared elevation test doubles themselves (tests/elevation_doubles.py).

A planted interpreter that fails to record its start makes every "the planted python never ran"
assertion pass vacuously, so the recorder is checked against interpreters that are known to make a
naive recorder miss.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from elevation_doubles import (
    default_uv_cache_env,
    managed_python_dir,
    removing_a_new_account_temp_dir,
    write_planted_venv,
)

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


@pytest.mark.os_agnostic
def test_asking_uv_for_the_managed_pythons_writes_no_default_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[111]: the question the helpers ask uv leaves the default cache of whoever runs the suite alone.

    ``uv python find`` caches what it learned about every interpreter it ran, and with no UV_CACHE_DIR
    that cache is the running account's own (root's real one on a root run of the suite)."""
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    for name, value in default_uv_cache_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    # Unwrapped: the session's cached answer was asked before this test pointed the default cache here.
    found = managed_python_dir.__wrapped__()
    if found is None:
        pytest.skip("no uv-managed Python on this host, so uv runs no interpreter to cache")
    written = sorted(path for path in (tmp_path / "default-cache").rglob("*") if path.is_file())
    assert written == [], written


def _recording_uv(directory: Path, record: Path, answer: Path) -> None:
    """A ``uv`` double (POSIX sh) that records each call's cache and download settings and answers.

    One line per call: ``<arguments>|<UV_CACHE_DIR or unset>|<UV_PYTHON_DOWNLOADS or unset>``. ``python
    find`` prints a stand-in interpreter path and ``python dir`` prints ``answer``, so the helper asks
    both questions.
    """
    directory.mkdir()
    double = directory / "uv"
    double.write_text(
        "#!/bin/sh\n"
        f'printf \'%s|%s|%s\\n\' "$*" "${{UV_CACHE_DIR-unset}}" "${{UV_PYTHON_DOWNLOADS-unset}}" >> \'{record}\'\n'
        f"if [ \"$1 $2\" = \"python find\" ]; then printf '%s\\n' '{answer / 'python'}'; fi\n"
        f"if [ \"$1 $2\" = \"python dir\" ]; then printf '%s\\n' '{answer}'; fi\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    double.chmod(0o755)


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the uv double is a POSIX sh script")
def test_every_question_to_uv_names_a_cache_of_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[130]: each call the helpers make to uv names a UV_CACHE_DIR of its own, gone once the answer is
    in, and turns downloads off; so no question writes the default cache of whoever runs the suite.
    A recording double stands in for uv, so this runs on every host, CI runners without a uv-managed
    Python included (where the test above skips)."""
    record = tmp_path / "uv-calls.txt"
    answer = tmp_path / "managed"
    answer.mkdir()
    _recording_uv(tmp_path / "bin", record, answer)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    for name, value in default_uv_cache_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    assert managed_python_dir.__wrapped__() == answer
    calls = [line.split("|") for line in record.read_text(encoding="utf-8").splitlines()]
    assert [arguments.split()[:2] for arguments, _, _ in calls] == [["python", "find"], ["python", "dir"]], calls
    for arguments, cache, downloads in calls:
        assert cache != "unset", arguments
        assert Path(cache).name.startswith("pwshpy-uv-ask-"), (arguments, cache)
        assert not Path(cache).exists(), f"{arguments}: the cache {cache} outlived the question"
        assert downloads == "never", (arguments, downloads)
    assert not (tmp_path / "default-cache").exists()


#: A second process that enters :func:`removing_a_new_account_temp_dir`: it prints ``ready`` just
#: before entering and ``entered`` once inside.
_SECOND_ENTRANT = """
import sys
sys.path.insert(0, sys.argv[1])
from elevation_doubles import removing_a_new_account_temp_dir
print("ready", flush=True)
with removing_a_new_account_temp_dir():
    print("entered", flush=True)
"""


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the account temporary directory is a POSIX root-run path")
def test_a_second_process_waits_outside_the_account_temp_dir_block() -> None:
    """Only one PROCESS at a time is inside the block, not only one xdist worker of one run.

    bmk's test-all runs a pytest per Python version at the same time, all below the one real
    ``~/.cache/pwshpy-tmp``; one run's cleanup removed the directory between another run's
    ``mkdir -p`` and ``chmod 700`` (exit 70, "cannot make ... private").
    """
    with removing_a_new_account_temp_dir():
        second = subprocess.Popen(  # noqa: S603 - a fixed interpreter and script, no shell
            [sys.executable, "-c", _SECOND_ENTRANT, str(Path(__file__).parent)],
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert second.stdout is not None
        assert second.stdout.readline().strip() == "ready"
        try:
            second.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        assert second.poll() is None, "a second process entered while the first was inside"
    out, _ = second.communicate(timeout=30)
    assert out.strip() == "entered", out
