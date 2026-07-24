"""End-to-end: pack a script, RUN the artefact under a real PowerShell, unpack it again.

This is the contract proof for the packer.  Everything else in the suite checks what the
packer writes; this checks that what it writes actually works - the payload unpacks, uv
resolves the PEP 723 dependencies, local submodules import, the arguments arrive exactly as
typed, and the script's own exit code comes back through PowerShell.

These run in CI on purpose: GitHub's ubuntu, windows and macos runners all ship PowerShell and
the workflow installs ``uv``, so the windows-latest leg exercises the real Windows PowerShell
5.1 path (``_powershell()`` picks up ``powershell.exe`` there) - the one branch this dev box
cannot reach. They skip cleanly when ``pwsh``/``uv`` are absent, and the dependency cases reach
PyPI. Two tests stay ``local_only`` and never touch a runner: the ``mutating`` elevate test
(GitHub runners have passwordless sudo, so ``-m "not local_only"`` would really run it as root)
and the uv-absent test (it blanks ``PATH``/``HOME``, which can break process creation on Windows).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from pwshpy.adapters.native.packer import pack_script, unpack_script

pytestmark = [pytest.mark.os_agnostic]

_PWSH = shutil.which("pwsh") or shutil.which("powershell")
_UV = shutil.which("uv")

if _PWSH is None or _UV is None:  # pragma: no cover - environment gate, not logic
    pytest.skip("the packer end-to-end test needs both pwsh and uv", allow_module_level=True)

_ENTRY_WITH_SUBMODULE = """import sys
from pkg import helper

print("greeting:" + helper.greet())
print("argv:" + repr(sys.argv[1:]))
sys.exit(helper.EXIT_CODE)
"""

_ENTRY_WITH_DEPENDENCY = """# /// script
# requires-python = ">=3.10"
# dependencies = ["cowsay"]
# ///
import cowsay

print("dependency:" + cowsay.__name__)
"""


def _run(runner: Path, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Execute a packed runner the way a user would, and capture everything it produced."""
    assert _PWSH is not None
    child_env = {**os.environ, **(env or {})}
    # The uv the runner finds must be the one this test found, so a PATH-less environment
    # does not silently exercise the network installer.
    child_env["PATH"] = os.pathsep.join([str(Path(_UV or "uv").parent), child_env.get("PATH", "")])
    return subprocess.run(  # noqa: S603 - fixed argv built from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(runner), *arguments],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=child_env,
    )


def _project(root: Path, *, exit_code: int = 0) -> Path:
    """Write an entry script that imports a local package submodule."""
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "helper.py").write_text(
        f'EXIT_CODE = {exit_code}\n\n\ndef greet():\n    return "hello from the submodule"\n'
    )
    entry = root / "app.py"
    entry.write_text(_ENTRY_WITH_SUBMODULE)
    return entry


def _isolated_cache(tmp_path: Path) -> dict[str, str]:
    """Point the runner's extraction cache at a temp dir so tests never touch the real one."""
    return {"PWSHPY_PACK_CACHE": str(tmp_path / "cache")}


def test_packed_script_runs_and_imports_its_submodule(tmp_path: Path) -> None:
    """The headline case: one file in, a working program out."""
    manifest = pack_script(_project(tmp_path))
    result = _run(Path(manifest.output_path), env=_isolated_cache(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "greeting:hello from the submodule" in result.stdout


def test_packed_script_returns_its_own_exit_code(tmp_path: Path) -> None:
    """A nonzero exit must survive Python -> uv -> PowerShell, or scripting on it is useless."""
    manifest = pack_script(_project(tmp_path, exit_code=42))
    result = _run(Path(manifest.output_path), env=_isolated_cache(tmp_path))
    assert result.returncode == 42, result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["plain", "two"], id="plain"),
        pytest.param(["with space", "trailing "], id="spaces"),
        pytest.param(['embedded "quotes"', "it's"], id="quotes"),
        pytest.param(["", "after-empty"], id="empty-string"),
        pytest.param(["-v", "-d", "-ErrorAction"], id="powershell-common-parameter-names"),
        pytest.param(["--flag", "value", "--other=x"], id="unix-flags"),
        pytest.param(["grüße", "日本語", "emoji-\U0001f600"], id="unicode"),
    ],
)
def test_arguments_arrive_exactly_as_typed(tmp_path: Path, arguments: Sequence[str]) -> None:
    """Every argument reaches sys.argv unchanged.

    The awkward cases are the point: PowerShell would bind -v/-d to its own common
    parameters if the runner declared any, and 5.1 re-quotes arguments on their way to a
    native executable, which is why the vector travels out of band.
    """
    manifest = pack_script(_project(tmp_path))
    result = _run(Path(manifest.output_path), *arguments, env=_isolated_cache(tmp_path))
    assert result.returncode == 0, result.stderr
    assert f"argv:{list(arguments)!r}" in result.stdout


def test_pep723_dependency_is_resolved_by_uv(tmp_path: Path) -> None:
    """The entry's inline metadata must reach uv, which means it must ride on the shim."""
    entry = tmp_path / "app.py"
    entry.write_text(_ENTRY_WITH_DEPENDENCY)
    manifest = pack_script(entry)
    result = _run(Path(manifest.output_path), env=_isolated_cache(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "dependency:cowsay" in result.stdout


def test_with_packages_dependency_is_resolved_by_uv(tmp_path: Path) -> None:
    """--with covers a script that declares nothing inline."""
    entry = tmp_path / "app.py"
    entry.write_text('import cowsay\n\nprint("dependency:" + cowsay.__name__)\n')
    manifest = pack_script(entry, with_packages=["cowsay"])
    result = _run(Path(manifest.output_path), env=_isolated_cache(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "dependency:cowsay" in result.stdout


def _plant_witness(cache_dir: Path) -> Path:
    """Drop a file the manifest knows nothing about, to observe whether extraction re-ran.

    Extracted files cannot be timed: their mtimes come from the archive, which the packer
    pins to the zip epoch so that identical sources hash identically.  A stray file is the
    honest probe - re-extraction wipes the directory, cache reuse leaves it alone.
    """
    witness = cache_dir / "witness.txt"
    witness.write_text("planted")
    return witness


def test_second_run_reuses_the_extraction_cache(tmp_path: Path) -> None:
    """The cache is keyed by payload hash, so a repeat run must not unpack again."""
    manifest = pack_script(_project(tmp_path))
    environment = _isolated_cache(tmp_path)
    assert _run(Path(manifest.output_path), env=environment).returncode == 0
    witness = _plant_witness(tmp_path / "cache" / manifest.payload_sha256[:16])
    assert _run(Path(manifest.output_path), env=environment).returncode == 0
    assert witness.exists(), "the payload was extracted a second time"


def test_clean_switch_forces_a_fresh_extraction(tmp_path: Path) -> None:
    """-PwshPyClean is the escape hatch when a cached tree needs discarding."""
    manifest = pack_script(_project(tmp_path))
    environment = _isolated_cache(tmp_path)
    assert _run(Path(manifest.output_path), env=environment).returncode == 0
    witness = _plant_witness(tmp_path / "cache" / manifest.payload_sha256[:16])
    assert _run(Path(manifest.output_path), "-PwshPyClean", env=environment).returncode == 0
    assert not witness.exists(), "-PwshPyClean did not discard the cached extraction"


def test_tampered_cache_is_detected_and_replaced(tmp_path: Path) -> None:
    """A cached tree may later be executed elevated, so tampering must never survive a run."""
    manifest = pack_script(_project(tmp_path))
    environment = _isolated_cache(tmp_path)
    assert _run(Path(manifest.output_path), env=environment).returncode == 0
    planted = tmp_path / "cache" / manifest.payload_sha256[:16] / "pkg" / "helper.py"
    planted.write_text('EXIT_CODE = 0\n\n\ndef greet():\n    return "TAMPERED"\n')
    result = _run(Path(manifest.output_path), env=environment)
    assert result.returncode == 0, result.stderr
    assert "TAMPERED" not in result.stdout
    assert "greeting:hello from the submodule" in result.stdout


def test_info_switch_reports_the_manifest_without_running(tmp_path: Path) -> None:
    """-PwshPyInfo inspects an artefact you were handed, without executing its payload."""
    manifest = pack_script(_project(tmp_path))
    result = _run(Path(manifest.output_path), "-PwshPyInfo", env=_isolated_cache(tmp_path))
    assert result.returncode == 0, result.stderr
    assert manifest.payload_sha256 in result.stdout
    assert "pkg/helper.py" in result.stdout
    assert "greeting:" not in result.stdout


def test_help_switch_explains_how_to_unpack_and_repack(tmp_path: Path) -> None:
    """Whoever is handed the artefact must be able to read what it is and rebuild it."""
    manifest = pack_script(_project(tmp_path))
    result = _run(Path(manifest.output_path), "-PwshPyHelp", env=_isolated_cache(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "uvx pwshpy unpack" in result.stdout
    assert "uvx pwshpy pack src/app.py" in result.stdout
    assert "-PwshPyInfo" in result.stdout
    assert "greeting:" not in result.stdout


@pytest.mark.local_only
def test_no_install_uv_fails_cleanly_when_uv_is_absent(tmp_path: Path) -> None:
    """The opt-out must fail loudly with exit 127, never reach for the network installer.

    Stays ``local_only``: it blanks PATH and HOME to hide every uv, which is safe on POSIX but
    can break process creation on a Windows runner, so it never runs in CI.
    """
    manifest = pack_script(_project(tmp_path))
    empty = tmp_path / "empty-path"
    empty.mkdir()
    barren = tmp_path / "barren-home"
    barren.mkdir()
    result = subprocess.run(  # noqa: S603 - fixed argv from a resolved interpreter path
        [str(_PWSH), "-NoProfile", "-NonInteractive", "-File", str(manifest.output_path), "-PwshPyNoInstallUv"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        # An empty PATH is not enough: the runner also probes the well-known per-user install
        # dirs, so the home directory has to be barren too or it finds the real uv there.
        env={
            **os.environ,
            "PATH": str(empty),
            "HOME": str(barren),
            "USERPROFILE": str(barren),
            **_isolated_cache(tmp_path),
        },
    )
    assert result.returncode == 127
    assert "uv" in result.stderr


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_posix
@pytest.mark.skipif(
    subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode != 0,  # noqa: S607
    reason="needs passwordless sudo",
)
def test_elevate_switch_runs_the_script_as_root(tmp_path: Path) -> None:
    """-PwshPyElevate must run the script elevated AND hand back its output and exit code.

    Marked mutating because sudo resets the environment: the elevated child cannot see
    PWSHPY_PACK_CACHE, so it unpacks into root's real cache rather than a temp one.

    This guards a specific trap: a PowerShell function returns everything written to its
    output stream, so relaying the child's exit code through a return value silently
    swallowed the child's stdout and reported success for every run.
    """
    entry = tmp_path / "who.py"
    entry.write_text('import os\nimport sys\n\nprint("euid:%d" % os.geteuid())\nsys.exit(9)\n')
    manifest = pack_script(entry)
    result = _run(Path(manifest.output_path), "-PwshPyElevate")
    assert result.returncode == 9, result.stderr
    assert "euid:0" in result.stdout


def test_unpack_edit_repack_produces_a_working_artefact(tmp_path: Path) -> None:
    """The full editing workflow: unpack a pack, change a module, pack it again, run it."""
    original = pack_script(_project(tmp_path))
    restored = tmp_path / "restored"
    unpack_script(original.output_path, restored)
    helper = restored / "pkg" / "helper.py"
    helper.write_text('EXIT_CODE = 3\n\n\ndef greet():\n    return "edited greeting"\n')

    repacked = pack_script(restored / "app.py", tmp_path / "edited.ps1")
    result = _run(Path(repacked.output_path), env=_isolated_cache(tmp_path))
    assert result.returncode == 3, result.stderr
    assert "greeting:edited greeting" in result.stdout


def test_unpack_of_a_freshly_packed_artefact_round_trips(tmp_path: Path) -> None:
    """Unpacking returns the exact sources, so the pack is a lossless transport."""
    entry = _project(tmp_path)
    manifest = pack_script(entry)
    restored = unpack_script(manifest.output_path, tmp_path / "out")
    assert restored.files == ["app.py", "pkg/__init__.py", "pkg/helper.py"]
    assert (tmp_path / "out" / "pkg" / "helper.py").read_bytes() == (tmp_path / "pkg" / "helper.py").read_bytes()
