"""End-to-end: pack a script, RUN the artefact under a real PowerShell, unpack it again.

This is the contract proof for the packer.  Everything else in the suite checks what the
packer writes; this checks that what it writes actually works - the payload unpacks, uv
resolves the PEP 723 dependencies, local submodules import, the arguments arrive exactly as
typed, and the script's own exit code comes back through PowerShell.

These run in CI on purpose: GitHub's ubuntu, windows and macos runners all ship PowerShell and
the workflow installs ``uv``. The host is ``pwsh`` where it is on PATH, else ``powershell``; the
``PWSHPY_TEST_POWERSHELL`` environment variable names another one, which is how a Windows host
that has pwsh 7 runs the module under Windows PowerShell 5.1 (``PWSHPY_TEST_POWERSHELL=powershell``);
a host it names that cannot be found fails the collection, never skips the module, so a misspelled
selection cannot pass for a green run.
On Windows every runner starts under a process-scoped Bypass execution policy (see
``_process_scoped_execution_policy``). They skip cleanly when no PowerShell or ``uv`` is found, and
the dependency cases reach PyPI. Two tests stay ``local_only`` and never touch a runner: the ``mutating`` elevate test
(GitHub runners have passwordless sudo, so ``-m "not local_only"`` would really run it as root)
and the uv-absent test (it blanks ``PATH``/``HOME``, which can break process creation on Windows).
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import cast

import pytest
from elevation_doubles import (
    GIT_VARIABLES,
    INTERPRETER_VARIABLES,
    LIBRARY_PATH_PLANTS,
    LIBRARY_VARIABLES,
    LOCAL_WHEEL_DISTRIBUTION,
    MANAGED_PYTHON_VARIABLES,
    NETWORK_VARIABLES,
    NOT_A_GCM_VARIABLE,
    NOT_A_GIT_VARIABLE,
    OTHER_UID,
    RUNNING_AS_ROOT,
    UV_SOURCE_VARIABLES,
    data_home,
    default_uv_cache_dir,
    default_uv_cache_env,
    fetched_git_source,
    interpreter_probe_env,
    link_tools,
    managed_python_env,
    owned_by_another_account,
    plant_library_path,
    planted_managed_python,
    session_uv_cache,
    trusted_managed_python,
    with_account_cache_home,
    with_data_home,
    without_python_dir_check,
    write_git_source,
    write_local_wheel,
    write_planted_environment,
    write_planted_python,
    write_planted_venv,
    write_recording_uv,
)
from test_pack_sudo_double import second_prompts, write_sudo_double

from pwshpy.adapters.native.packer import pack_script, unpack_script
from pwshpy.domain.packing import PackOptions

if sys.platform != "win32":
    import pwd

pytestmark = [pytest.mark.os_agnostic]


def _selected_powershell() -> str | None:
    """The PowerShell every runner here starts under (see the module docstring for the override).

    None only when nothing was selected and neither default is installed: the module then skips. An
    explicit selection that is not found fails instead, since a skip would hide the typo.
    """
    selected = os.environ.get("PWSHPY_TEST_POWERSHELL")
    if not selected:
        return shutil.which("pwsh") or shutil.which("powershell")
    found = shutil.which(selected)
    if found is None:
        pytest.fail(f"PWSHPY_TEST_POWERSHELL={selected!r} names no PowerShell on PATH", pytrace=False)
    return found


_PWSH = _selected_powershell()
_UV = shutil.which("uv")
#: Passed on every run: a run that finds no uv it may use must fail with 127, never fetch the network
#: installer, which would install uv into the real profile of whoever runs the suite (it did once,
#: on the dev VM, while the runner refused every uv during a RED run).
_NEVER_INSTALL_UV = "-PwshPyNoInstallUv"
#: The POSIX path the runner probes whatever the environment says (pack_runner.ps1).
_SYSTEM_WIDE_UV = "/usr/local/bin/uv"
#: Variables that point the runner's uv lookup at an install directory.
_UV_LOCATION_VARIABLES = ("XDG_BIN_HOME", "CARGO_HOME")

if _PWSH is None or _UV is None:  # pragma: no cover - environment gate, not logic
    pytest.skip("the packer end-to-end test needs both pwsh and uv", allow_module_level=True)


class _UvLocation:
    """The directory the e2e runs put first on PATH so the runner finds uv there.

    On an elevated Windows host (CI's windows-latest, the dev VM) the runner accepts only a uv that
    no account other than the running one, Administrators and the system accounts can modify. A
    per-user uv passes that, but CI's uv sits in the hosted tool cache under the C: drive root, and
    a directory created there inherits the root's Authenticated Users Modify grant on a default
    Windows install, so the found uv cannot be relied on: the module fixture below points this at a
    copy in a directory only this account, SYSTEM and Administrators may change (no /setowner).
    """

    directory: Path = Path(_UV or "uv").parent


@pytest.fixture(scope="module", autouse=True)
def _process_scoped_execution_policy() -> Iterator[None]:
    """On Windows, start every runner of this module under a process-scoped Bypass execution policy.

    Windows PowerShell 5.1 on a client without a configured policy is Restricted and refuses to run
    any script file, so its arm would test nothing; pwsh 7 (RemoteSigned) runs them either way. The
    variable is what ``-ExecutionPolicy Bypass`` sets for its own process, inherited only by the
    processes this module starts, so both hosts are launched alike and the machine's policy is never
    changed. Elsewhere PowerShell has no execution policy to set.
    """
    if sys.platform != "win32":
        yield
        return
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PSExecutionPolicyPreference", "Bypass")
        yield


@pytest.fixture(scope="module", autouse=True)
def _pwsh_cache_in_a_scratch_directory(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """On POSIX, point XDG_CACHE_HOME at a scratch directory for every process this module starts ([123]).

    pwsh writes its own cache (StartupProfileData-NonInteractive, ModuleAnalysisCache) below
    $XDG_CACHE_HOME/powershell, else ~/.cache/powershell: the real account's, root's on a root run of
    the suite, as uv's cache was before [111]. The variable also keeps a runner given no
    PWSHPY_PACK_CACHE out of the real ~/.cache. A test that sets XDG_CACHE_HOME itself still wins.
    Windows is left as it is: pwsh caches below LOCALAPPDATA there, which only the tests given
    :func:`_isolated_cache` point elsewhere, and moving it for every test would move uv's defaults too.
    """
    if sys.platform == "win32":
        yield
        return
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))
        yield


@pytest.fixture(scope="module", autouse=True)
def _uv_the_runner_trusts(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Give an elevated Windows run a uv it trusts; elsewhere the found uv is used as it is."""
    if not _windows_elevated():
        yield
        return
    trusted = _trusted_uv(tmp_path_factory.mktemp("module-uv"))
    assert trusted is not None
    _UvLocation.directory = trusted.parent
    try:
        yield
    finally:
        _UvLocation.directory = Path(_UV or "uv").parent
        shutil.rmtree(trusted.parent, ignore_errors=True)


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
    child_env["PATH"] = os.pathsep.join([str(_UvLocation.directory), child_env.get("PATH", "")])
    # Pin the packed script's stdout to UTF-8 and decode it as UTF-8, so a non-ASCII argument
    # round-trips through the assertion regardless of the host's console code page. Without this,
    # `text=True` decodes the child's UTF-8 output as cp1252 on Windows and mangles it - a capture
    # artefact, not a packer defect (the argument reaches sys.argv intact either way).
    child_env["PYTHONIOENCODING"] = "utf-8"
    # Never a Python download into a real user directory ([92]): uv uses what is installed.
    child_env["UV_PYTHON_DOWNLOADS"] = "never"
    # Nor a script environment in the real user's uv cache ([93]), unless the test names its own.
    child_env.setdefault("UV_CACHE_DIR", str(session_uv_cache()))
    runner = _as_this_invoker(runner, arguments)
    return subprocess.run(  # noqa: S603 - fixed argv built from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(runner), _NEVER_INSTALL_UV, *arguments],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
        env=child_env,
    )


def _host_elevated() -> bool:
    """Whether the suite itself runs elevated: as root, or with a Windows administrator token."""
    return RUNNING_AS_ROOT or _windows_elevated()


#: What a whole-runner stand-in starts with in place of the elevation probe (see _elevation_stand_in).
_STAND_IN_PROBE = "function Test-PwshPyElevated {\n    return "


def _as_this_invoker(runner: Path, arguments: Sequence[str]) -> Path:
    """The runner a test of an UNELEVATED run executes: ``runner`` itself, or on an elevated host a
    stand-in whose probe answers not elevated ([92]).

    The real probe would take the elevated path there (only a uv-managed Python, which a host may not
    have and the suite never downloads), so the same test would test something else as root or on an
    elevated Windows CI runner. A runner that is already a stand-in, or a run asking to elevate, is
    left as it is.
    """
    if not _host_elevated() or "-PwshPyElevate" in arguments:
        return runner
    if _STAND_IN_PROBE in runner.read_text(encoding="utf-8"):
        return runner
    return _elevation_stand_in(runner, runner.with_name(runner.stem + ".unelevated.ps1"), elevated="$false")


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


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="pwsh caches below XDG_CACHE_HOME only on POSIX")
def test_pwsh_keeps_its_cache_in_the_modules_scratch_directory(tmp_path: Path) -> None:
    """[123]: the pwsh a test starts is handed the module's scratch XDG_CACHE_HOME (the fixture above),
    so it keeps its own cache (StartupProfileData-NonInteractive, ModuleAnalysisCache) there and never
    in the real account's ~/.cache/powershell. Asked of the child itself: pwsh writes that cache only
    when its startup profile changed, so the file's presence cannot be the check."""
    assert _PWSH is not None
    seen = subprocess.run(  # noqa: S603 - fixed argv from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-Command", "[Console]::Out.Write($env:XDG_CACHE_HOME)"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert seen.returncode == 0, seen.stderr
    assert Path(seen.stdout).is_relative_to(tmp_path.parent), f"pwsh caches below {seen.stdout!r}"


def _isolated_cache(tmp_path: Path) -> dict[str, str]:
    """Point the runner's extraction cache and uv's cache at temp dirs so tests never touch the real ones.

    ``UV_CACHE_DIR`` too: a run given only this mapping (the whole-runner stand-ins, the concurrent
    cold start) would otherwise build its script environment in the real user's uv cache ([93]). And
    uv's default cache, the one an elevated run falls back to after dropping ``UV_CACHE_DIR`` (X21 P3).
    """
    return {
        "PWSHPY_PACK_CACHE": str(tmp_path / "cache"),
        "UV_CACHE_DIR": str(session_uv_cache()),
        **default_uv_cache_env(tmp_path),
    }


def _has_passwordless_sudo() -> bool:
    """Whether ``sudo`` runs without a password prompt (POSIX only, safe on any OS).

    A ``skipif`` condition is evaluated at COLLECTION time on every platform, so it must not
    raise where ``sudo`` is absent: on Windows ``shutil.which`` returns ``None`` and we stop
    before a subprocess that would raise ``FileNotFoundError`` and abort the whole module.
    """
    if shutil.which("sudo") is None:
        return False
    return subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode == 0  # noqa: S607


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
    manifest = pack_script(entry, options=PackOptions(with_packages=["cowsay"]))
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


def test_concurrent_cold_starts_do_not_race(tmp_path: Path) -> None:
    """Two cold-cache runs of the same pack must both succeed, not stomp each other's extraction.

    Launches both processes against one empty cache so they contend for the first extraction;
    the runner's lock must serialize it so neither yanks the directory the other runs from.
    """
    runner = _as_this_invoker(Path(pack_script(_project(tmp_path)).output_path), ())
    environment = {
        **os.environ,
        "PATH": os.pathsep.join([str(_UvLocation.directory), os.environ.get("PATH", "")]),
        "UV_PYTHON_DOWNLOADS": "never",
        **_isolated_cache(tmp_path),
    }
    assert _PWSH is not None
    argv = [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(runner), _NEVER_INSTALL_UV, "concurrent"]
    first = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)  # noqa: S603
    second = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)  # noqa: S603
    out_a, err_a = first.communicate(timeout=300)
    out_b, err_b = second.communicate(timeout=300)
    assert first.returncode == 0, err_a
    assert second.returncode == 0, err_b
    assert "greeting:hello from the submodule" in out_a
    assert "greeting:hello from the submodule" in out_b


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


def test_corrupt_payload_is_rejected_before_extraction(tmp_path: Path) -> None:
    """A payload damaged in transit must fail with a clear message, not a cryptic zip error."""
    manifest = pack_script(_project(tmp_path))
    runner = Path(manifest.output_path)
    lines = runner.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "$PwshPyPayload = @'")
    # Flip the first character of the first payload line to a different valid base64 symbol, so
    # the blob still decodes but its bytes - and therefore its sha256 - no longer match.
    body = lines[start + 1]
    lines[start + 1] = ("B" if body[0] != "B" else "C") + body[1:]
    runner.write_text("\n".join(lines) + "\n")
    result = _run(runner, env=_isolated_cache(tmp_path))
    assert result.returncode != 0
    assert "corrupt" in (result.stdout + result.stderr).lower()


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
    can break process creation on a Windows runner, so it never runs in CI. A system-wide uv at
    the fixed path the runner also probes cannot be hidden by the environment, so on such a
    machine the premise cannot be set up and the test skips rather than report the runner wrong.
    """
    if os.name != "nt" and Path(_SYSTEM_WIDE_UV).exists():
        pytest.skip(f"a system-wide uv at {_SYSTEM_WIDE_UV} cannot be hidden from the runner")
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
        # dirs (and XDG_BIN_HOME / CARGO_HOME when set), so the home directory has to be barren
        # too and those variables gone, or it finds the real uv there.
        env={
            **{k: v for k, v in os.environ.items() if k not in _UV_LOCATION_VARIABLES},
            "PATH": str(empty),
            "HOME": str(barren),
            "USERPROFILE": str(barren),
            "LOCALAPPDATA": str(barren),
            **_isolated_cache(tmp_path),
        },
    )
    assert result.returncode == 127
    assert "uv" in result.stderr


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_posix
@pytest.mark.skipif(not _has_passwordless_sudo(), reason="needs passwordless sudo")
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


@pytest.mark.local_only
@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the sudo double needs a POSIX host")
@pytest.mark.skipif(RUNNING_AS_ROOT, reason="the runner skips elevation when already root")
def test_elevate_switch_exits_77_when_sudo_refuses(tmp_path: Path) -> None:
    """A refused sudo is exit 77 (EX_NOPERM), told apart from the script's own exit 1."""
    entry = tmp_path / "who.py"
    marker = tmp_path / "ran.marker"
    entry.write_text(f'open({str(marker)!r}, "w").close()\n')
    manifest = pack_script(entry)
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    write_sudo_double(bin_dir, grant=False)
    environment = {**_isolated_cache(tmp_path), "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])}
    result = _run(Path(manifest.output_path), "-PwshPyElevate", env=environment)
    assert result.returncode == 77, result.stderr
    assert "pwshpy: elevation declined" in result.stderr
    assert not marker.exists(), "the packed script ran although sudo refused"


@pytest.mark.local_only
@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the sudo double needs a POSIX host")
@pytest.mark.skipif(RUNNING_AS_ROOT, reason="the runner skips elevation when already root")
def test_elevate_switch_forwards_the_clean_switch(tmp_path: Path) -> None:
    """-PwshPyClean survives the relaunch: a stray file in the cached tree is gone afterwards."""
    manifest = pack_script(_project(tmp_path, exit_code=4))
    runner = Path(manifest.output_path)
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    write_sudo_double(bin_dir, grant=True)
    environment = {**_isolated_cache(tmp_path), "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])}
    assert _run(runner, env=environment).returncode == 4
    # The .ps1 leaves its lock file beside the extraction; only the directory is the cached tree.
    caches = [path for path in (tmp_path / "cache").iterdir() if path.is_dir()]
    assert len(caches) == 1
    # Not a payload file, so the per-file hash check alone never removes it; only a clean does.
    stray = caches[0] / "stray.txt"
    stray.write_text("left over")
    result = _run(runner, "-PwshPyElevate", "-PwshPyClean", env=environment)
    assert result.returncode == 4, result.stderr
    assert "greeting:hello from the submodule" in result.stdout
    assert not stray.exists(), "-PwshPyClean was dropped by the elevated relaunch"


@pytest.mark.local_only
@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the sudo double needs a POSIX host")
@pytest.mark.skipif(RUNNING_AS_ROOT, reason="the runner skips elevation when already root")
@pytest.mark.parametrize("cached", [True, False], ids=["credential-cached", "timestamp-timeout-0"])
def test_elevate_switch_never_lets_sudo_prompt_twice(tmp_path: Path, cached: bool) -> None:
    """[136-sh]: the .ps1 relaunch on POSIX asks once (``sudo -v``), checks with ``sudo -n -v`` that a
    credential was cached and relaunches with ``sudo -n``: sudo never prompts again. Where it caches
    nothing (``timestamp_timeout=0``) the run is declined (exit 77) and the script never runs."""
    entry = tmp_path / "who.py"
    marker = tmp_path / "ran.marker"
    entry.write_text(f'open({str(marker)!r}, "w").close()\n')
    manifest = pack_script(entry)
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    calls = tmp_path / "sudo-calls.txt"
    write_sudo_double(bin_dir, grant=True, cached=cached, calls=calls)
    environment = {**_isolated_cache(tmp_path), "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])}
    result = _run(Path(manifest.output_path), "-PwshPyElevate", env=environment)
    assert second_prompts(calls) == [], calls.read_text(encoding="utf-8")
    if cached:
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.exists()
        return
    assert result.returncode == 77, result.stdout + result.stderr
    assert "pwshpy: elevation declined" in result.stderr, result.stderr
    assert not marker.exists(), "the packed script ran although sudo kept no credential"


@pytest.mark.local_only
@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="hiding sudo needs a POSIX host")
@pytest.mark.skipif(RUNNING_AS_ROOT, reason="the runner skips elevation when already root")
def test_elevate_switch_without_sudo_exits_127_not_77(tmp_path: Path) -> None:
    """A host with no sudo is a missing tool (127), not a refused elevation (77)."""
    entry = tmp_path / "who.py"
    marker = tmp_path / "ran.marker"
    entry.write_text(f'open({str(marker)!r}, "w").close()\n')
    manifest = pack_script(entry)
    # Only `id` (the runner's root check): sudo lives in the same system dir, so the whole PATH
    # has to be this one directory to hide it.
    environment = {**_isolated_cache(tmp_path), "PATH": str(link_tools(tmp_path / "no-sudo-bin", ("id",)))}
    result = _run(Path(manifest.output_path), "-PwshPyElevate", env=environment)
    assert result.returncode == 127, result.stderr
    assert "pwshpy: sudo not found" in result.stderr
    assert not marker.exists()


_UAC_MAPPING_PROBE = r"""
param([string] $Artefact)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Artefact, [ref]$tokens, [ref]$errors)
$definition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-PwshPyUacDeclined'
}, $true)
if (-not $definition) { throw 'Test-PwshPyUacDeclined not found in the artefact' }
. ([ScriptBlock]::Create($definition.Extent.Text))
$cancelled = New-Object System.ComponentModel.Win32Exception 1223
$denied = New-Object System.ComponentModel.Win32Exception 5
$cases = [ordered]@{
    wrapped_1223 = (New-Object System.Management.Automation.MethodInvocationException 'start failed', $cancelled)
    bare_1223 = $cancelled
    wrapped_5 = (New-Object System.Management.Automation.MethodInvocationException 'start failed', $denied)
    message_only = (New-Object System.InvalidOperationException 'The operation was canceled by the user.')
}
foreach ($name in $cases.Keys) { Write-Output ("{0}={1}" -f $name, (Test-PwshPyUacDeclined -Failure $cases[$name])) }
"""


def test_a_refused_uac_prompt_is_recognised_by_its_win32_code(tmp_path: Path) -> None:
    """The .ps1's refusal test maps Win32Exception 1223 (also wrapped) to declined, nothing else.

    The real UAC "No" click needs a person; this pins the mapping the Windows branch relies on by
    running the artefact's own function against constructed exceptions. A message-only
    exception (what Start-Process -Verb RunAs throws) must NOT read as declined.
    """
    assert _PWSH is not None
    manifest = pack_script(_project(tmp_path))
    probe = tmp_path / "probe.ps1"
    probe.write_text(_UAC_MAPPING_PROBE, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed argv from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(probe), str(manifest.output_path)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["wrapped_1223=True", "bare_1223=True", "wrapped_5=False", "message_only=False"]


#: Builds a stand-in for the Windows elevated relaunch out of the artefact's own functions. The
#: only change is the 'runas' verb, which becomes '' because no non-Windows host (and no test)
#: can answer a UAC prompt; everything else - the start info, the command line, the relay files,
#: the exit code - is the artefact's code, run for real.
_RELAUNCH_BUILDER = r"""
param([string] $Artefact, [string] $Body, [string] $Output)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Artefact, [ref]$tokens, [ref]$errors)
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false) | ForEach-Object { $_.Extent.Text }
$functions = $definitions -join "`n`n"
$verbs = ([regex]::Matches($functions, "'runas'")).Count
if ($verbs -ne 1) { throw "expected exactly one 'runas' verb in the artefact, found $verbs" }
$functions = $functions.Replace("'runas'", "''")
[IO.File]::WriteAllText($Output, $functions + "`n`n" + (Get-Content -LiteralPath $Body -Raw))
"""

#: The stand-in's script body. Run normally it moves to PWSHPY_TEST_CWD and relaunches itself
#: through the artefact's Invoke-PwshPyElevated; the relaunched child (recognised by the relay
#: switch the relaunch adds) reports its working directory, its script argv and its host's own
#: argv through the relay file. With PWSHPY_TEST_SILENT set the child exits 3 and writes nothing,
#: which is what an elevated child refused by the execution policy looks like to the parent.
_RELAUNCH_BODY = r"""
$PwshPyOnWindows = $true
$relayAt = [Array]::IndexOf([object[]]$args, '-PwshPyRelay')
if ($relayAt -ge 0 -and $env:PWSHPY_TEST_SILENT) { exit 3 }
if ($relayAt -ge 0) {
    $report = ConvertTo-Json -Compress -InputObject ([ordered]@{
        cwd = (Get-Location).ProviderPath; argv = @($args); host_argv = @([Environment]::GetCommandLineArgs())
    })
    [IO.File]::WriteAllText($args[$relayAt + 1] + '.out', $report)
    exit 5
}
Set-Location -LiteralPath $env:PWSHPY_TEST_CWD
Invoke-PwshPyElevated -ForwardArgs ([string[]]$args)
"""


def _run_relaunch_stand_in(
    tmp_path: Path, harness_dir: Path, cwd: Path, *arguments: str, env: dict[str, str] | None = None
) -> tuple[int, dict[str, object], str]:
    """Run the artefact's Windows relaunch with a non-elevating verb.

    Returns the parent's exit code, the child's JSON report (empty when the child wrote none) and
    the parent's stderr.
    """
    assert _PWSH is not None
    manifest = pack_script(_project(tmp_path))
    harness_dir.mkdir(parents=True, exist_ok=True)
    builder = tmp_path / "relaunch_builder.ps1"
    builder.write_text(_RELAUNCH_BUILDER, encoding="utf-8")
    body = tmp_path / "relaunch_body.ps1"
    body.write_text(_RELAUNCH_BODY, encoding="utf-8")
    harness = harness_dir / "relaunch.ps1"
    built = subprocess.run(  # noqa: S603 - fixed argv from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", *map(str, (builder, manifest.output_path, body, harness))],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    result = subprocess.run(  # noqa: S603 - fixed argv from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(harness), *arguments],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        env={**os.environ, "PWSHPY_TEST_CWD": str(cwd), **(env or {})},
    )
    try:
        report: object = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as error:
        pytest.fail(f"the child report is not JSON ({error}); stdout={result.stdout!r} stderr={result.stderr!r}")
    assert isinstance(report, dict), f"stdout={result.stdout!r} stderr={result.stderr!r}"
    return result.returncode, cast("dict[str, object]", report), result.stderr


def test_the_windows_elevated_relaunch_keeps_the_working_directory(tmp_path: Path) -> None:
    """The elevated child starts in the caller's PowerShell location, as Start-Process did.

    A bare ProcessStartInfo starts in the PROCESS's current directory, which PowerShell does not
    move on Set-Location, so `cd C:\\work; .\\runner.ps1 -PwshPyElevate` would run the script
    elsewhere. The harness moves with Set-Location only, which is exactly that situation.
    """
    work = tmp_path / "work"
    work.mkdir()
    exit_code, report, _ = _run_relaunch_stand_in(tmp_path, tmp_path / "harness", work, "plain")
    assert exit_code == 5, report
    assert report["cwd"] == str(work)


def test_the_windows_elevated_relaunch_bypasses_the_execution_policy(tmp_path: Path) -> None:
    """The elevated child's host gets -ExecutionPolicy Bypass ahead of -File.

    A process-scope execution policy lives only in the PSExecutionPolicyPreference variable, and a
    UAC launch builds the elevated environment fresh, so the caller's own Bypass never reaches the
    child: on a default client (5.1 policy Restricted) it would refuse to load the runner.
    """
    work = tmp_path / "work"
    work.mkdir()
    exit_code, report, stderr = _run_relaunch_stand_in(tmp_path, tmp_path / "harness", work, "plain")
    assert exit_code == 5, stderr
    host_argv = [str(item) for item in cast("list[object]", report["host_argv"])]
    assert "-File" in host_argv, host_argv
    file_at = host_argv.index("-File")
    policy_at = [index for index, item in enumerate(host_argv) if item == "-ExecutionPolicy"]
    assert policy_at, host_argv
    assert policy_at[0] < file_at, host_argv
    assert host_argv[policy_at[0] + 1] == "Bypass", host_argv


def test_a_silent_elevated_child_is_reported_not_swallowed(tmp_path: Path) -> None:
    """A child that exits non-zero without writing a relay file gets one stderr line; its code stands.

    An elevated host refused by the execution policy (or a window closed early) leaves no output at
    all, so without the line the caller sees a bare failure with nothing to go on.
    """
    work = tmp_path / "work"
    work.mkdir()
    exit_code, report, stderr = _run_relaunch_stand_in(
        tmp_path, tmp_path / "harness", work, "plain", env={"PWSHPY_TEST_SILENT": "1"}
    )
    assert exit_code == 3, stderr
    assert report == {}
    assert "pwshpy: the elevated child produced no output" in stderr


#: What the runner's elevated child writes ahead of its error when it fails before the script runs.
_EARLY_FAILURE_LINE = "pwshpy: the elevated child failed before running the script"
#: The exit code of that failure (EX_SOFTWARE), kept apart from PowerShell's generic 1.
_EARLY_FAILURE_EXIT = 70

#: Two of the three exact substitutions that turn a whole packed runner into a relaunch stand-in;
#: the third, the elevation probe, is built per call (see _whole_runner_stand_in).
_WHOLE_RUNNER_SUBSTITUTIONS = (
    (
        "if ($null -eq $IsWindows) { $PwshPyOnWindows = $true } else { $PwshPyOnWindows = $IsWindows }",
        "$PwshPyOnWindows = $true",
    ),
    ("'runas'", "''"),
)


def _whole_runner_stand_in(runner: Path, output: Path, *, elevated: str = "$false", judge_cache: bool = False) -> Path:
    """Turn a whole packed runner into a Windows-relaunch stand-in that runs its REAL body.

    Three exact, counted substitutions and nothing else: the host is declared Windows so the UAC
    branch of Invoke-PwshPyElevated is taken; the 'runas' verb becomes '' because no test can answer
    a UAC prompt; and the elevation probe answers ``elevated`` (a PowerShell expression, "not
    elevated" by default: the Windows token API does not exist off Windows). The child the relaunch
    starts is this same file, so it unpacks, finds uv and fails exactly where the shipped runner
    would. ``[bool]$PwshPyRelay`` answers "elevated" in the relaunched child only, as UAC would. A
    stand-in that can answer "elevated" also names a cache of its own ([X22 C1], see
    :func:`elevation_doubles.with_account_cache_home`), never the real account's, and takes its pack
    cache as trusted (:func:`_trusting_the_cache`) unless ``judge_cache`` keeps that check ([98]).
    """
    text = runner.read_text(encoding="utf-8")
    if elevated != "$false":
        text = with_account_cache_home(text, output.parent, powershell=True)
        if not judge_cache:
            text = _trusting_the_cache(text)
    probe = (
        "function Test-PwshPyElevated {",
        f"function Test-PwshPyElevated {{\n    return {elevated}\n}}\n\nfunction Test-PwshPyElevatedOnHost {{",
    )
    for old, new in (*_WHOLE_RUNNER_SUBSTITUTIONS, probe):
        assert text.count(old) == 1, f"expected exactly one {old!r} in the runner"
        text = text.replace(old, new)
    output.write_text(text, encoding="utf-8")
    return output


def _run_whole_runner_elevated(
    tmp_path: Path, runner: Path, *arguments: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a whole-runner stand-in with -PwshPyElevate and capture what the PARENT printed."""
    assert _PWSH is not None
    stand_in = _whole_runner_stand_in(runner, tmp_path / "stand_in.ps1")
    child_env = {**os.environ, **env}
    child_env["PATH"] = os.pathsep.join([str(_UvLocation.directory), child_env.get("PATH", "")])
    return subprocess.run(  # noqa: S603 - fixed argv from a resolved interpreter path
        [
            _PWSH,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(stand_in),
            _NEVER_INSTALL_UV,
            "-PwshPyElevate",
            *arguments,
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
        env=child_env,
    )


def test_an_elevated_child_that_fails_unpacking_relays_its_error(tmp_path: Path) -> None:
    """A corrupt payload stops the elevated child before uv; the parent prints the child's reason.

    On Windows the elevated child has its own console window, which closes when it exits, so an
    error it prints there is gone. It must reach the parent's stderr through the relay file, with
    exit 70 rather than PowerShell's generic 1 and without the "produced no output" line.
    """
    manifest = pack_script(_project(tmp_path))
    runner = Path(manifest.output_path)
    lines = runner.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "$PwshPyPayload = @'")
    body = lines[start + 1]
    lines[start + 1] = ("B" if body[0] != "B" else "C") + body[1:]
    runner.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _run_whole_runner_elevated(tmp_path, runner, "plain", env=_isolated_cache(tmp_path))
    assert result.returncode == _EARLY_FAILURE_EXIT, result.stderr
    assert _EARLY_FAILURE_LINE in result.stderr, result.stderr
    assert "the embedded payload is corrupt" in result.stderr, result.stderr
    assert "produced no output" not in result.stderr, result.stderr


def test_an_elevated_child_whose_cache_cannot_be_created_relays_its_error(tmp_path: Path) -> None:
    """A cache root that is a FILE fails the child's directory creation; that error is relayed."""
    manifest = pack_script(_project(tmp_path))
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("a file where the cache root should be\n", encoding="utf-8")
    result = _run_whole_runner_elevated(
        tmp_path, Path(manifest.output_path), "plain", env={"PWSHPY_PACK_CACHE": str(blocker)}
    )
    assert result.returncode == _EARLY_FAILURE_EXIT, result.stderr
    assert _EARLY_FAILURE_LINE in result.stderr, result.stderr
    assert "is not a directory" in result.stderr, result.stderr
    assert "produced no output" not in result.stderr, result.stderr


#: The first step of the run preparation (argument vector, target, uv arguments, working directory),
#: which a test makes fail on purpose.
_RUN_PREPARATION_STEP = "$PwshPyTarget = Join-Path $PwshPyCacheDir $PwshPyShim"


def test_an_elevated_child_that_fails_preparing_the_run_relays_its_error(tmp_path: Path) -> None:
    """[A5]: a failure after the uv lookup but before uv starts (resolving the target, reading the
    working directory, setting the variables the shim reads) is relayed like any earlier one: exit 70,
    the fixed line, and the reason, rather than PowerShell's generic 1 in a window that closes."""
    manifest = pack_script(_project(tmp_path))
    runner = Path(manifest.output_path)
    text = runner.read_text(encoding="utf-8")
    assert text.count(_RUN_PREPARATION_STEP) == 1
    runner.write_text(
        text.replace(_RUN_PREPARATION_STEP, _RUN_PREPARATION_STEP + "; throw 'injected run-preparation failure'"),
        encoding="utf-8",
    )
    result = _run_whole_runner_elevated(tmp_path, runner, "plain", env=_isolated_cache(tmp_path))
    assert result.returncode == _EARLY_FAILURE_EXIT, result.stderr
    assert _EARLY_FAILURE_LINE in result.stderr, result.stderr
    assert "injected run-preparation failure" in result.stderr, result.stderr


def test_an_elevated_child_that_runs_the_script_relays_its_output_and_code(tmp_path: Path) -> None:
    """Control: the same stand-in reaches uv run, so its output and the script's own code come back."""
    manifest = pack_script(_project(tmp_path, exit_code=4))
    result = _run_whole_runner_elevated(tmp_path, Path(manifest.output_path), "plain", env=_isolated_cache(tmp_path))
    assert result.returncode == 4, result.stderr
    assert "greeting:hello from the submodule" in result.stdout, result.stdout + result.stderr
    assert _EARLY_FAILURE_LINE not in result.stderr, result.stderr
    assert "produced no output" not in result.stderr, result.stderr


def test_the_windows_elevated_relaunch_keeps_spaced_paths_and_arguments_whole(tmp_path: Path) -> None:
    """A space in the script path, the temp dir or an argument reaches the child as one argument.

    The relaunch builds a flat command line (5.1's .NET Framework has no ArgumentList), so each
    argument must be quoted the way the child's parser splits it: `C:\\Users\\John Doe\\...` must
    not become two arguments.
    """
    spaced_temp = tmp_path / "temp with space"
    spaced_temp.mkdir()
    work = tmp_path / "work dir"
    work.mkdir()
    arguments = ["has space", 'quote"inside', "ends with backslash\\", "plain"]
    temp_variables = {"TMPDIR": str(spaced_temp), "TMP": str(spaced_temp), "TEMP": str(spaced_temp)}
    exit_code, report, _ = _run_relaunch_stand_in(
        tmp_path, tmp_path / "dir with space", work, *arguments, env=temp_variables
    )
    assert exit_code == 5, report
    assert isinstance(report["argv"], list)
    argv = cast("list[object]", report["argv"])
    assert argv[0] == "-PwshPyRelay"
    assert str(argv[1]).startswith(str(spaced_temp))
    assert argv[2:] == arguments
    assert report["cwd"] == str(work)


#: Quotes a list of arguments with the artefact's own command-line builder.
_COMMAND_LINE_PROBE = r"""
param([string] $Artefact, [string] $Source, [string] $Output)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Artefact, [ref]$tokens, [ref]$errors)
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false)
foreach ($definition in $definitions) { . ([ScriptBlock]::Create($definition.Extent.Text)) }
$values = [string[]](ConvertFrom-Json ([IO.File]::ReadAllText($Source)))
[IO.File]::WriteAllText($Output, (Join-PwshPyCommandLine -Arguments $values))
"""

#: One Windows command-line token: a quote with the backslashes before it, a backslash run, a
#: blank run, or a run of anything else.
_COMMAND_LINE_TOKEN = re.compile(r'(?P<quote>\\*")|(?P<slashes>\\+)|(?P<blank>[ \t]+)|(?P<text>[^\\" \t]+)')


def _command_line_to_argv(line: str) -> list[str]:
    """Split a Windows command line (program name already removed) the way CommandLineToArgvW does.

    2n backslashes before a quote give n backslashes and toggle quoting; 2n+1 give n backslashes and
    a literal quote; backslashes anywhere else are literal; blanks split only outside quotes. Two
    adjacent quotes INSIDE a quoted region are parsed differently by CommandLineToArgvW and the C
    runtime, so the builder must never emit them, and this parser does not model them.

    Example:
        >>> _command_line_to_argv('a "b c" "d\\\\"e" f\\\\')
        ['a', 'b c', 'd"e', 'f\\\\']
    """
    argv: list[str] = []
    current: str | None = None
    quoted = False
    for match in _COMMAND_LINE_TOKEN.finditer(line):
        token = match.group()
        if match.lastgroup == "blank" and not quoted:
            if current is not None:
                argv.append(current)
            current = None
            continue
        current = current or ""
        if match.lastgroup != "quote":
            current += token
            continue
        slashes = len(token) - 1
        current += "\\" * (slashes // 2) + ('"' if slashes % 2 else "")
        quoted = quoted if slashes % 2 else not quoted
    if current is not None:
        argv.append(current)
    return argv


def test_the_relaunch_command_line_round_trips_through_the_windows_argument_rules(tmp_path: Path) -> None:
    """Every argument the relaunch quotes comes back unchanged under CommandLineToArgvW's rules."""
    assert _PWSH is not None
    values = [
        "plain",
        "has space",
        "tab\there",
        'quote"inside',
        '"',
        "ends with backslash\\",
        "C:\\Users\\John Doe\\AppData\\Local\\Temp\\",
        "\\\\server\\share with space\\",
        'back\\\\"slash',
        "",
        "-PwshPyRelay",
        "gr\u00fc\u00dfe",
    ]
    manifest = pack_script(_project(tmp_path))
    probe = tmp_path / "probe.ps1"
    probe.write_text(_COMMAND_LINE_PROBE, encoding="utf-8")
    source = tmp_path / "values.json"
    source.write_text(json.dumps(values), encoding="utf-8")
    output = tmp_path / "line.txt"
    result = subprocess.run(  # noqa: S603 - fixed argv from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", *map(str, (probe, manifest.output_path, source, output))],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert _command_line_to_argv(output.read_text(encoding="utf-8")) == values


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


# --- which uv an elevated run trusts ([56]) --------------------------------------------------
# An elevated (or SYSTEM) run executes the uv it finds with full rights, so a uv that another account
# can replace is a privilege escalation; the running account itself, SYSTEM and Administrators may
# write it. The runner judges WHO CAN WRITE the binary and its directory, never where it was found.
# These tests force the runner's elevation probe so the same artefact code is exercised elevated and
# not, on any host.

#: The packed script under test: prints which uv started it. A POSIX stand-in exports its own path
#: as PWSHPY_TEST_UV before handing over; a real (or copied) uv sets UV to its own executable.
_ENTRY_REPORTING_UV = """import os
import sys

print("uv:" + (os.environ.get("PWSHPY_TEST_UV") or os.environ.get("UV") or ""))
sys.exit(0)
"""

#: The refusal the runner prints when an elevated run is pinned to a uv it cannot trust.
_PIN_REFUSED = "pwshpy: refusing the uv given with"
#: The line the runner prints for each uv an elevated run skips.
_UV_SKIPPED = "pwshpy: skipping uv at"
#: Exit code for a malformed wrapper switch (EX_USAGE).
_USAGE_EXIT = 64
#: SIDs granted in the Windows layouts; by SID because the guests are German (Administratoren).
_SID_ADMINISTRATORS = "*S-1-5-32-544"
_SID_SYSTEM = "*S-1-5-18"
_SID_USERS = "*S-1-5-32-545"
#: Broad groups whose write access always refuses a uv: Users, Authenticated Users, Everyone.
_BROAD_GROUP_SIDS = ("S-1-5-32-545", "S-1-5-11", "S-1-1-0")


def _windows_elevated() -> bool:
    """Whether this process holds an elevated administrator token (False off Windows)."""
    if sys.platform != "win32":
        return False
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _icacls(path: Path, *arguments: str) -> None:
    """Run icacls and fail the test on a non-zero exit (it changes nothing when it fails)."""
    result = subprocess.run(  # noqa: S603 - fixed argv; icacls is a system tool
        ["icacls", str(path), *arguments],  # noqa: S607 - resolved from System32 on every Windows host
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"icacls {path} {arguments} exited {result.returncode}: {result.stdout} {result.stderr}"
    )


def _current_user_sid() -> str:
    """This process's user SID, from whoami's locale-independent CSV form (Windows only)."""
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],  # noqa: S607 - resolved from System32 on every Windows host
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=True,
    )
    return result.stdout.strip().split(",")[-1].strip('"')


def _other_user_sid() -> str:
    """A real account that is not this one: the Guest (RID 501) of this account's own domain."""
    return _current_user_sid().rsplit("-", 1)[0] + "-501"


def _private_dir(path: Path) -> Path:
    """A directory only this account, SYSTEM and Administrators can change (Users may read and run).

    The owner is left as Windows sets it (this account, or Administrators for an elevated process
    on some hosts); both are trusted by the runner, so no /setowner is needed.
    """
    path.mkdir(parents=True)
    _icacls(
        path,
        "/inheritance:r",
        "/grant:r",
        f"*{_current_user_sid()}:(OI)(CI)F",
        f"{_SID_ADMINISTRATORS}:(OI)(CI)F",
        f"{_SID_SYSTEM}:(OI)(CI)F",
        f"{_SID_USERS}:(OI)(CI)RX",
    )
    return path


def _user_writable_dir(path: Path, *, sid: str = _SID_USERS[1:]) -> Path:
    """A directory ``sid`` (every member of Users by default) may modify: the planted-uv situation.

    On Windows its owner is this account or Administrators, both trusted, so the grant alone is
    what makes it untrusted. On POSIX it is world-writable (``sid`` does not apply).
    """
    path.mkdir(parents=True)
    if sys.platform == "win32":
        _icacls(path, "/grant", f"*{sid}:(OI)(CI)M")
    else:
        path.chmod(0o777)
    return path


def _copy_uv(directory: Path) -> Path:
    """Copy the real uv into ``directory``: the copy sets UV to its own path when it runs."""
    assert _UV is not None
    target = directory / Path(_UV).name
    shutil.copy2(Path(_UV).resolve(), target)
    return target


def _uv_stand_in(directory: Path) -> Path:
    """A uv the runner can execute from ``directory`` whose own path the packed script reports.

    On POSIX a two-line wrapper that names itself and hands over to the real uv; on Windows a copy
    of uv.exe (no test can write an .exe), which reports itself through uv's own UV variable.
    """
    assert _UV is not None
    if sys.platform == "win32":
        return _copy_uv(directory)
    wrapper = directory / "uv"
    wrapper.write_text(
        f'#!/bin/sh\nPWSHPY_TEST_UV="$0"\nexport PWSHPY_TEST_UV\nexec "{_UV}" "$@"\n', encoding="utf-8", newline="\n"
    )
    wrapper.chmod(0o755)
    return wrapper


def _root_only_writable(path: Path) -> bool:
    """Whether the runner's root rule accepts ``path`` (not a symlink, no POSIX ACL checked here).

    Root owns the file and its directory and neither has group or other write; every directory
    above is root-owned and has neither unless it is sticky.
    """
    if path.is_symlink():
        return False
    for item in (path, path.parent):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            return False
    for ancestor in path.parent.resolve().parents:
        info = ancestor.stat()
        sticky = bool(info.st_mode & 0o1000)
        if info.st_uid != 0 or (info.st_mode & 0o022 and not sticky):
            return False
    return True


def _trusted_uv(tmp_path: Path) -> Path | None:
    """A uv an elevated run must accept, or None where this host cannot provide one.

    Windows: a copy in a directory only this account, SYSTEM and Administrators may change. POSIX:
    the system-wide /usr/local/bin/uv when root owns it, since no unprivileged test can create one.
    """
    if sys.platform == "win32":
        return _copy_uv(_private_dir(tmp_path / "trusted-bin"))
    system_wide = Path(_SYSTEM_WIDE_UV)
    if system_wide.is_file() and _root_only_writable(system_wide):
        return system_wide
    return None


#: Where an elevated run judges its pack cache ([98]). A runner forced elevated answers "trusted" there
#: unless the test is about that check: a test's cache sits in its temp directory, below one the test's
#: own account owns, which the POSIX root rule refuses (and a stand-in declared Windows off Windows has
#: no drive path to judge).
_CACHE_CHECK = "function Get-PwshPyCacheDistrust {"


def _trusting_the_cache(text: str) -> str:
    """``text`` with the elevated run's cache check answering "trusted" (one counted substitution)."""
    assert text.count(_CACHE_CHECK) == 1
    return text.replace(_CACHE_CHECK, f"{_CACHE_CHECK}\n    return ''\n}}\n\nfunction Get-PwshPyCacheDistrustOnHost {{")


#: Where the elevated .ps1 run judges the empty file it points GIT_CONFIG_GLOBAL at (Windows).
_GIT_CONFIG_CHECK = "$PwshPyGitGlobalWhy = Get-PwshPyEmptyFileDistrust -Path $PwshPyGitGlobal"


def _trusting_the_git_config(text: str) -> str:
    """``text`` with the elevated run's check of its empty git configuration file answering "trusted",
    for a test whose subject is something else (one counted substitution; nothing is created)."""
    assert text.count(_GIT_CONFIG_CHECK) == 1
    return text.replace(_GIT_CONFIG_CHECK, "$PwshPyGitGlobalWhy = ''")


#: The runner's POSIX trusted owners (root alone); a test widens it so its own files stand in for
#: root-owned ones, which no unprivileged test can create.
_TRUSTED_UIDS = "$PwshPyTrustedUids = @('0')"


def _elevation_stand_in(
    runner: Path,
    output: Path,
    *,
    elevated: str,
    running_sid: str | None = None,
    trust_my_uid: bool = False,
    real_data_home: bool = False,
    data_scratch: Path | None = None,
    judge_cache: bool = False,
) -> Path:
    """The whole runner with its elevation probe answering ``elevated`` (a PowerShell expression).

    One exact substitution and nothing else, so the uv lookup, the pin and the run are the shipped
    code. Without -PwshPyElevate nothing relaunches, so this probes the uv trust rule alone.
    ``running_sid`` also makes the runner believe it runs as that SID ('S-1-5-18' stands in for a
    SYSTEM scheduled task, which no test can start without changing the host). ``trust_my_uid``
    (POSIX) counts this test's uid as root for ownership, so a layout the test builds can pass or
    fail on everything but its owner. A runner forced elevated also takes ``data_home(output.parent)``
    for its account's data directory (see :func:`elevation_doubles.with_data_home`) unless
    ``real_data_home`` keeps the passwd or known-folder lookup; that real directory is then taken as
    trusted (see :func:`elevation_doubles.without_python_dir_check`). ``data_scratch`` replaces
    ``output.parent`` there, for a data directory the runner file cannot sit next to (a share). A runner
    forced elevated takes its pack cache as trusted (:func:`_trusting_the_cache`) unless ``judge_cache``
    keeps that check ([98]).
    """
    text = runner.read_text(encoding="utf-8")
    if elevated != "$false" and not judge_cache:
        text = _trusting_the_cache(text)
    if elevated != "$false" and not real_data_home:
        text = with_data_home(text, data_scratch or output.parent, powershell=True)
    elif elevated != "$false":
        text = without_python_dir_check(text, powershell=True)
    if trust_my_uid and sys.platform != "win32":
        assert text.count(_TRUSTED_UIDS) == 1
        text = text.replace(_TRUSTED_UIDS, f"$PwshPyTrustedUids = @('0', '{os.getuid()}')")
    probes = [("function Test-PwshPyElevated {", elevated, "Test-PwshPyElevatedOnHost")]
    if running_sid is not None:
        probes.append(("function Get-PwshPyRunningUserSid {", f"'{running_sid}'", "Get-PwshPyRunningUserSidOnHost"))
    for old, answer, renamed in probes:
        assert text.count(old) == 1, old
        text = text.replace(old, f"{old}\n    return {answer}\n}}\n\nfunction {renamed} {{")
    output.write_text(text, encoding="utf-8")
    return output


def _run_with_path(
    tmp_path: Path,
    runner: Path,
    *arguments: str,
    path_first: Sequence[Path | str],
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    real_probe: bool = False,
    inherit_path: bool = True,
    install_uv: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a runner with exactly ``path_first`` ahead of the inherited PATH (no real uv added).

    A ``str`` entry reaches PATH exactly as spelled; a ``Path`` would normalise its separators.

    ``inherit_path=False`` makes ``path_first`` the whole PATH, for a test that must control every
    directory on it (the interpreter is started by its full path either way). ``install_uv`` drops
    ``-PwshPyNoInstallUv``, only for a runner whose installer download a test has replaced.

    The interpreter and library variables a suite run may carry (bmk's VIRTUAL_ENV among them) are
    dropped unless ``env`` sets them, so uv's interpreter choice is the one the test arranged, and an
    env file a test plants can set PYTHONPATH: uv does not override a variable the environment already
    holds (measured: with PYTHONPATH in the suite's environment the env-file control under make test
    never ran its plant). Python downloads are off unless ``env`` says otherwise ([92]); a runner
    forced elevated runs on this host's managed Python (read only), or the test skips when there is
    none, unless ``env`` names the managed directory itself. ``real_probe`` keeps a plain runner's own
    elevation probe on an elevated host (see :func:`_as_this_invoker`); there the probe says elevated
    and the runner names uv's cache from its account ([X22 C1]), so a copy answers that lookup inside
    ``tmp_path`` instead, never the real account's cache ([93], [111]).
    """
    assert _PWSH is not None
    if not real_probe:
        runner = _as_this_invoker(runner, arguments)
    elif _host_elevated():
        own_cache = runner.with_name(runner.stem + ".own-cache.ps1")
        own_cache.write_text(
            with_account_cache_home(runner.read_text(encoding="utf-8"), tmp_path, powershell=True), encoding="utf-8"
        )
        runner = own_cache
    child_env = {k: v for k, v in os.environ.items() if k not in (*INTERPRETER_VARIABLES, *LIBRARY_VARIABLES)}
    child_env.update({**_isolated_cache(tmp_path), "PYTHONIOENCODING": "utf-8", "UV_PYTHON_DOWNLOADS": "never"})
    if f"{_STAND_IN_PROBE}$true" in runner.read_text(encoding="utf-8") and "UV_PYTHON_INSTALL_DIR" not in (env or {}):
        child_env.update(managed_python_env(tmp_path, managed=_needs_managed_python()))
    child_env.update(env or {})
    # Never the real user's uv cache ([93]), unless the test names its own.
    child_env.setdefault("UV_CACHE_DIR", str(session_uv_cache()))
    child_env.pop("PWSHPY_TEST_UV", None)
    inherited = [os.environ.get("PATH", "")] if inherit_path else []
    child_env["PATH"] = os.pathsep.join([*map(str, path_first), *inherited])
    never_install = [] if install_uv else [_NEVER_INSTALL_UV]
    return subprocess.run(  # noqa: S603 - fixed argv built from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(runner), *never_install, *arguments],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
        env=child_env,
        cwd=cwd,
    )


def _reported_uv(result: subprocess.CompletedProcess[str]) -> Path | None:
    """The uv the packed script says started it, or None when the script never ran."""
    for line in result.stdout.splitlines():
        if line.startswith("uv:") and line[3:]:
            return Path(line[3:].strip())
    return None


def _same(reported: Path | None, expected: Path) -> bool:
    return reported is not None and reported.exists() and reported.samefile(expected)


def _trust_runner(tmp_path: Path, *, elevated: str, trust_my_uid: bool = False) -> Path:
    entry = tmp_path / "which_uv.py"
    entry.write_text(_ENTRY_REPORTING_UV)
    manifest = pack_script(entry)
    return _elevation_stand_in(
        Path(manifest.output_path), tmp_path / "trust_stand_in.ps1", elevated=elevated, trust_my_uid=trust_my_uid
    )


_needs_a_trusted_uv = "this host cannot provide a uv an elevated run may trust (see _trusted_uv)"


@pytest.mark.local_only
def test_an_elevated_run_skips_a_uv_a_non_admin_can_modify(tmp_path: Path) -> None:
    """[56]: a uv planted FIRST on PATH in a directory non-admins can write is not what runs elevated."""
    trusted = _trusted_uv(tmp_path)
    if trusted is None:
        pytest.skip(_needs_a_trusted_uv)
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin"))
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[planted.parent, trusted.parent])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _same(_reported_uv(result), trusted), result.stdout + result.stderr
    assert _skip_line(result, planted), result.stderr


def _skip_line(result: subprocess.CompletedProcess[str], uv: Path) -> str:
    """The runner's skip line for ``uv``; case-folded on Windows, where Get-Command reports uv.EXE."""
    wanted = os.path.normcase(f"{_UV_SKIPPED} {uv}:")
    return next((line for line in result.stderr.splitlines() if os.path.normcase(line).startswith(wanted)), "")


@pytest.mark.local_only
def test_an_elevated_run_names_why_it_skipped_each_uv(tmp_path: Path) -> None:
    """Every PATH hit is judged and named: a directory Users may modify, then one another account may.

    Pins the parsed reason as well as the skip, so a misread ACL or listing cannot pass for a check.
    On Windows "another account" is a specific user SID (this domain's Guest); on POSIX, where an
    elevated run is root, any non-root owner is one.
    """
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin"))
    if sys.platform == "win32":
        other_sid = _other_user_sid()
        other = _user_writable_dir(tmp_path / "other-bin", sid=other_sid)
        expected_planted = f"its directory {planted.parent} lets {_SID_USERS[1:]} change it"
        expected_other = f"its directory {other} lets {other_sid} change it"
    else:
        other = tmp_path / "other-bin"
        other.mkdir()
        other.chmod(0o755)
        expected_planted = f"its directory {planted.parent} is writable by every account"
    other_uv = _uv_stand_in(other)
    if sys.platform != "win32":
        expected_other = f"its directory {other} is owned by uid {owned_by_another_account(other)}, not root"
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[planted.parent, other])
    assert expected_planted in _skip_line(result, planted), result.stderr
    assert expected_other in _skip_line(result, other_uv), result.stderr


@pytest.mark.local_only
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_an_elevated_run_accepts_a_uv_only_the_running_account_can_modify(tmp_path: Path) -> None:
    """An elevated administrator's own per-user uv runs: owned by and writable only by that account.

    The running account already holds everything an elevated run could do with that uv, so only
    another principal's write access makes it a privilege escalation (user decision on [56]).
    """
    user_sid = _current_user_sid()
    own = _private_dir(tmp_path / "own-bin")
    own_uv = _copy_uv(own)
    for item in (own, own_uv):
        _icacls(item, "/setowner", f"*{user_sid}")
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[own])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _same(_reported_uv(result), own_uv), result.stdout + result.stderr
    assert not _skip_line(result, own_uv), result.stderr


@pytest.mark.local_only
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
@pytest.mark.parametrize("group_sid", _BROAD_GROUP_SIDS, ids=["users", "authenticated-users", "everyone"])
def test_an_elevated_run_refuses_a_uv_a_broad_group_can_modify(tmp_path: Path, group_sid: str) -> None:
    """Users, Authenticated Users and Everyone write access still refuses a uv; the next one runs."""
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin", sid=group_sid))
    trusted = _trusted_uv(tmp_path)
    assert trusted is not None
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[planted.parent, trusted.parent])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _same(_reported_uv(result), trusted), result.stdout + result.stderr
    assert f"its directory {planted.parent} lets {group_sid} change it" in _skip_line(result, planted), result.stderr


@pytest.mark.local_only
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_as_system_the_strict_rule_is_unchanged(tmp_path: Path) -> None:
    """Running as SYSTEM, a logged-on account's own uv is refused; a uv only SYSTEM's peers control runs.

    The runner's identity probe answers S-1-5-18 here (starting a real SYSTEM task would change the
    host); since SYSTEM is already trusted, the running account adds nothing to the trusted set.
    Everything under this account's profile is therefore refused (the account owns the profile), so
    the positive case pins a system binary: whoami.exe stands in for uv, is accepted, and fails on
    the argument ``run`` with its own exit code 1. The managed-Python directory and the empty git
    configuration file this test hands the run sit in the same profile, so their checks are taken as
    passed (the uv is the subject; [138]: the git file's check refused the run with 127 before uv was
    looked for, rightly, since SYSTEM trusts no account's profile).
    """
    user_sid = _current_user_sid()
    own = _private_dir(tmp_path / "own-bin")
    own_uv = _copy_uv(own)
    for item in (own, own_uv):
        _icacls(item, "/setowner", f"*{user_sid}")
    entry = tmp_path / "which_uv.py"
    entry.write_text(_ENTRY_REPORTING_UV)
    manifest = pack_script(entry)
    runner = _elevation_stand_in(
        Path(manifest.output_path), tmp_path / "system_stand_in.ps1", elevated="$true", running_sid="S-1-5-18"
    )
    text = without_python_dir_check(runner.read_text(encoding="utf-8"), powershell=True)
    runner.write_text(_trusting_the_git_config(text), encoding="utf-8")
    searched = _run_with_path(tmp_path, runner, path_first=[own])
    assert f"its directory {own} is owned by {user_sid}" in _skip_line(searched, own_uv), searched.stderr
    assert not _skip_line(searched, own_uv).endswith("not root"), searched.stderr
    # PATH beyond own-bin is this host's, so a uv elsewhere might still pass; the account's own did not run.
    assert not _same(_reported_uv(searched), own_uv), searched.stdout + searched.stderr
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    whoami = system_root / "System32" / "whoami.exe"
    pinned = _run_with_path(tmp_path, runner, "-PwshPyUv", str(whoami), path_first=[own])
    assert _PIN_REFUSED not in pinned.stderr, pinned.stderr
    assert pinned.returncode == 1, pinned.stdout + pinned.stderr
    assert _reported_uv(pinned) is None, pinned.stdout


@pytest.mark.local_only
def test_an_elevated_run_uses_a_trusted_pinned_uv(tmp_path: Path) -> None:
    """-PwshPyUv names the uv outright; elevated, a trusted pin wins over a planted PATH hit."""
    trusted = _trusted_uv(tmp_path)
    if trusted is None:
        pytest.skip(_needs_a_trusted_uv)
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin"))
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, "-PwshPyUv", str(trusted), path_first=[planted.parent])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _same(_reported_uv(result), trusted), result.stdout + result.stderr


@pytest.mark.local_only
def test_an_elevated_run_refuses_an_untrusted_pinned_uv(tmp_path: Path) -> None:
    """Elevated, a pin a non-admin can modify is refused with exit 127; the script never runs."""
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin"))
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, "-PwshPyUv", str(planted), path_first=[])
    assert result.returncode == 127, result.stdout + result.stderr
    assert os.path.normcase(f"{_PIN_REFUSED} -PwshPyUv: {planted}:") in os.path.normcase(result.stderr), result.stderr
    assert _reported_uv(result) is None, result.stdout


@pytest.mark.local_only
def test_an_unelevated_run_still_takes_the_first_uv_on_path(tmp_path: Path) -> None:
    """Control: not elevated, the lookup is unchanged - the first uv on PATH runs, trusted or not."""
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin"))
    runner = _trust_runner(tmp_path, elevated="$false")
    result = _run_with_path(tmp_path, runner, path_first=[planted.parent, Path(_UV or "uv").parent])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _same(_reported_uv(result), planted), result.stdout + result.stderr
    assert _UV_SKIPPED not in result.stderr, result.stderr


@pytest.mark.local_only
def test_an_unelevated_run_uses_the_pinned_uv_as_is(tmp_path: Path) -> None:
    """Not elevated, a pin is taken as given, ahead of whatever PATH holds."""
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin"))
    runner = _trust_runner(tmp_path, elevated="$false")
    result = _run_with_path(tmp_path, runner, "-PwshPyUv", str(planted), path_first=[Path(_UV or "uv").parent])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _same(_reported_uv(result), planted), result.stdout + result.stderr


@pytest.mark.parametrize(
    "arguments",
    [pytest.param(["-PwshPyUv", "relative/uv"], id="relative"), pytest.param(["-PwshPyUv"], id="missing")],
)
def test_a_pinned_uv_must_be_an_absolute_path(tmp_path: Path, arguments: list[str]) -> None:
    """A relative or missing -PwshPyUv value is a usage error (64), before anything is unpacked."""
    entry = tmp_path / "which_uv.py"
    entry.write_text(_ENTRY_REPORTING_UV)
    manifest = pack_script(entry)
    result = _run(Path(manifest.output_path), *arguments, env=_isolated_cache(tmp_path))
    assert result.returncode == _USAGE_EXIT, result.stdout + result.stderr
    assert "-PwshPyUv" in result.stderr, result.stderr
    assert _reported_uv(result) is None, result.stdout
    assert not (tmp_path / "cache").exists(), "the payload was unpacked for a malformed switch"


@pytest.mark.local_only
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows symbolic links and ACLs")
def test_an_elevated_run_follows_a_symlinked_uv_to_its_target(tmp_path: Path) -> None:
    """A uv.exe symlink is judged by its target too: an admin-only link into a writable dir is skipped."""
    trusted = _trusted_uv(tmp_path)
    if trusted is None:
        pytest.skip(_needs_a_trusted_uv)
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin"))
    links = _private_dir(tmp_path / "links-bin")
    link = links / "uv.exe"
    link.symlink_to(planted)
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[links, trusted.parent])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _same(_reported_uv(result), trusted), result.stdout + result.stderr
    assert _skip_line(result, link), result.stderr


@pytest.mark.local_only
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="the UAC relay exists only on Windows")
def test_the_elevated_child_keeps_the_pin_and_relays_what_it_skipped(tmp_path: Path) -> None:
    """-PwshPyUv survives the UAC relaunch, and the child's skip lines reach the parent's stderr.

    The stand-in answers "elevated" only in the relaunched child (the one holding -PwshPyRelay),
    as UAC would. The child skips the planted PATH hit, names it through the relay, and runs the
    pinned uv; the pinned one must also pass the trust rule there.
    """
    trusted = _trusted_uv(tmp_path)
    if trusted is None:
        pytest.skip(_needs_a_trusted_uv)
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin"))
    entry = tmp_path / "which_uv.py"
    entry.write_text(_ENTRY_REPORTING_UV)
    manifest = pack_script(entry)
    stand_in = _whole_runner_stand_in(
        Path(manifest.output_path), tmp_path / "relay_stand_in.ps1", elevated="[bool]$PwshPyRelay"
    )
    pinned = _run_with_path(
        tmp_path, stand_in, "-PwshPyElevate", "-PwshPyUv", str(trusted), path_first=[planted.parent]
    )
    assert pinned.returncode == 0, pinned.stdout + pinned.stderr
    assert _same(_reported_uv(pinned), trusted), pinned.stdout + pinned.stderr
    searched = _run_with_path(tmp_path, stand_in, "-PwshPyElevate", path_first=[planted.parent, trusted.parent])
    assert searched.returncode == 0, searched.stdout + searched.stderr
    assert _same(_reported_uv(searched), trusted), searched.stdout + searched.stderr
    assert _skip_line(searched, planted), searched.stderr


# --- round 2: ancestors, a planted ls / id, POSIX root PATH ------------------------------------

_WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and root")


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_an_elevated_run_refuses_a_locked_uv_under_a_deletable_ancestor(tmp_path: Path) -> None:
    """A locked leaf under an ancestor Users may modify (Delete, DeleteChild) is refused.

    Users could rename the locked directory away and put their own in its place between the check
    and the exec, so the lock on the leaf is worth nothing (like a Tools folder under the C: root
    that inherits the root's Modify grant).
    """
    deletable = _user_writable_dir(tmp_path / "deletable")
    locked = _private_dir(deletable / "locked-bin")
    uv = _copy_uv(locked)
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[locked])
    assert f"its ancestor {deletable} lets {_SID_USERS[1:]} change it" in _skip_line(result, uv), result.stderr
    assert not _same(_reported_uv(result), uv), result.stdout + result.stderr


#: Judges directories with the artefact's own walk: each one gets a file name that does not exist,
#: so a directory and every ancestor that pass leave only the missing file's unreadable ACL.
_DIRECTORY_TRUST_PROBE = r"""
param([string] $Artefact, [string] $RunningSid)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Artefact, [ref]$tokens, [ref]$errors)
$language = 'System.Management.Automation.Language'
$functions = $ast.FindAll({ param($n) $n -is ("$language.FunctionDefinitionAst" -as [type]) }, $false)
foreach ($definition in $functions) { . ([ScriptBlock]::Create($definition.Extent.Text)) }
$wanted = '^\$PwshPy(TrustedSids|FileWriteRights|DirectoryWriteRights|AncestorWriteRights|TrustedUids)$'
$assignments = $ast.FindAll({
    param($n)
    $n -is ("$language.AssignmentStatementAst" -as [type]) -and $n.Left.Extent.Text -match $wanted
}, $false)
foreach ($assignment in $assignments) { . ([ScriptBlock]::Create($assignment.Extent.Text)) }
$PwshPyOnWindows = $true
$PwshPyRelay = ''
if ($RunningSid) { $script:ForcedSid = $RunningSid; function Get-PwshPyRunningUserSid { return $script:ForcedSid } }
$systemProfile = Join-Path $env:SystemRoot 'System32\config\systemprofile'
foreach ($directory in @($env:USERPROFILE, $env:LOCALAPPDATA, $systemProfile)) {
    Write-Output ($directory + '|' + (Get-PwshPyUvDistrust -Path (Join-Path $directory 'pwshpy-x12-absent.exe')))
}
"""


def _directory_trust(tmp_path: Path, *, running_sid: str = "") -> dict[str, str]:
    assert _PWSH is not None
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest = pack_script(_project(tmp_path))
    probe = tmp_path / "directory_trust.ps1"
    probe.write_text(_DIRECTORY_TRUST_PROBE, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed argv from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(probe), str(manifest.output_path), running_sid],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split("|", 1) for line in result.stdout.splitlines() if "|" in line)


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_the_profile_and_the_system_profile_pass_the_ancestor_walk(tmp_path: Path) -> None:
    """Under the running-account rule a per-user profile and SYSTEM's profile are trusted, ancestors included.

    Only the probe's absent file is left to fail; under SYSTEM the user's profile is refused.
    """
    verdicts = _directory_trust(tmp_path)
    assert len(verdicts) == 3, verdicts
    for directory, why in verdicts.items():
        assert why.startswith("the ACL of ") and "pwshpy-x12-absent.exe" in why, (directory, why)
    as_system = _directory_trust(tmp_path / "system", running_sid="S-1-5-18")
    profile = os.environ["USERPROFILE"]
    assert "is owned by" in as_system[profile] or " lets " in as_system[profile], as_system
    system_profile = next(key for key in as_system if key.lower().endswith("systemprofile"))
    assert as_system[system_profile].startswith("the ACL of "), as_system


def _marker_double(directory: Path, name: str, marker: Path, body: str) -> None:
    tool = directory / name
    tool.write_text(f'#!/bin/sh\necho {name} >> "{marker}"\n{body}\n', encoding="utf-8", newline="\n")
    tool.chmod(0o755)


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
def test_an_elevated_posix_run_never_runs_a_planted_ls(tmp_path: Path) -> None:
    """As root the .ps1 lists modes with the ls from the fixed system dirs, never one planted on PATH.

    The planted ls claims every path is root-owned 0755; trusting it would accept the planted uv.
    """
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin"))
    marker = tmp_path / "ran.txt"
    _marker_double(planted.parent, "ls", marker, "echo 'drwxr-xr-x 2 0 0 4096 Jan  1 00:00 x'")
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[planted.parent])
    assert not marker.exists(), "the planted ls ran"
    assert not _same(_reported_uv(result), planted), result.stdout + result.stderr
    assert "is writable by every account" in _skip_line(result, planted), result.stderr


# --- [107] elevated runs: uv sees only trusted PATH directories, the script all of them -----------

_ENTRY_REPORTING_PATH = """import os
print("path:" + os.environ.get("PATH", ""))
"""

_NO_GIT_HINT = "pwshpy: no git on uv's PATH: "


def _path_runner(tmp_path: Path, *, elevated: str) -> tuple[Path, list[Path]]:
    """A runner whose script reports its PATH, plus the PATH head holding a uv it trusts (see
    :func:`_python_runner`: on POSIX this test's own uid counts as root's)."""
    return _python_runner(tmp_path, elevated=elevated, source=_ENTRY_REPORTING_PATH)


def _reported_path(result: subprocess.CompletedProcess[str]) -> str:
    lines = [line[5:] for line in result.stdout.splitlines() if line.startswith("path:")]
    assert len(lines) == 1, result.stdout + result.stderr
    return lines[0]


def _path_powershell_sees(path: str) -> str:
    """The PATH a runner started with ``path`` reads as its caller's: pwsh puts its own directory in
    front at startup, which would otherwise pass for an entry uv put there."""
    assert _PWSH is not None
    result = subprocess.run(  # noqa: S603 - fixed argv built from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-Command", "[Console]::Out.Write($env:PATH)"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=True,
        env={**os.environ, "PATH": path},
    )
    return result.stdout


def _git_requirement(tmp_path: Path) -> str:
    """A git+file requirement, so uv runs git to fetch it (the same source as the .sh e2e tests)."""
    return f"pwshpy-git-probe @ git+{write_git_source(tmp_path).as_uri()}"


def _writable_by_others(directory: Path) -> str:
    """Why the runner's walk refuses ``directory`` made by :func:`_user_writable_dir` (default sid)."""
    if sys.platform == "win32":
        return f"its directory {directory} lets {_SID_USERS[1:]} change it"
    return f"its directory {directory} is writable by every account"


@pytest.mark.local_only
def test_an_elevated_run_leaves_an_untrusted_directory_off_uvs_path(tmp_path: Path) -> None:
    """[107]: a PATH directory other accounts can modify is named and left off uv's PATH; the script
    still gets the caller's whole PATH, behind the entries uv puts in front (user decision: "Keep uv's
    entry first")."""
    planted = _user_writable_dir(tmp_path / "planted-bin")
    runner, uv_dirs = _path_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[planted, *uv_dirs])
    assert result.returncode == 0, result.stdout + result.stderr
    leaving = f"pwshpy: leaving {planted} off uv's PATH: {_writable_by_others(planted)}"
    assert leaving in result.stderr, result.stderr
    caller = _path_powershell_sees(os.pathsep.join([str(planted), *map(str, uv_dirs), os.environ.get("PATH", "")]))
    seen = _reported_path(result)
    assert seen.endswith(os.pathsep + caller), seen
    assert seen[: -len(caller)].strip(os.pathsep), f"uv put nothing in front of the caller's PATH: {seen}"


@pytest.mark.local_only
def test_a_run_that_is_not_elevated_leaves_uvs_path_alone(tmp_path: Path) -> None:
    """Control: not elevated, nothing is filtered or named."""
    planted = _user_writable_dir(tmp_path / "planted-bin")
    runner, uv_dirs = _path_runner(tmp_path, elevated="$false")
    result = _run_with_path(tmp_path, runner, path_first=[planted, *uv_dirs])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "off uv's PATH" not in result.stderr, result.stderr
    assert str(planted) in _reported_path(result).split(os.pathsep), result.stdout


@pytest.mark.local_only
def test_a_run_that_is_not_elevated_ignores_an_inherited_path_hand_over(tmp_path: Path) -> None:
    """[107]: the variables an elevated run hands the shim, inherited by an unelevated run, must not
    replace the PATH its script gets (the .ps1 always starts the shim, unlike the .sh)."""
    planted = _user_writable_dir(tmp_path / "planted-bin")
    runner, uv_dirs = _path_runner(tmp_path, elevated="$false")
    bogus = str(tmp_path / "not-the-callers-path")
    hand_over = {"PWSHPY_PACK_PATH": bogus, "PWSHPY_PACK_UV_PATH": bogus}
    result = _run_with_path(tmp_path, runner, path_first=[planted, *uv_dirs], env=hand_over)
    assert result.returncode == 0, result.stdout + result.stderr
    seen = _reported_path(result).split(os.pathsep)
    assert bogus not in seen, seen
    assert str(planted) in seen, seen


@pytest.mark.local_only
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs and a real git.exe")
def test_an_elevated_windows_run_never_runs_a_planted_git(tmp_path: Path) -> None:
    """[107] as measured: a git.exe planted in a directory Authenticated Users can modify is not the git uv runs.

    The stand-in is a copy of whoami.exe: run as git, it fails uv's first git call naming its own path.
    uv still fetches the source with a trusted git (the fetch leaves its FETCH_HEAD in the run's uv
    cache, the one :func:`_elevation_stand_in` names inside ``tmp_path``; see
    :func:`fetched_git_source`), so a missing path is not a run that never reached git.
    """
    planted = _user_writable_dir(tmp_path / "planted-git", sid=_BROAD_GROUP_SIDS[1])
    shutil.copy(Path(os.environ["SYSTEMROOT"]) / "System32" / "whoami.exe", planted / "git.exe")
    trusted = _trusted_uv(tmp_path)
    if trusted is None or shutil.which("git") is None:
        pytest.skip("needs a trusted uv and a real git on this host")
    entry = tmp_path / "needs_git.py"
    entry.write_text(f'# /// script\n# dependencies = ["{_git_requirement(tmp_path)}"]\n# ///\nprint("ran")\n')
    runner = _elevation_stand_in(Path(pack_script(entry).output_path), tmp_path / "git_stand_in.ps1", elevated="$true")
    result = _run_with_path(
        tmp_path, runner, path_first=[planted, trusted.parent], env={"UV_PYTHON_DOWNLOADS": "never"}
    )
    assert fetched_git_source(default_uv_cache_dir(tmp_path)), "uv fetched no git source: " + result.stderr
    assert str(planted / "git.exe") not in result.stderr, result.stderr
    assert f"pwshpy: leaving {planted} off uv's PATH: " in result.stderr, result.stderr


def _planted_git_dir(tmp_path: Path, marker: Path) -> tuple[Path, Path]:
    """A directory other accounts can modify holding a git, and that git; it never runs a real one.

    POSIX: a script recording that it ran. Windows: a copy of whoami.exe (no test can write an .exe).
    """
    planted = _user_writable_dir(tmp_path / "planted-git-bin")
    if sys.platform == "win32":
        git = planted / "git.exe"
        shutil.copy(Path(os.environ["SYSTEMROOT"]) / "System32" / "whoami.exe", git)
        return planted, git
    _marker_double(planted, "git", marker, "exit 1")
    return planted, planted / "git"


@pytest.mark.local_only
@pytest.mark.skipif(shutil.which("git") is None, reason="needs a real git")
@pytest.mark.parametrize(
    ("gits", "hinted"),
    [("planted-only", True), ("planted-and-trusted", False), ("none", False)],
    ids=["planted-only", "planted-and-trusted", "none"],
)
def test_an_elevated_run_names_why_uv_has_no_git(tmp_path: Path, gits: str, hinted: bool) -> None:
    """[107]: when every git on the caller's PATH sits in a directory left off uv's PATH, one stderr line
    says a git dependency will fail and why; none when a trusted git is left, or when there is no git.

    The PATH is exactly the one listed, so no git elsewhere on this host's PATH decides the case.
    """
    real_git = Path(str(shutil.which("git"))).parent
    if sys.platform != "win32" and not _root_only_writable(real_git / "git"):
        pytest.skip(f"the real git in {real_git} is not one a root run trusts")
    marker = tmp_path / "git-ran.txt"
    planted, planted_git = _planted_git_dir(tmp_path, marker)
    first: list[Path] = {"planted-only": [planted], "planted-and-trusted": [planted, real_git], "none": []}[gits]
    # Windows keeps its system directories, which hold no git; the runner itself needs no PATH.
    system: list[Path] = [Path(os.environ["SYSTEMROOT"]) / "System32"] if sys.platform == "win32" else []
    runner, uv_dirs = _path_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[*first, *uv_dirs, *system], inherit_path=False)
    assert result.returncode == 0, result.stdout + result.stderr
    hints = [line for line in result.stderr.splitlines() if line.startswith(_NO_GIT_HINT)]
    assert len(hints) == (1 if hinted else 0), result.stderr
    if hinted:
        assert str(planted_git) in hints[0], hints[0]
        only = (
            "in a directory no other account can modify (e.g. a system-wide install)"
            if sys.platform == "win32"
            else "in a directory only root can modify"
        )
        assert only in hints[0], hints[0]
    assert not marker.exists(), "the hint ran git"


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.skipif(shutil.which("git") is None, reason="needs a real git")
@pytest.mark.parametrize(
    ("elevated", "planted_runs"),
    [pytest.param("$true", False, id="root"), pytest.param("$false", True, id="not-root-control")],
)
def test_a_posix_root_run_never_runs_a_git_planted_on_path(tmp_path: Path, elevated: str, planted_runs: bool) -> None:
    """[107] on the .ps1 under POSIX pwsh: a git planted first on PATH in a world-writable directory is not
    the git uv runs as root for a git+file dependency; the control, not root, runs it.

    The planted git records that it ran, then hands over to the real one, so both arms reach the fetch:
    uv's fetch appears in the cache, so a missing marker is not a run that never reached git. Offline,
    so both arms then fail to build the source. local_only: it needs a uv-managed Python and a git only
    root can modify, which the CI runners do not guarantee."""
    managed = _needs_managed_python()
    real_git = Path(str(shutil.which("git")))
    if not _root_only_writable(real_git):
        pytest.skip(f"the real git {real_git} is not one a root run trusts")
    marker = tmp_path / "git-ran.txt"
    planted = _user_writable_dir(tmp_path / "planted-git-bin")
    _marker_double(planted, "git", marker, f'exec "{real_git}" "$@"')
    source = f'# /// script\n# dependencies = ["{_git_requirement(tmp_path)}"]\n# ///\nprint("ran")\n'
    runner, head = _python_runner(tmp_path, elevated=elevated, source=source)
    environment = {**interpreter_probe_env(tmp_path, managed=managed), "UV_OFFLINE": "1"}
    result = _run_with_path(tmp_path, runner, path_first=[planted, *head], env=environment, cwd=_clean_cwd(tmp_path))
    caches = (default_uv_cache_dir(tmp_path), tmp_path / "uv-cache")
    assert any(fetched_git_source(cache) for cache in caches), "uv fetched no git source: " + result.stderr
    assert marker.exists() is planted_runs, result.stdout + result.stderr
    leaving = f"pwshpy: leaving {planted} off uv's PATH: {_writable_by_others(planted)}"
    assert (leaving in result.stderr) is not planted_runs, result.stderr


# --- [146] elevated runs trust no network (UNC) path ---------------------------------------------

#: Why the runner's walk refuses a path on a network share, whatever ACL its server reports.
_NETWORK_PATH = "it is a network path, and an elevated run trusts only local paths"
#: Why it refuses a device-namespace path other than the extended-length drive form.
_DEVICE_PATH = "it is a device path, and an elevated run trusts only local drive paths"

#: Each way the runner can meet a UNC path, built from the plain ``\\server\share\...`` spelling.
_NETWORK_SPELLINGS: dict[str, Callable[[str], str]] = {
    "unc": lambda share: share,
    "forward-slashes": lambda share: share.replace("\\", "/"),
    "mixed-separators": lambda share: "\\/" + share[2:],
    "extended-length": lambda share: "\\\\?\\UNC\\" + share[2:],
}


def _admin_share(path: Path) -> str:
    """``path`` reached through this host's own administrative share (``\\\\localhost\\c$\\...``), or a skip.

    The server is this host, so it reports exactly the ACL the local walk reads: a test that sees the
    share refused sees the network rule at work, not a different ACL.
    """
    drive, rest = os.path.splitdrive(str(path))
    share = f"\\\\localhost\\{drive[:1].lower()}$" + rest
    if not Path(share).is_dir():
        pytest.skip(f"this host's administrative share {share} is not reachable")
    return share


_NETWORK_RULE_PROBE = r"""
param([string] $Artefact, [Parameter(ValueFromRemainingArguments = $true)] [string[]] $Paths)
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Artefact, [ref]$null, [ref]$null)
$language = 'System.Management.Automation.Language'
$functions = $ast.FindAll({ param($n) $n -is ("$language.FunctionDefinitionAst" -as [type]) }, $false)
foreach ($definition in $functions) { . ([ScriptBlock]::Create($definition.Extent.Text)) }
$PwshPyOnWindows = $true
foreach ($path in $Paths) {
    # The walk only for a refused spelling: it returns before touching the filesystem there, while a
    # local one would read ACLs this host may not have.
    $rule = Get-PwshPyNetworkDistrust -Path $path
    $walk = if ($rule) { Get-PwshPyUvDistrust -Path $path } else { '' }
    Write-Output ($path + "`t" + $rule + "`t" + $walk)
}
"""

#: Spellings the rule refuses as network paths, with either separator and the device prefixes.
_NETWORK_RULE_NETWORK = (
    r"\\server\share\bin\uv.exe",
    "//server/share/bin/uv.exe",
    r"\/server\share/bin\uv.exe",
    r"\\?\UNC\server\share\bin\uv.exe",
    r"\\?\unc\server\share\bin\uv.exe",
    "//?/UNC/server/share/bin/uv.exe",
    r"\\.\UNC\server\share\bin\uv.exe",
    r"\\server",
)
#: Device-namespace spellings the rule refuses: none of them is a plain drive path.
_NETWORK_RULE_DEVICE = (
    r"\\.\C:\bin\uv.exe",
    r"\\?\GLOBALROOT\Device\Mup\server\share\uv.exe",
    r"\\?\Volume{00000000-0000-0000-0000-000000000000}\bin\uv.exe",
    r"\\.\pipe\uv",
)
#: Local spellings the rule leaves to the ACL walk.
_NETWORK_RULE_LOCAL = (r"C:\bin\uv.exe", "c:/bin/uv.exe", r"\\?\C:\bin\uv.exe", "//?/C:/bin/uv.exe")


def test_the_network_rule_judges_every_unc_spelling(tmp_path: Path) -> None:
    """[146]: every UNC spelling is refused as a network path, by the rule and by the walk that calls it;
    other device-namespace paths are refused as device paths; drive paths, the extended-length one
    included, are left to the ACL walk. Pure string work, so it runs on any host with a PowerShell."""
    assert _PWSH is not None
    probe = tmp_path / "network_rule.ps1"
    probe.write_text(_NETWORK_RULE_PROBE, encoding="utf-8")
    artefact = pack_script(_project(tmp_path)).output_path
    spellings = (*_NETWORK_RULE_NETWORK, *_NETWORK_RULE_DEVICE, *_NETWORK_RULE_LOCAL)
    result = subprocess.run(  # noqa: S603 - fixed argv from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(probe), str(artefact), *spellings],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    verdicts = {path: (rule, walk) for path, rule, walk in (line.split("\t") for line in result.stdout.splitlines())}
    assert len(verdicts) == len(spellings), result.stdout
    for path in _NETWORK_RULE_NETWORK:
        assert verdicts[path] == (_NETWORK_PATH, _NETWORK_PATH), (path, verdicts[path])
    for path in _NETWORK_RULE_DEVICE:
        assert verdicts[path] == (_DEVICE_PATH, _DEVICE_PATH), (path, verdicts[path])
    for path in _NETWORK_RULE_LOCAL:
        assert verdicts[path] == ("", ""), (path, verdicts[path])


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
@pytest.mark.parametrize("spelling", sorted(_NETWORK_SPELLINGS))
def test_an_elevated_run_leaves_a_network_path_off_uvs_path(tmp_path: Path, spelling: str) -> None:
    """[146]: a PATH directory on a network share is named and left off uv's PATH, whatever ACL its server
    reports. The directory is private, so the same directory spelled locally passes the walk (the
    control) and the share, this host's own, reports that same ACL: only the network rule refuses it."""
    shared = _private_dir(tmp_path / "shared-bin")
    entry = _NETWORK_SPELLINGS[spelling](_admin_share(shared))
    runner, uv_dirs = _path_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[entry, shared, *uv_dirs])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"pwshpy: leaving {entry} off uv's PATH: {_NETWORK_PATH} (" in result.stderr, result.stderr
    assert f"pwshpy: leaving {shared} off" not in result.stderr, result.stderr


def _network_link(tmp_path: Path, target: str) -> Path:
    """A directory symlink in a private directory, pointing at the network path ``target``."""
    link = _private_dir(tmp_path / "links") / "net"
    link.symlink_to(target, target_is_directory=True)
    return link


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_an_elevated_run_leaves_a_link_to_a_network_path_off_uvs_path(tmp_path: Path) -> None:
    """[146]: a local PATH directory that is a link to a share is refused for its resolved target. The
    link and the directory holding it are private, so nothing else about it fails the walk."""
    share = _admin_share(_private_dir(tmp_path / "shared-bin"))
    link = _network_link(tmp_path, share)
    runner, uv_dirs = _path_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[link, *uv_dirs])
    assert result.returncode == 0, result.stdout + result.stderr
    refused = f"pwshpy: leaving {link} off uv's PATH: the link {link} points to {share}: {_NETWORK_PATH} ("
    assert refused in result.stderr, result.stderr


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_an_elevated_run_keeps_an_extended_length_local_path_on_uvs_path(tmp_path: Path) -> None:
    """[146] control: ``\\\\?\\C:\\...`` is the long spelling of a local path, so the network rule leaves
    it to the ACL walk, which keeps a private directory. [148]: on Windows PowerShell 5.1 too, whose
    Join-Path finds no drive in that spelling: the walk judges it as ``C:\\...``."""
    entry = "\\\\?\\" + str(_private_dir(tmp_path / "long-bin"))
    runner, uv_dirs = _path_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[entry, *uv_dirs])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"pwshpy: leaving {entry} off uv's PATH: " not in result.stderr, result.stderr


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_an_elevated_run_judges_an_extended_length_path_by_its_acl(tmp_path: Path) -> None:
    """[148] negative arm: ``\\\\?\\C:\\...`` naming a directory Users may modify is still left off, for its
    ACL (the reason names the Users grant), on pwsh 7 and Windows PowerShell 5.1 alike."""
    entry = "\\\\?\\" + str(_user_writable_dir(tmp_path / "long-open-bin"))
    runner, uv_dirs = _path_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[entry, *uv_dirs])
    assert result.returncode == 0, result.stdout + result.stderr
    leaving = f"pwshpy: leaving {entry} off uv's PATH: "
    refusal = next(line for line in result.stderr.splitlines() if line.startswith(leaving))
    assert f" lets {_SID_USERS[1:]} change it " in refusal, refusal


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
@pytest.mark.parametrize("reached", ["unc", "link"])
def test_an_elevated_run_skips_a_uv_on_a_network_path(tmp_path: Path, reached: str) -> None:
    """[146]: a uv found on a share, directly or through a local link, is skipped with the network reason,
    and the trusted local uv later on PATH runs. Its directory is private, so spelled locally it would
    pass (see test_an_elevated_run_accepts_a_uv_only_the_running_account_can_modify)."""
    shared_uv = _copy_uv(_private_dir(tmp_path / "shared-bin"))
    share = _admin_share(shared_uv.parent)
    trusted = _trusted_uv(tmp_path)
    assert trusted is not None
    entry = share if reached == "unc" else str(_network_link(tmp_path, share))
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[entry, trusted.parent])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _same(_reported_uv(result), trusted), result.stdout + result.stderr
    reason = _NETWORK_PATH if reached == "unc" else f"the link {entry} points to {share}: {_NETWORK_PATH}"
    assert reason in _skip_line(result, Path(entry) / shared_uv.name), result.stderr


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_an_elevated_run_refuses_a_pinned_uv_on_a_network_path(tmp_path: Path) -> None:
    """[146]: a -PwshPyUv pin on a share is refused like any untrusted pin (exit 127), naming the reason."""
    shared_uv = _copy_uv(_private_dir(tmp_path / "shared-bin"))
    pinned = _admin_share(shared_uv.parent) + "\\" + shared_uv.name
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, "-PwshPyUv", pinned, path_first=[])
    assert result.returncode == 127, result.stdout + result.stderr
    assert f"{_PIN_REFUSED} -PwshPyUv: {pinned}: {_NETWORK_PATH}." in result.stderr, result.stderr
    assert _reported_uv(result) is None, result.stdout


#: What an elevated run says in place of a remedy when a path below its account's data directory
#: (RoamingAppData) fails the network rule for being on a share itself (the rule's reason alone, not a
#: link's): neither the ACL change nor a fresh directory makes it local.
_NETWORK_REMEDY = "the running account's AppData is on a network share; an elevated run cannot use it"
#: The usual remedy a refused managed-Python path names on Windows, for every reason but the bare network one.
_WINDOWS_PYTHON_REMEDY = (
    "take every other account's write access off the path named above (only the running account, SYSTEM and"
    " Administrators may change it), or remove uv's managed-Python directory and install the Python again"
    " elevated (uv python install)"
)


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_an_elevated_run_names_a_network_appdata_when_it_refuses_the_git_configuration_file(tmp_path: Path) -> None:
    """[146]: with the running account's data directory (RoamingAppData) on a share, the empty git
    configuration file below it fails the network rule, and the run exits 127 saying the AppData is on a
    network share instead of the usual remedy, which would meet the same share on the next run. The
    data directory is a private one reached through this host's own administrative share, so only the
    network rule refuses it."""
    remote = Path(_admin_share(_private_dir(tmp_path / "remote")))
    entry = tmp_path / "git_global.py"
    entry.write_text(_ENTRY_REPORTING_GIT_GLOBAL)
    runner = _elevation_stand_in(
        Path(pack_script(entry).output_path), tmp_path / "git_stand_in.ps1", elevated="$true", data_scratch=remote
    )
    # The run drops the variable and names the directory itself; set, it keeps _run_with_path from
    # linking a managed Python the run never reaches.
    unused = {"UV_PYTHON_INSTALL_DIR": str(tmp_path / "unused")}
    result = _run_with_path(tmp_path, runner, path_first=[_UvLocation.directory], env=unused, cwd=_clean_cwd(tmp_path))
    assert result.returncode == 127, result.stdout + result.stderr
    refused = data_home(remote) / "pwshpy" / "empty.gitconfig"
    refusal = (
        f"{_GIT_GLOBAL_REFUSED} {refused}: {_NETWORK_PATH} (an elevated run points GIT_CONFIG_GLOBAL only at an"
        f" empty file no other account can modify): {_NETWORK_REMEDY}"
    )
    assert refusal in result.stderr, result.stderr
    assert "run again elevated" not in result.stderr, result.stderr
    assert _reported_git_global(result) is None, result.stdout


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_an_elevated_run_keeps_the_usual_remedy_for_a_managed_python_directory_linked_to_a_share(
    tmp_path: Path,
) -> None:
    """[146]: uv's managed-Python directory below a local data directory that is a link to a share is
    refused for the link's target, and the run exits 127 naming the usual remedy, not the network
    AppData one: the data directory is not redirected, and removing the link works. The reason line
    names the link and where it points. A data directory on a share itself stops at the git
    configuration file first and gets the network remedy (see
    test_an_elevated_run_names_a_network_appdata_when_it_refuses_the_git_configuration_file); the link
    and the share's directory are private, so only the network rule refuses them."""
    share = _admin_share(_private_dir(tmp_path / "remote-python"))
    runner, head = _python_runner(tmp_path, elevated="$true")
    environment = interpreter_probe_env(tmp_path, managed=Path(share))
    install = environment["UV_PYTHON_INSTALL_DIR"]
    result = _run_with_path(tmp_path, runner, path_first=head, env=environment, cwd=_clean_cwd(tmp_path))
    assert result.returncode == 127, result.stdout + result.stderr
    refusal = (
        f"{_PYTHON_DIR_REFUSED} {install}: the link {install} points to {share}: {_NETWORK_PATH} (an elevated"
        f" run lets uv search only a directory no other account can modify, nor anything below it):"
        f" {_WINDOWS_PYTHON_REMEDY}"
    )
    assert refusal in result.stderr, result.stderr
    assert _NETWORK_REMEDY not in result.stderr, result.stderr
    assert _reported_python(result) is None, result.stdout


#: Loads the runner's functions alone and prints, for each directory, why the managed-Python check
#: refuses it and the remedy an elevated run names for that reason; then the remedy for a device path
#: and for a link to a share, in the form the walk gives it (the e2e run with a managed-Python
#: directory linked to a share pins that form from the real walk).
_PYTHON_DIR_PROBE = r"""
param([string] $Artefact, [Parameter(ValueFromRemainingArguments = $true)] [string[]] $Directories)
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Artefact, [ref]$null, [ref]$null)
$language = 'System.Management.Automation.Language'
$functions = $ast.FindAll({ param($n) $n -is ("$language.FunctionDefinitionAst" -as [type]) }, $false)
foreach ($definition in $functions) { . ([ScriptBlock]::Create($definition.Extent.Text)) }
$PwshPyOnWindows = $true
foreach ($directory in $Directories) {
    $why = Get-PwshPyPythonDirDistrust -Path $directory
    Write-Output ($directory + "`t" + $why + "`t" + (Get-PwshPyPythonRemedy -Why $why))
}
$device = Get-PwshPyNetworkDistrust -Path '\\.\pipe\uv'
Write-Output ("device`t" + $device + "`t" + (Get-PwshPyPythonRemedy -Why $device))
$share = '\\server\share\python'
$link = "the link C:\data\uv\python points to ${share}: " + (Get-PwshPyNetworkDistrust -Path $share)
Write-Output ("link`t" + $link + "`t" + (Get-PwshPyPythonRemedy -Why $link))
"""


@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_a_managed_python_directory_climbing_to_a_share_root_is_refused_with_the_network_remedy(
    tmp_path: Path,
) -> None:
    """[146]: a managed-Python directory that does not exist yet is judged by the nearest directory that
    does; on a share with nothing below its root that is the share root, judged without the walk, so the
    network rule is asked there itself, and the remedy says the AppData is on a network share. A whole
    run cannot reach this branch: the data directory holding the managed Pythons also holds the git
    configuration file, judged first (see
    test_an_elevated_run_names_a_network_appdata_when_it_refuses_the_git_configuration_file), so the
    runner's own functions are asked here. The controls keep the usual remedy: another refusal (a device
    path), and a link to a share, whose reason names the link and where it points, and which removing
    the link cures. Read only: nothing below the share root exists, and nothing is created."""
    assert _PWSH is not None
    probe = tmp_path / "python_dir.ps1"
    probe.write_text(_PYTHON_DIR_PROBE, encoding="utf-8")
    artefact = pack_script(_project(tmp_path)).output_path
    missing = f"\\\\localhost\\c$\\pwshpy-146-missing-{uuid.uuid4().hex}\\uv\\python"
    result = subprocess.run(  # noqa: S603 - fixed argv from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(probe), str(artefact), missing],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    verdicts = {name: (why, remedy) for name, why, remedy in (line.split("\t") for line in result.stdout.splitlines())}
    assert verdicts[missing] == (_NETWORK_PATH, _NETWORK_REMEDY), verdicts
    assert verdicts["device"] == (_DEVICE_PATH, _WINDOWS_PYTHON_REMEDY), verdicts
    link_reason = f"the link C:\\data\\uv\\python points to \\\\server\\share\\python: {_NETWORK_PATH}"
    assert verdicts["link"] == (link_reason, _WINDOWS_PYTHON_REMEDY), verdicts


#: The runner's download of uv's installer, the one line a test of the installation replaces.
_UV_INSTALLER_DOWNLOAD = (
    "Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing -OutFile $installer"
)


def _ps_literal(value: object) -> str:
    """``value`` as a single-quoted PowerShell literal: a ``'`` inside is doubled, the one escape such a
    literal has (a profile or temp path may hold one, as in O'Brien).

    Example:
        >>> _ps_literal("C:/temp/it's")
        "'C:/temp/it''s'"
    """
    return "'" + str(value).replace("'", "''") + "'"


def _with_local_uv_installer(runner: Path, installer: Path) -> None:
    """``runner`` with the installer download replaced by a copy of ``installer``; one counted
    substitution, so the installer step that runs it and the PATH handling after it are the shipped code."""
    text = runner.read_text(encoding="utf-8")
    assert text.count(_UV_INSTALLER_DOWNLOAD) == 1
    runner.write_text(
        text.replace(
            _UV_INSTALLER_DOWNLOAD, f"Copy-Item -LiteralPath {_ps_literal(installer)} -Destination $installer"
        ),
        encoding="utf-8",
    )


@pytest.mark.local_only
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="the Windows branch of the uv installation")
def test_an_elevated_windows_run_keeps_the_installed_uvs_directory_on_path(tmp_path: Path) -> None:
    """[107]: after the runner installs uv, an elevated Windows run keeps ``.local\\bin`` on the PATH
    it rebuilds for uv and hands the script, and never names it as left off uv's PATH.

    Both are built from the caller's PATH, so a directory joined only to the runner's own PATH is lost
    to them. A local installer stands in for astral.sh's: it copies this host's uv into the
    ``.local\\bin`` of a profile only this account, SYSTEM and Administrators can change, so the
    installed uv passes the elevated trust rule; nothing finds a uv before it runs.

    The installer also records its host's own argv: the runner must start it with
    ``-ExecutionPolicy Bypass`` ahead of ``-File``. This module's process-scoped Bypass
    (``_process_scoped_execution_policy``) reaches the installer's host too, so the installer would
    run without the flag here, but not on a host whose policy refuses a downloaded script.
    """
    profile = _private_dir(tmp_path / "profile")
    installed = profile / ".local" / "bin"
    installer = tmp_path / "local-uv-install.ps1"
    host_argv = tmp_path / "installer-host-argv.json"
    record = (
        f"[IO.File]::WriteAllText({_ps_literal(host_argv)},"
        " (ConvertTo-Json -Compress -InputObject @([Environment]::GetCommandLineArgs())))"
    )
    make_dir = f"New-Item -ItemType Directory -Force -Path {_ps_literal(installed)} | Out-Null"
    source, target = _ps_literal(_UvLocation.directory / "uv.exe"), _ps_literal(installed / "uv.exe")
    copy_uv = f"Copy-Item -LiteralPath {source} -Destination {target}"
    # With a BOM, so Windows PowerShell 5.1 and pwsh 7 both read a non-ASCII path in it as UTF-8.
    installer.write_text(f"{record}\n{make_dir}\n{copy_uv}\n", encoding="utf-8-sig")
    runner, _ = _path_runner(tmp_path, elevated="$true")
    _with_local_uv_installer(runner, installer)
    nowhere = tmp_path / "no-uv-here"
    nowhere.mkdir()
    system = Path(os.environ["SYSTEMROOT"]) / "System32"
    result = _run_with_path(
        tmp_path,
        runner,
        path_first=[system],
        inherit_path=False,
        install_uv=True,
        env={"USERPROFILE": str(profile), "XDG_BIN_HOME": str(nowhere)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "uv was not found; installing it" in result.stdout, result.stdout + result.stderr
    assert (installed / "uv.exe").is_file(), result.stdout + result.stderr
    assert str(installed) in _reported_path(result).split(os.pathsep), result.stdout
    assert f"pwshpy: leaving {installed} off uv's PATH" not in result.stderr, result.stderr
    argv = [str(item) for item in cast("list[object]", json.loads(host_argv.read_text(encoding="utf-8-sig")))]
    assert "-File" in argv, argv
    policy_at = [index for index, item in enumerate(argv) if item == "-ExecutionPolicy"]
    assert policy_at, argv
    assert policy_at[0] < argv.index("-File"), argv
    assert argv[policy_at[0] + 1] == "Bypass", argv


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.skipif(
    RUNNING_AS_ROOT, reason="as root the real id answers 0, so a planted id answering 0 changes nothing to observe"
)
def test_the_posix_root_probe_never_asks_a_planted_id(tmp_path: Path) -> None:
    """The .ps1's euid comes from an id at a fixed path: a planted id answering 1000 would switch the rule off."""
    marker = tmp_path / "ran.txt"
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    _marker_double(fake, "id", marker, "echo 0")
    entry = tmp_path / "which_uv.py"
    entry.write_text(_ENTRY_REPORTING_UV)
    manifest = pack_script(entry)
    result = _run_with_path(
        tmp_path, Path(manifest.output_path), path_first=[fake, Path(_UV or "uv").parent], real_probe=True
    )
    assert not marker.exists(), "the runner asked the planted id"
    assert result.returncode == 0, result.stdout + result.stderr


def _own_posix_tree(tmp_path: Path, *, ancestor_mode: int) -> Path:
    ancestor = tmp_path / "anc"
    ancestor.mkdir()
    leaf = ancestor / "bin"
    leaf.mkdir()
    uv = _uv_stand_in(leaf)
    leaf.chmod(0o755)
    ancestor.chmod(ancestor_mode)
    return uv


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize(
    ("ancestor_mode", "refused"),
    [
        pytest.param(0o777, "is writable by every account and not sticky", id="other-write"),
        pytest.param(0o775, "is writable by group", id="group-write"),
        pytest.param(0o1777, "", id="sticky"),
        pytest.param(0o755, "", id="locked"),
    ],
)
def test_an_elevated_posix_run_judges_every_ancestor(tmp_path: Path, ancestor_mode: int, refused: str) -> None:
    """A locked leaf under a writable, non-sticky ancestor is refused; locked or sticky ancestors pass."""
    uv = _own_posix_tree(tmp_path, ancestor_mode=ancestor_mode)
    runner = _trust_runner(tmp_path, elevated="$true", trust_my_uid=True)
    result = _run_with_path(tmp_path, runner, path_first=[uv.parent])
    if refused:
        assert f"its ancestor {uv.parent.parent} {refused}" in _skip_line(result, uv), result.stderr
        assert not _same(_reported_uv(result), uv), result.stdout + result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert _same(_reported_uv(result), uv), result.stdout + result.stderr
        assert not _skip_line(result, uv), result.stderr


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.skipif(
    not (Path("/bin").is_symlink() and Path("/usr/bin/python3").exists()),
    reason="needs a merged-/usr host (/bin -> usr/bin) with /usr/bin/python3",
)
def test_an_elevated_posix_pin_through_merged_usr_is_accepted(tmp_path: Path) -> None:
    """/bin/python3 resolves through /bin -> usr/bin and python3's own link and is accepted, then run.

    This test's own managed-Python directory is not root's, so its check is taken as passed.
    """
    runner = _trust_runner(tmp_path, elevated="$true")
    runner.write_text(without_python_dir_check(runner.read_text(encoding="utf-8"), powershell=True), encoding="utf-8")
    result = _run_with_path(tmp_path, runner, "-PwshPyUv", "/bin/python3", path_first=[])
    assert _PIN_REFUSED not in result.stderr, result.stderr
    assert result.returncode == 2, result.stdout + result.stderr
    assert "can't open file" in result.stderr, result.stderr


# --- round 3: the POSIX euid probe fails closed; PATH entries are literal --------------------------

#: The .ps1's fixed id locations (one counted substitution simulates a system with id at none).
_FIXED_IDS = (
    "@('/usr/bin/id', '/bin/id', '/usr/sbin/id', '/sbin/id', "
    "'/run/current-system/sw/bin/id', '/run/current-system/profile/bin/id')"
)


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize("probe", ["planted-id-on-path", "id-answers-garbage"])
def test_an_unanswered_posix_euid_probe_applies_the_root_rule(tmp_path: Path, probe: str) -> None:
    """With no fixed-path id answering a decimal uid the .ps1 treats the run as root, never asks PATH.

    A planted id answering 1000 would switch the rule off; treated as root, the planted uv first on
    PATH is skipped and the planted id never runs.
    """
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin"))
    marker = tmp_path / "ran.txt"
    _marker_double(planted.parent, "id", marker, "echo 1000")
    fixed = tmp_path / "fixed"
    fixed.mkdir()
    if probe == "id-answers-garbage":
        _marker_double(fixed, "id0", tmp_path / "garbage.txt", "echo not-a-uid")
    entry = tmp_path / "which_uv.py"
    entry.write_text(_ENTRY_REPORTING_UV)
    runner = Path(pack_script(entry).output_path)
    text = runner.read_text(encoding="utf-8")
    assert text.count(_FIXED_IDS) == 1
    replacement = "@(" + ", ".join(_ps_literal(fixed / f"id{n}") for n in range(6)) + ")"
    # Treated as root, the run would first refuse its cache below this test's own directory ([98]).
    runner.write_text(_trusting_the_cache(text.replace(_FIXED_IDS, replacement)), encoding="utf-8")
    result = _run_with_path(tmp_path, runner, path_first=[planted.parent], real_probe=True)
    assert not marker.exists(), "the runner asked an id on PATH"
    assert not _same(_reported_uv(result), planted), result.stdout + result.stderr
    assert "is writable by every account" in _skip_line(result, planted), result.stderr


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
def test_an_elevated_posix_run_takes_path_entries_literally(tmp_path: Path) -> None:
    """A PATH entry holding '[', '*' or '?' names that directory, not a wildcard pattern."""
    base = tmp_path / "br[a]ck*ts?"
    base.mkdir()
    base.chmod(0o755)  # not the umask's mode: a group-writable ancestor is refused, as it should be
    uv = _own_posix_tree(base, ancestor_mode=0o755)
    # A pattern-looking entry that names no directory, beside one the pattern would match.
    decoy = tmp_path / "decoyX"
    decoy.mkdir()
    _uv_stand_in(decoy)
    pattern = tmp_path / "decoy?"
    runner = _trust_runner(tmp_path, elevated="$true", trust_my_uid=True)
    result = _run_with_path(tmp_path, runner, path_first=[pattern, uv.parent])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _same(_reported_uv(result), uv), result.stdout + result.stderr
    # decoy? does not exist, so it is no uv candidate at all - not one read as matching decoyX. uv's
    # PATH filter ([107]) may name it, as the literal directory it cannot inspect, never decoyX.
    assert str(pattern / "uv") not in result.stderr, result.stderr
    assert str(decoy) not in result.stderr, result.stderr


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
def test_an_elevated_posix_listing_is_never_globbed(tmp_path: Path) -> None:
    """pwsh on POSIX globs an unquoted argument to a native command, even a variable's value.

    An ancestor literally named ``d?r`` (world-writable) beside a locked ``d0r`` that the pattern
    also matches and that sorts first: listing the pattern would judge ``d0r`` and accept the uv.
    """
    sibling = tmp_path / "d0r"
    sibling.mkdir()
    sibling.chmod(0o755)
    literal = tmp_path / "d?r"
    literal.mkdir()
    leaf = literal / "bin"
    leaf.mkdir()
    uv = _uv_stand_in(leaf)
    leaf.chmod(0o755)
    literal.chmod(0o777)
    runner = _trust_runner(tmp_path, elevated="$true", trust_my_uid=True)
    result = _run_with_path(tmp_path, runner, path_first=[leaf])
    assert f"its ancestor {literal} is writable by every account and not sticky" in _skip_line(result, uv), (
        result.stderr
    )
    assert not _same(_reported_uv(result), uv), result.stdout + result.stderr


#: The PATH an elevated POSIX run gives its own helpers (user decision): system directories only.
_ROOT_HELPER_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/run/current-system/sw/bin:/run/current-system/profile/bin"


def test_the_posix_root_helper_path_is_system_dirs_only(tmp_path: Path) -> None:
    """The .ps1's fixed helper PATH is exactly the decided list, the same as the .sh runner's."""
    text = Path(pack_script(_project(tmp_path)).output_path).read_text(encoding="utf-8")
    assert text.count(f"$PwshPyFixedPath = '{_ROOT_HELPER_PATH}'") == 1
    assert "/usr/local/" not in text.split("$PwshPyFixedPath = ", 1)[1].splitlines()[0]


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
def test_an_elevated_posix_run_never_uses_ls_from_usr_local_bin(tmp_path: Path) -> None:
    """An ls in /usr/local/bin is not root's helper. A temp dir first on PATH stands in for it."""
    usr_local_bin = tmp_path / "usr-local-bin"
    usr_local_bin.mkdir()
    marker = tmp_path / "ran.txt"
    _marker_double(usr_local_bin, "ls", marker, "echo 'drwxr-xr-x 2 0 0 4096 Jan  1 00:00 x'")
    planted = _uv_stand_in(_user_writable_dir(tmp_path / "planted-bin"))
    runner = _trust_runner(tmp_path, elevated="$true")
    result = _run_with_path(tmp_path, runner, path_first=[usr_local_bin, planted.parent])
    assert not marker.exists(), "the ls from the /usr/local/bin stand-in ran"
    assert "is writable by every account" in _skip_line(result, planted), result.stderr


# --- [90] native-call arguments reach the program literally on POSIX pwsh ---------------------------
# pwsh on Linux and macOS globs and tilde-expands every argument to a native program that is not a
# quoted literal in the source - a variable, a splat and a subexpression alike - so '*.log' became the
# matching file names and a cache path holding '?' could name a sibling directory.

#: Files the globs in these tests would match, put in the runner's working directory.
_GLOB_BAIT = ("a.log", "b.log", "pkga")


def _recording_runner(
    tmp_path: Path, *, elevated: str, with_packages: Sequence[str] = (), real_data_home: bool = False
) -> tuple[Path, Path, Path]:
    """A runner whose uv is a recording double pinned with -PwshPyUv (POSIX), in a tree counted as root's.

    Returns the runner, the double, and the file it records into. ``real_data_home`` as for
    :func:`_elevation_stand_in`.
    """
    entry = tmp_path / "app.py"
    entry.write_text("print('never run: uv is a recording double')\n")
    runner = _elevation_stand_in(
        Path(pack_script(entry, options=PackOptions(with_packages=list(with_packages))).output_path),
        tmp_path / "recording_stand_in.ps1",
        elevated=elevated,
        trust_my_uid=True,
        real_data_home=real_data_home,
    )
    leaf = tmp_path / "rec" / "bin"
    leaf.mkdir(parents=True)
    record = tmp_path / "argv.txt"
    uv = write_recording_uv(leaf, record)
    leaf.chmod(0o755)
    (tmp_path / "rec").chmod(0o755)
    return runner, uv, record


def _glob_bait(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in _GLOB_BAIT:
        (directory / name).write_text("bait\n", encoding="utf-8")
    return directory


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.skipif(RUNNING_AS_ROOT, reason="the runner skips elevation when already root")
def test_the_sudo_relaunch_forwards_wildcards_and_tildes_literally(tmp_path: Path) -> None:
    """[90]: '*', '*.log' and '~' reach the packed script as typed through the -PwshPyElevate relaunch."""
    manifest = pack_script(_project(tmp_path))
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    write_sudo_double(bin_dir, grant=True)
    arguments = ["*", "*.log", "~", "~/x", "[ab].log"]
    result = _run_with_path(
        tmp_path,
        Path(manifest.output_path),
        "-PwshPyElevate",
        *arguments,
        path_first=[bin_dir, _UvLocation.directory],
        cwd=_glob_bait(tmp_path / "work"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"argv:{arguments!r}" in result.stdout, result.stdout


@pytest.mark.os_posix
@_POSIX_ONLY
def test_a_cache_path_with_wildcards_runs_the_verified_entry(tmp_path: Path) -> None:
    """[90]: the verified entry under a cache root named 'c?che' runs, not a sibling the pattern matches.

    The decoys are full copies of the extracted tree whose shim prints 'decoy'; '!' and '#' sort
    before '?' under both ordinal and culture-aware ordering, so a globbed target would pick one.
    """
    entry = _project(tmp_path, exit_code=0)
    runner = Path(pack_script(entry).output_path)
    cache = tmp_path / "c?che"
    environment = {"PWSHPY_PACK_CACHE": str(cache)}
    assert _run(runner, env=environment).returncode == 0
    (extracted,) = [path for path in cache.iterdir() if path.is_dir()]
    for decoy_root in (tmp_path / "c!che", tmp_path / "c#che"):
        decoy = decoy_root / extracted.name
        shutil.copytree(extracted, decoy)
        (decoy / "__pwshpy_pack_main__.py").write_text("print('decoy')\n", encoding="utf-8")
    result = _run(runner, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "greeting:hello from the submodule" in result.stdout, result.stdout
    assert "decoy" not in result.stdout, result.stdout


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
def test_the_uv_arguments_and_target_reach_uv_literally(tmp_path: Path) -> None:
    """[90]: a --with spec with brackets and a target under a '?' cache reach uv unglobbed."""
    runner, uv, record = _recording_runner(tmp_path, elevated="$false", with_packages=["pkg[ab]"])
    cache = tmp_path / "c?che"
    # A decoy target the pattern also matches, sorting before the literal one.
    sha = re.search(r"\$PwshPySha = '([0-9a-f]{64})'", runner.read_text(encoding="utf-8"))
    assert sha is not None
    decoy = tmp_path / "c!che" / sha.group(1)[:16]
    decoy.mkdir(parents=True)
    (decoy / "__pwshpy_pack_main__.py").write_text("print('decoy')\n", encoding="utf-8")
    result = _run_with_path(
        tmp_path,
        runner,
        "-PwshPyUv",
        str(uv),
        path_first=[],
        env={"PWSHPY_PACK_CACHE": str(cache)},
        cwd=_glob_bait(tmp_path / "work"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    argv = record.read_text(encoding="utf-8").splitlines()
    assert argv[2:4] == ["--with", "pkg[ab]"], argv
    assert argv[4].startswith(str(cache) + os.sep), argv
    assert argv[4].endswith("__pwshpy_pack_main__.py"), argv


# --- [89] an elevated run only runs a uv-managed Python --------------------------------------------
# uv picks the interpreter the packed script runs on, and uv EXECUTES every candidate it considers (it
# asks each for its version), so a python planted in a PATH directory another account can write would
# run with the elevated run's rights. Elevated, the runner passes --python-preference only-managed and
# moves uv's project discovery (.python-version, uv.toml) to a directory only administrators can write,
# and drops the variables that name an interpreter outright. Every run here has downloads off and a
# fresh uv cache, so nothing reaches the network and no remembered interpreter hides a planted one.

#: The packed script under test: prints the interpreter it runs on.
_ENTRY_REPORTING_PYTHON = """import sys

print("python:" + sys.executable)
"""
#: uv's refusal when `uv python find --system --python-preference only-managed` finds nothing, which an
#: elevated run passes on (measured with uv 0.13.0).
_NO_MANAGED_PYTHON = "No interpreter found in managed installations"
#: The line the runner prints for each interpreter variable an elevated run drops.
_VARIABLE_DROPPED = "pwshpy: ignoring {name}"


def _reported_python(result: subprocess.CompletedProcess[str]) -> Path | None:
    for line in result.stdout.splitlines():
        if line.startswith("python:") and line[7:]:
            return Path(line[7:].strip())
    return None


def _python_runner(
    tmp_path: Path, *, elevated: str, source: str = _ENTRY_REPORTING_PYTHON, with_packages: Sequence[str] = ()
) -> tuple[Path, list[Path]]:
    """A which-python runner with its elevation probe forced, plus the PATH head holding a uv it trusts.

    POSIX: a stand-in uv in a tree this test owns, counted as root's. Windows: the module's uv,
    which an elevated host already trusts. ``source`` replaces the packed script.
    """
    entry = tmp_path / "which_python.py"
    entry.write_text(source)
    runner = _elevation_stand_in(
        Path(pack_script(entry, options=PackOptions(with_packages=list(with_packages))).output_path),
        tmp_path / "python_stand_in.ps1",
        elevated=elevated,
        trust_my_uid=True,
    )
    if sys.platform == "win32":
        return runner, [_UvLocation.directory]
    return runner, [_own_posix_tree(tmp_path, ancestor_mode=0o755).parent]


def _clean_cwd(tmp_path: Path) -> Path:
    """An empty working directory: uv queries a ``.venv`` in the cwd or above it (the suite's own,
    under the repository), which would answer before the PATH python these tests plant."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return work


def _needs_managed_python() -> Path:
    managed = trusted_managed_python()
    if managed is None:
        pytest.skip("this host has no uv-managed Python (and the test must not download one)")
    return managed


@pytest.mark.local_only
@pytest.mark.parametrize(
    ("elevated", "planted_runs"),
    [pytest.param("$true", False, id="elevated"), pytest.param("$false", True, id="not-elevated-control")],
)
def test_an_elevated_run_never_executes_a_python_planted_first_on_path(
    tmp_path: Path, elevated: str, planted_runs: bool
) -> None:
    """[89]: with no managed Python, an elevated run refuses rather than run the PATH python.

    The control proves the arrangement: unelevated, uv runs the planted python (POSIX) or the
    system one (Windows, where no test can write a python.exe double).
    """
    runner, head = _python_runner(tmp_path, elevated=elevated)
    marker = tmp_path / "planted-ran.txt"
    if sys.platform != "win32":
        head = [*head, write_planted_python(tmp_path / "planted-bin", marker).parent]
    probe = interpreter_probe_env(tmp_path, managed=None)
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=_clean_cwd(tmp_path))
    if planted_runs:
        assert result.returncode == 0, result.stdout + result.stderr
        reported = _reported_python(result)
        assert reported is not None, result.stdout
        assert Path(probe["UV_PYTHON_INSTALL_DIR"]) not in reported.parents, reported
        assert sys.platform == "win32" or marker.is_file(), "the control never ran the planted python"
        return
    assert not marker.exists(), "the elevated run executed the planted python: " + marker.read_text()
    assert _reported_python(result) is None, result.stdout
    assert _NO_MANAGED_PYTHON in result.stderr, result.stderr


@pytest.mark.local_only
def test_an_elevated_run_uses_an_installed_managed_python(tmp_path: Path) -> None:
    """[89]: the positive control - with a managed Python installed, the elevated run runs on it."""
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated="$true")
    marker = tmp_path / "planted-ran.txt"
    if sys.platform != "win32":
        head = [*head, write_planted_python(tmp_path / "planted-bin", marker).parent]
    result = _run_with_path(
        tmp_path,
        runner,
        path_first=head,
        env=interpreter_probe_env(tmp_path, managed=managed),
        cwd=_clean_cwd(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reported = _reported_python(result)
    assert reported is not None and managed.resolve() in reported.resolve().parents, result.stdout
    assert not marker.exists(), "the planted python ran"


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize(
    ("elevated", "planted_runs"),
    [pytest.param("$true", False, id="elevated"), pytest.param("$false", True, id="not-elevated-control")],
)
def test_an_elevated_run_ignores_a_python_version_file_above_the_cache(
    tmp_path: Path, elevated: str, planted_runs: bool
) -> None:
    """[89]: uv reads .python-version from the script's directory up, and a path there beats only-managed.

    The cache is the script's directory, so whoever can write a directory above it would otherwise
    choose the interpreter.
    """
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated=elevated)
    marker = tmp_path / "planted-ran.txt"
    planted = write_planted_python(tmp_path / "planted-bin", marker)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / ".python-version").write_text(f"{planted}\n", encoding="utf-8")
    result = _run_with_path(
        tmp_path,
        runner,
        path_first=head,
        env=interpreter_probe_env(tmp_path, managed=managed),
        cwd=_clean_cwd(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file() is planted_runs, result.stdout + result.stderr


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize("variable", ["UV_PYTHON", "VIRTUAL_ENV", "CONDA_PREFIX"])
@pytest.mark.parametrize(
    ("elevated", "planted_runs"),
    [pytest.param("$true", False, id="elevated"), pytest.param("$false", True, id="not-elevated-control")],
)
def test_an_elevated_run_drops_a_variable_that_names_an_interpreter(
    tmp_path: Path, variable: str, elevated: str, planted_runs: bool
) -> None:
    """[89]: UV_PYTHON naming a path, or an active virtual or conda environment, each beats only-managed.

    Elevated, the runner drops the variable (and says so); unelevated, it reaches uv untouched.
    """
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated=elevated)
    marker = tmp_path / "planted-ran.txt"
    if variable == "UV_PYTHON":
        value = str(write_planted_python(tmp_path / "planted-bin", marker))
    else:
        value = str(write_planted_environment(tmp_path / "planted-env", marker))
    probe = {**interpreter_probe_env(tmp_path, managed=managed), variable: value}
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=_clean_cwd(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file() is planted_runs, result.stdout + result.stderr
    assert (_VARIABLE_DROPPED.format(name=variable) in result.stderr) is not planted_runs, result.stderr


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated"])
def test_the_uv_command_line_carries_only_managed_exactly_when_elevated(tmp_path: Path, elevated: str) -> None:
    """[89]: the argv uv receives - only-managed and the root-owned project anchor when elevated, else neither."""
    runner, uv, record = _recording_runner(tmp_path, elevated=elevated)
    result = _run_with_path(tmp_path, runner, "-PwshPyUv", str(uv), path_first=[], cwd=_clean_cwd(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    argv = record.read_text(encoding="utf-8").splitlines()
    python = str(uv.parent / "python")
    managed = [
        "--no-config",
        "--python-preference",
        "only-managed",
        "--no-env-file",
        "--python",
        python,
        "--project",
        "/",
        "--directory",
        "/",
    ]
    if elevated == "$true":
        assert argv[:12] == ["run", "--no-project", *managed], argv
        found = [line for line in Path(f"{record}.python").read_text(encoding="utf-8").splitlines() if ":" not in line]
        assert found == [
            "python",
            "find",
            "--system",
            "--python-preference",
            "only-managed",
            "--no-config",
            "--directory",
            "/",
        ], found
    else:
        assert argv[:2] == ["run", "--no-project"], argv
        assert "--python-preference" not in argv and "--project" not in argv, argv
        assert "--directory" not in argv, argv


def _recorded_variables(record: Path) -> dict[str, str]:
    """The ``env:NAME=value`` lines a recording uv double wrote, as a mapping."""
    lines = record.read_text(encoding="utf-8").splitlines()
    return dict(line[4:].split("=", 1) for line in lines if line.startswith("env:"))


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated-control"])
def test_an_elevated_run_drops_the_variables_that_steer_the_managed_python(tmp_path: Path, elevated: str) -> None:
    """[59]: another install dir, config file, mirror or download list never reaches an elevated uv.

    Elevated, each is dropped with a line naming it; the control shows each reaching uv unchanged.
    """
    runner, uv, record = _recording_runner(tmp_path, elevated=elevated)
    steering = {name: str(tmp_path / f"steer-{name.lower()}") for name in MANAGED_PYTHON_VARIABLES}
    result = _run_with_path(
        tmp_path, runner, "-PwshPyUv", str(uv), path_first=[], env=steering, cwd=_clean_cwd(tmp_path)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = _recorded_variables(record)
    for name, value in steering.items():
        dropped_line = _VARIABLE_DROPPED.format(name=name) in result.stderr
        if elevated == "$true":
            # The runner names its own install directory (the account's), never the caller's.
            assert recorded.get(name) != value and dropped_line, (name, recorded, result.stderr)
        else:
            assert recorded.get(name) == value and not dropped_line, (name, recorded, result.stderr)


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated-control"])
def test_an_elevated_posix_run_takes_the_managed_python_dir_from_the_account(tmp_path: Path, elevated: str) -> None:
    """[93]: elevated, uv's managed-Python directory is the passwd home's whatever HOME and XDG_DATA_HOME
    say, and PYTHONHOME / PYTHONPATH are dropped with a line each; the control names no directory and
    passes both on. [X22 C1]: the cache directory likewise, whatever XDG_CACHE_HOME says."""
    runner, uv, record = _recording_runner(tmp_path, elevated=elevated, real_data_home=True)
    library = {"PYTHONHOME": str(tmp_path / "planted-home"), "PYTHONPATH": str(tmp_path / "planted-path")}
    result = _run_with_path(
        tmp_path,
        runner,
        "-PwshPyUv",
        str(uv),
        path_first=[],
        env={
            **library,
            "XDG_DATA_HOME": str(tmp_path / "elsewhere"),
            "XDG_CACHE_HOME": str(tmp_path / "elsewhere"),
            "HOME": str(tmp_path / "elsewhere"),
        },
        cwd=_clean_cwd(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = _recorded_variables(record)
    if elevated == "$true":
        assert sys.platform != "win32"
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        assert recorded.get("UV_PYTHON_INSTALL_DIR") == str(home / ".local" / "share" / "uv" / "python"), recorded
        # [X22 C1] the cache as well (the recording uv writes nothing there).
        assert recorded.get("UV_CACHE_DIR") == str(home / ".cache" / "uv"), recorded
    else:
        assert "UV_PYTHON_INSTALL_DIR" not in recorded, recorded
    for name, value in library.items():
        dropped_line = _VARIABLE_DROPPED.format(name=name) in result.stderr
        if elevated == "$true":
            assert name not in recorded and dropped_line, (name, recorded, result.stderr)
        else:
            assert recorded.get(name) == value and not dropped_line, (name, recorded, result.stderr)


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated-control"])
def test_an_elevated_run_lets_uv_take_its_default_package_sources_cache_and_no_env_file(
    tmp_path: Path, elevated: str
) -> None:
    """[X21 P2/P3], user decision "Drop them too": elevated, every variable naming a package index, a
    find-links location, an insecure host, a cache directory or an env file, and PYTHONUSERBASE, is
    dropped with a line each, before uv python find already; the user site-packages is off and uv run
    gets --no-env-file. The control passes each on unchanged and adds nothing."""
    runner, uv, record = _recording_runner(tmp_path, elevated=elevated)
    given = {name: str(tmp_path / f"given-{name.lower()}") for name in (*UV_SOURCE_VARIABLES, "PYTHONUSERBASE")}
    result = _run_with_path(tmp_path, runner, "-PwshPyUv", str(uv), path_first=[], env=given, cwd=_clean_cwd(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    argv = record.read_text(encoding="utf-8").splitlines()
    recorded = _recorded_variables(record)
    for name, value in given.items():
        dropped_line = _VARIABLE_DROPPED.format(name=name) in result.stderr
        if elevated == "$true":
            # Dropped; UV_CACHE_DIR is then named again, from the account ([X22 C1]), never the caller's.
            assert recorded.get(name) != value and dropped_line, (name, recorded, result.stderr)
        else:
            assert recorded.get(name) == value and not dropped_line, (name, recorded, result.stderr)
    if elevated == "$true":
        assert recorded.get("PYTHONNOUSERSITE") == "1" and "--no-env-file" in argv, (recorded, argv)
        found = _recorded_variables(Path(f"{record}.python"))
        assert all(found.get(name) != value for name, value in given.items()), found  # before uv python find
    else:
        assert "PYTHONNOUSERSITE" not in recorded and "--no-env-file" not in argv, (recorded, argv)


#: Loads the runner's functions alone and prints what Get-PwshPyAccountDataHome and
#: Get-PwshPyAccountCacheHome answer, with APPDATA and LOCALAPPDATA pointed somewhere the test chose.
_DATA_HOME_PROBE = r"""
param([string] $Artefact, [string] $PlantedAppData)
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Artefact, [ref]$null, [ref]$null)
$language = 'System.Management.Automation.Language'
$functions = $ast.FindAll({ param($n) $n -is ("$language.FunctionDefinitionAst" -as [type]) }, $false)
foreach ($definition in $functions) { . ([ScriptBlock]::Create($definition.Extent.Text)) }
$PwshPyOnWindows = $true
$env:APPDATA = $PlantedAppData
$env:LOCALAPPDATA = $PlantedAppData
Write-Output ('known:' + [Environment]::GetFolderPath([Environment+SpecialFolder]::ApplicationData))
Write-Output ('answer:' + (Get-PwshPyAccountDataHome))
Write-Output ('knownlocal:' + [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData))
Write-Output ('cache:' + (Get-PwshPyAccountCacheHome))
"""


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_the_windows_data_home_is_the_known_folder_not_appdata(tmp_path: Path) -> None:
    """[93]: an elevated run's managed-Python directory comes from the RoamingAppData known folder, and
    [X22 C1] its uv cache from the LocalAppData known folder.

    APPDATA is pointed at a directory of the test's own; the answer must be the known folder, which
    the probe also prints, and the control is that the two differ.
    """
    assert _PWSH is not None
    manifest = pack_script(_project(tmp_path))
    probe = tmp_path / "data_home_probe.ps1"
    probe.write_text(_DATA_HOME_PROBE, encoding="utf-8")
    planted = tmp_path / "planted-appdata"
    result = subprocess.run(  # noqa: S603 - fixed argv from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(probe), str(manifest.output_path), str(planted)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    prefixes = ("known:", "answer:", "knownlocal:", "cache:")
    lines = dict(line.split(":", 1) for line in result.stdout.splitlines() if line.startswith(prefixes))
    assert lines["known"] and Path(lines["known"]) != planted, lines
    assert lines["answer"] == lines["known"], lines
    # [X22 C1] the cache: the LocalAppData known folder, not LOCALAPPDATA.
    assert lines["knownlocal"] and Path(lines["knownlocal"]) != planted, lines
    assert lines["cache"] == lines["knownlocal"], lines


#: A key uv refuses to parse: a uv.toml holding it fails every uv call that reads it (exit 2, naming it).
_PLANTED_UV_KEY = "pwshpy-planted-key"


@pytest.mark.local_only
@pytest.mark.parametrize("scope", ["system", "user"])
@pytest.mark.parametrize(
    ("elevated", "config_read"),
    [pytest.param("$true", False, id="elevated"), pytest.param("$false", True, id="not-elevated-control")],
)
def test_an_elevated_run_reads_no_uv_configuration_file(
    tmp_path: Path, scope: str, elevated: str, config_read: bool
) -> None:
    """[A2] (user decision: ignore all uv config): a uv.toml can name a cache directory, an index or a
    Python download mirror, so an elevated run passes --no-config to every uv call. The planted config
    holds a key uv refuses, so reading it fails the run with no side effect; the control shows uv reads
    it. System scope: XDG_CONFIG_DIRS/uv (POSIX), or on Windows %SYSTEMDRIVE%\\ProgramData\\uv, which is
    where uv 0.13 looks (measured: PROGRAMDATA and ALLUSERSPROFILE change nothing). User scope:
    XDG_CONFIG_HOME/uv (POSIX), or APPDATA\\uv (Windows, which the probe environment already points at
    the test's data home).
    """
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated=elevated)
    probe = interpreter_probe_env(tmp_path, managed=managed)
    config_base = tmp_path / f"config-{scope}"
    if sys.platform != "win32":
        probe["XDG_CONFIG_DIRS" if scope == "system" else "XDG_CONFIG_HOME"] = str(config_base)
        config_root = config_base
    elif scope == "system":
        probe["SYSTEMDRIVE"] = str(config_base)
        config_root = config_base / "ProgramData"
    else:
        config_root = data_home(tmp_path)
    (config_root / "uv").mkdir(parents=True, exist_ok=True)
    (config_root / "uv" / "uv.toml").write_text(f"{_PLANTED_UV_KEY} = 1\n", encoding="utf-8")
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=_clean_cwd(tmp_path))
    if config_read:
        assert result.returncode == 2 and _PLANTED_UV_KEY in result.stderr, result.stdout + result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert _PLANTED_UV_KEY not in result.stderr, result.stderr


# --- [57] an elevated run starts uv in a directory only administrators can write -------------------
# uv probes a .venv in its working directory and every directory above, and under only-managed it even
# RUNS the script on that venv's python (measured with uv 0.13.0), so an elevated run started from a
# directory another account can write would run that account's interpreter. Elevated, uv starts in
# the anchor (/ on POSIX, System32 on Windows) via --directory, and the generated shim moves the
# script back to the caller's directory before it runs.

#: The packed script under test: the interpreter and the working directory it runs with.
_ENTRY_REPORTING_CWD = """import os
import sys

print("python:" + sys.executable)
print("cwd:" + os.getcwd())
"""


def _reported_cwd(result: subprocess.CompletedProcess[str]) -> Path | None:
    for line in result.stdout.splitlines():
        if line.startswith("cwd:") and line[4:]:
            return Path(line[4:].strip())
    return None


def _planted_chain(tmp_path: Path, marker: Path) -> Path:
    """A working directory one level below a directory holding a planted ``.venv``."""
    project = tmp_path / "project"
    write_planted_venv(project / ".venv", marker)
    work = project / "work"
    work.mkdir()
    return work


@pytest.mark.local_only
@pytest.mark.parametrize(
    ("elevated", "planted_runs"),
    [pytest.param("$true", False, id="elevated"), pytest.param("$false", True, id="not-elevated-control")],
)
def test_an_elevated_run_never_executes_a_venv_above_the_working_directory(
    tmp_path: Path, elevated: str, planted_runs: bool
) -> None:
    """[57]: the planted .venv's python never starts elevated, and the script still sees the caller's cwd.

    The control shows the plant is reachable: not elevated, uv starts in the caller's directory and
    runs the planted interpreter.
    """
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated=elevated, source=_ENTRY_REPORTING_CWD)
    marker = tmp_path / "planted-venv-ran.txt"
    work = _planted_chain(tmp_path, marker)
    probe = interpreter_probe_env(tmp_path, managed=managed)
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=work)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file() is planted_runs, result.stdout + result.stderr
    reported = _reported_cwd(result)
    assert reported is not None and reported.samefile(work), result.stdout


#: The runner's anchor function, which a test answers with a directory of its own.
_ANCHOR_FUNCTION = "function Get-PwshPyAnchorDirectory {"


def _with_anchor(runner: Path, anchor: Path) -> None:
    """Make ``runner`` start uv in ``anchor``: no test can plant a .venv above System32 or /."""
    text = runner.read_text(encoding="utf-8")
    assert text.count(_ANCHOR_FUNCTION) == 1
    literal = "'" + str(anchor).replace("'", "''") + "'"
    stand_in = f"{_ANCHOR_FUNCTION}\n    return {literal}\n}}\n\nfunction Get-PwshPyAnchorDirectoryOnHost {{"
    runner.write_text(text.replace(_ANCHOR_FUNCTION, stand_in), encoding="utf-8")


@pytest.mark.local_only
@pytest.mark.parametrize(
    ("elevated", "planted_runs"),
    [pytest.param("$true", False, id="elevated"), pytest.param("$false", True, id="not-elevated-control")],
)
def test_an_elevated_run_never_executes_a_venv_above_the_anchor(
    tmp_path: Path, elevated: str, planted_runs: bool
) -> None:
    """[A1]: uv probes a .venv in its working directory and EVERY directory above it, so a .venv above
    the anchor (C:\\.venv above System32, creatable by any authenticated user) would run elevated; an
    elevated run names the interpreter outright. The control, unelevated and started in the anchor
    itself, shows the plant is reachable.
    """
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated=elevated, source=_ENTRY_REPORTING_CWD)
    marker = tmp_path / "planted-venv-ran.txt"
    write_planted_venv(tmp_path / "anchor-parent" / ".venv", marker)
    anchor = tmp_path / "anchor-parent" / "anchor"
    anchor.mkdir()
    _with_anchor(runner, anchor)
    probe = interpreter_probe_env(tmp_path, managed=managed)
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=anchor)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file() is planted_runs, result.stdout + result.stderr
    reported = _reported_cwd(result)
    assert reported is not None and reported.samefile(anchor), result.stdout


#: The start of an elevated run's uv options; the control below removes --no-config from it.
_ELEVATED_RUN_OPTIONS = "return @('--no-config', '--python-preference'"


@pytest.mark.local_only
@pytest.mark.parametrize("no_config", [True, False], ids=["elevated", "elevated-without-no-config-control"])
def test_a_uv_toml_directory_above_the_anchor_changes_nothing(tmp_path: Path, no_config: bool) -> None:
    """[A2]: uv run --project reads a uv.toml from the project directory and every one above it unless
    --no-config, so a DIRECTORY named uv.toml there (any authenticated user can create C:\\uv.toml above
    System32) failed every elevated run; now it changes nothing. The control, the same elevated run with
    --no-config taken off its uv run, fails on it: the plant is reachable."""
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated="$true")
    (tmp_path / "anchor-parent" / "uv.toml").mkdir(parents=True)
    anchor = tmp_path / "anchor-parent" / "anchor"
    anchor.mkdir()
    _with_anchor(runner, anchor)
    if not no_config:
        text = runner.read_text(encoding="utf-8")
        assert text.count(_ELEVATED_RUN_OPTIONS) == 1
        runner.write_text(text.replace(_ELEVATED_RUN_OPTIONS, "return @('--python-preference'"), encoding="utf-8")
    probe = interpreter_probe_env(tmp_path, managed=managed)
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=anchor)
    if not no_config:
        assert result.returncode == 2 and "uv.toml" in result.stderr, result.stdout + result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert _reported_python(result) is not None, result.stdout


#: The refusal of uv's managed-Python directory, and the remedy every refused managed-Python path names.
_PYTHON_DIR_REFUSED = "refusing uv's managed-Python directory"
_PYTHON_REMEDY = (
    "take every other account's write access off" if sys.platform == "win32" else "chown root, then chmod go-w"
)


def _open_to_other_accounts(directory: Path) -> None:
    """Let every account modify ``directory`` itself, nothing below it (no inheritance on Windows).

    The planted install's files are hard links to shared ones; an inherited grant would reach them.
    """
    if sys.platform == "win32":
        _icacls(directory, "/grant", f"{_SID_USERS}:(M)")
    else:
        directory.chmod(0o777)


def _planted_python_probe(tmp_path: Path, marker: Path) -> tuple[dict[str, str], Path]:
    """An elevated run's environment whose managed-Python directory holds a planted install."""
    planted = planted_managed_python(tmp_path / "planted-managed", marker)
    if planted is None:
        pytest.skip("this host has no uv-managed Python (and the test must not download one)")
    return interpreter_probe_env(tmp_path, managed=planted), planted


@pytest.mark.local_only
@pytest.mark.parametrize("refused", [True, False], ids=["untrusted-directory", "trusted-directory-control"])
def test_an_untrusted_managed_python_dir_is_refused_before_uv_runs_a_python_in_it(
    tmp_path: Path, refused: bool
) -> None:
    """[X21 P1]: `uv python find` EXECUTES every interpreter it considers in uv's managed-Python
    directory, so a .pth planted there ran elevated before the found path was judged. The directory
    is now judged before uv starts: when Users (Windows) or every account (POSIX) may modify it, the
    run exits 127 and the plant never runs. The control, the same elevated run with the directory
    left trusted, runs the plant (the installs below it:
    test_an_elevated_run_refuses_a_python_whose_install_another_account_can_modify)."""
    runner, head = _python_runner(tmp_path, elevated="$true")
    marker = tmp_path / "planted-pth-ran.txt"
    probe, planted = _planted_python_probe(tmp_path, marker)
    if refused:
        _open_to_other_accounts(planted)
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=_clean_cwd(tmp_path))
    if refused:
        assert not marker.exists(), "uv ran a Python from the untrusted directory elevated: " + marker.read_text()
        assert result.returncode == 127, result.stdout + result.stderr
        assert _PYTHON_DIR_REFUSED in result.stderr and _PYTHON_REMEDY in result.stderr, result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file(), "the control never ran the planted .pth: " + result.stderr
    assert _reported_python(result) is not None, result.stdout


def _open_below(planted: Path, where: str) -> Path:
    """Let every account modify one entry below the planted managed-Python directory; its path.

    The install's files are hard links to shared ones (on Windows to the host's own managed Python),
    and a hard link shares its file's ACL and mode, so a file is replaced by a copy of its own first.
    """
    (install,) = [child for child in planted.iterdir() if child.is_dir()]
    (site_packages,) = [*install.glob("lib/python3*/site-packages"), *install.glob("Lib/site-packages")]
    target = {
        "install-dir": install,
        "site-packages-dir": site_packages,
        "planted-file": site_packages / "pwshpy_planted.pth",
    }[where]
    if target.is_file():
        text = target.read_bytes()
        target.unlink()
        target.write_bytes(text)
    if sys.platform == "win32":
        _icacls(target, "/grant", f"{_SID_USERS}:(M)")
    else:
        target.chmod(0o777 if target.is_dir() else 0o666)
    return target


@pytest.mark.local_only
@pytest.mark.parametrize(
    "where",
    ["install-dir", "site-packages-dir", "planted-file", "trusted-control", "uvs-lock-file-control"],
    ids=lambda where: where,
)
def test_an_elevated_run_refuses_a_python_whose_install_another_account_can_modify(tmp_path: Path, where: str) -> None:
    """[A1], [X22 W1]: an install inside a trusted managed-Python directory that Users (Windows) or every
    account (POSIX) may modify (the install, its site-packages, one file in it) is refused before uv runs
    any Python (exit 127, naming the entry and the remedy): `uv python find` runs every install it
    considers, so a .pth planted there ran elevated before the post-find check (measured on 1.6.0rc).
    The planted marker stays absent. The control, the same tree left trusted, runs the plant and the
    script; so does a tree holding a world-writable lock file where uv keeps its own (POSIX: uv creates
    it mode 0666 and only ever locks it)."""
    runner, head = _python_runner(tmp_path, elevated="$true")
    marker = tmp_path / "planted-pth-ran.txt"
    probe, planted = _planted_python_probe(tmp_path, marker)
    if where == "uvs-lock-file-control":
        (planted / ".lock").touch()
        if sys.platform != "win32":
            (planted / ".lock").chmod(0o666)
    target = None if where.endswith("-control") else _open_below(planted, where)
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=_clean_cwd(tmp_path))
    if target is None:
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.is_file(), "the control never ran the planted .pth: " + result.stderr
        assert _reported_python(result) is not None, result.stdout
        return
    assert not marker.exists(), "uv ran a Python from the modifiable install elevated: " + marker.read_text()
    assert result.returncode == 127, result.stdout + result.stderr
    assert _PYTHON_DIR_REFUSED in result.stderr and _PYTHON_REMEDY in result.stderr, result.stderr
    assert target.name in result.stderr, result.stderr
    assert _reported_python(result) is None, result.stdout


@pytest.mark.local_only
def test_an_elevated_run_still_judges_the_found_python_after_the_directory_check(tmp_path: Path) -> None:
    """[A1]: the Python uv found passes the same check as uv itself, a second line of defence behind
    the directory check (which this test takes as passed, its subject being the later one): an install
    Users (Windows) or every account (POSIX) may modify refuses it (exit 127, the script never runs)
    and names the remedy."""
    runner, head = _python_runner(tmp_path, elevated="$true")
    runner.write_text(without_python_dir_check(runner.read_text(encoding="utf-8"), powershell=True), encoding="utf-8")
    probe, planted = _planted_python_probe(tmp_path, tmp_path / "planted-pth-ran.txt")
    _open_below(planted, "install-dir")
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=_clean_cwd(tmp_path))
    assert result.returncode == 127, result.stdout + result.stderr
    assert "refusing the Python uv found at" in result.stderr and _PYTHON_REMEDY in result.stderr, result.stderr
    assert _PYTHON_DIR_REFUSED not in result.stderr, result.stderr
    assert _reported_python(result) is None, result.stdout


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
def test_an_elevated_posix_run_refuses_an_acl_below_the_managed_python_dir(tmp_path: Path) -> None:
    """[X22 W1]: an ACL on an entry below the managed-Python directory refuses it before uv runs any
    Python, as it refuses a uv; a read-only one too, so the '+' ls marks is what is read (a write ACL
    would show as the mask's group write)."""
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        pytest.skip("setfacl is missing")
    runner, head = _python_runner(tmp_path, elevated="$true")
    marker = tmp_path / "planted-pth-ran.txt"
    probe, planted = _planted_python_probe(tmp_path, marker)
    (site_packages,) = planted.glob("*/lib/python3*/site-packages")
    done = subprocess.run([setfacl, "-m", "u:65534:r-x", str(site_packages)], capture_output=True, check=False)  # noqa: S603
    if done.returncode:
        pytest.skip("this filesystem has no POSIX ACLs")
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=_clean_cwd(tmp_path))
    assert not marker.exists(), "uv ran a Python below an ACL elevated: " + marker.read_text()
    assert result.returncode == 127, result.stdout + result.stderr
    assert f"{site_packages.relative_to(planted).as_posix()} carries an ACL" in result.stderr, result.stderr


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.skipif(not RUNNING_AS_ROOT, reason="only root can give a symlink to another account (lchown)")
@pytest.mark.parametrize("foreign", [True, False], ids=["foreign-symlink", "roots-symlink-control"])
def test_an_elevated_posix_run_judges_a_symlink_below_the_managed_python_dir_by_its_owner(
    tmp_path: Path, foreign: bool
) -> None:
    """[X23 R1]: a symlink below the managed-Python directory owned by another account is refused
    before uv runs any Python (exit 127, naming the link and its owner): that account can repoint it.
    The control, the same link left root's, runs the plant and the script."""
    runner, head = _python_runner(tmp_path, elevated="$true")
    marker = tmp_path / "planted-pth-ran.txt"
    probe, planted = _planted_python_probe(tmp_path, marker)
    (site_packages,) = planted.glob("*/lib/python3*/site-packages")
    link = site_packages / "pwshpy_link"
    link.symlink_to("pwshpy_planted.pth")
    if foreign:
        owned_by_another_account(link)
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=_clean_cwd(tmp_path))
    if not foreign:
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.is_file(), "the control never ran the planted .pth: " + result.stderr
        return
    assert not marker.exists(), "uv ran a Python below a foreign symlink elevated: " + marker.read_text()
    assert result.returncode == 127, result.stdout + result.stderr
    named = link.relative_to(planted).as_posix()
    assert f"{named} is owned by uid {OTHER_UID}, not root" in result.stderr, result.stderr


@pytest.mark.local_only
@pytest.mark.parametrize("kind", LIBRARY_PATH_PLANTS)
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated-control"])
def test_an_elevated_run_lets_no_library_path_from_the_callers_environment_reach_the_python(
    tmp_path: Path, kind: str, elevated: str
) -> None:
    """[X21 P2]: uv run loads the env file UV_ENV_FILE names despite --no-config (one holding
    PYTHONPATH put the caller's sitecustomize ahead of the standard library), and a script without a
    PEP 723 block runs on the interpreter itself with the user site-packages on (from PYTHONUSERBASE,
    or below APPDATA on Windows and HOME on POSIX, which some sudo setups keep). Elevated, none of them
    reaches the Python; the control runs each one."""
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated=elevated)
    marker = tmp_path / "planted-library-ran.txt"
    home = tmp_path / "home"
    environment = {**interpreter_probe_env(tmp_path, managed=managed)}
    if sys.platform != "win32":
        environment["HOME"] = str(home)
    environment.update(plant_library_path(tmp_path, kind, marker, home=home))
    result = _run_with_path(tmp_path, runner, path_first=head, env=environment, cwd=_clean_cwd(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file() is (elevated == "$false"), result.stdout + result.stderr
    assert _reported_python(result) is not None, result.stdout


@pytest.mark.local_only
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated-control"])
def test_an_elevated_run_caches_in_the_accounts_own_cache_never_the_callers(tmp_path: Path, elevated: str) -> None:
    """[X21 P3], [X22 C1]: elevated, UV_CACHE_DIR is dropped and uv's cache is named from the account
    (the LocalAppData known folder on Windows, the passwd home on POSIX), never from LOCALAPPDATA,
    XDG_CACHE_HOME or HOME, with one line on stderr. In a test the account's cache home is answered
    inside tmp_path (with_account_cache_home), never the real user's ([93]). The control uses the
    caller's UV_CACHE_DIR, never creates the account's cache and prints no line."""
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated=elevated)
    caller_cache, caller_default = tmp_path / "caller-cache", tmp_path / "caller-default-cache"
    environment = {**interpreter_probe_env(tmp_path, managed=managed), "UV_CACHE_DIR": str(caller_cache)}
    environment.update({name: str(caller_default) for name in default_uv_cache_env(tmp_path)})
    result = _run_with_path(tmp_path, runner, path_first=head, env=environment, cwd=_clean_cwd(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert caller_cache.exists() is (elevated == "$false"), result.stderr
    assert not (caller_default / "uv").exists(), result.stderr
    account_cache = default_uv_cache_dir(tmp_path)
    assert account_cache.is_dir() is (elevated == "$true"), result.stderr
    assert (f"pwshpy: uv caches in {account_cache}:" in result.stderr) is (elevated == "$true"), result.stderr


@pytest.mark.local_only
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated"])
@pytest.mark.parametrize(
    "spec",
    ["./{wheel}", "{wheel}", "wheels/{wheel}", LOCAL_WHEEL_DISTRIBUTION + " @ ./{wheel}"],
    ids=["dot-slash", "bare-file", "subdirectory", "named-direct-reference"],
)
def test_a_relative_with_path_resolves_against_the_callers_directory(tmp_path: Path, elevated: str, spec: str) -> None:
    """[57]: a --with path relative to where the runner is started still resolves when uv starts elsewhere."""
    managed = _needs_managed_python()
    work = tmp_path / "work"
    wheel = write_local_wheel(work / "wheels" if spec.startswith("wheels/") else work)
    source = f"import {LOCAL_WHEEL_DISTRIBUTION}\nprint('wheel:' + {LOCAL_WHEEL_DISTRIBUTION}.VALUE)\n"
    runner, head = _python_runner(
        tmp_path, elevated=elevated, source=source, with_packages=[spec.format(wheel=wheel.name)]
    )
    probe = interpreter_probe_env(tmp_path, managed=managed)
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=work)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "wheel:from the wheel" in result.stdout, result.stdout


@pytest.mark.local_only
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated"])
def test_a_relative_cache_directory_still_runs_the_script(tmp_path: Path, elevated: str) -> None:
    """[57]: PWSHPY_PACK_CACHE given relative to the caller's directory still names the script uv runs."""
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated=elevated, source=_ENTRY_REPORTING_CWD)
    work = _clean_cwd(tmp_path)
    probe = {**interpreter_probe_env(tmp_path, managed=managed), "PWSHPY_PACK_CACHE": "relative-cache"}
    result = _run_with_path(tmp_path, runner, path_first=head, env=probe, cwd=work)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (work / "relative-cache").is_dir()
    reported = _reported_cwd(result)
    assert reported is not None and reported.samefile(work), result.stdout


# --- [61] [62] values the generated .ps1 carries as literals ----------------------------------------
# PowerShell ends a single-quoted literal at any of five quote characters, the typographic ones
# included, so a value holding one must be escaped where the packer writes it or the runner does not
# parse. These read a literal back out of a generated runner without running it.

#: Parses the runner (any parse error is reported), evaluates the right-hand side of one assignment
#: alone, and prints each value as UTF-16 code units, so a non-ASCII value survives any console code
#: page and Windows PowerShell 5.1 alike.
_READ_LITERAL = """param([string] $Path, [string] $Name)
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { [Console]::Out.WriteLine('parse-error:' + $errors[0].Message); exit 3 }
$found = $ast.Find({
    param($node)
    $node -is [Management.Automation.Language.AssignmentStatementAst] -and
        $node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
        $node.Left.VariablePath.UserPath -eq $Name
}, $true)
if (-not $found) { [Console]::Out.WriteLine('no-assignment'); exit 4 }
foreach ($value in @(& ([scriptblock]::Create($found.Right.Extent.Text)))) {
    [Console]::Out.WriteLine('value:' + ((([int[]][char[]][string]$value)) -join ','))
}
"""

#: Every quote character PowerShell reads as a single quote, plus a prime it does not, by code point.
_QUOTES_AND_A_PRIME = "".join(chr(c) for c in (0x27, 0x2018, 0x2019, 0x201A, 0x201B, 0x2032))


def _literal_values(tmp_path: Path, runner: Path, name: str) -> list[str]:
    """The values the runner's ``$<name> = ...`` assignment holds, read without running the runner."""
    assert _PWSH is not None
    reader = tmp_path / "read_literal.ps1"
    reader.write_text(_READ_LITERAL, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed argv built from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(reader), str(runner), name],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("value:"):
            units = [int(unit) for unit in line[6:].split(",") if unit]
            values.append("".join(map(chr, units)).encode("utf-16-le", "surrogatepass").decode("utf-16-le"))
    return values


def test_a_with_value_holding_typographic_quotes_parses_and_survives(tmp_path: Path) -> None:
    """[61]: the generated .ps1 parses, and $PwshPyUvArgs holds the --with value exactly as packed."""
    entry = tmp_path / "app.py"
    entry.write_text("print('hi')\n")
    spec = f"pkg{_QUOTES_AND_A_PRIME}x"
    runner = Path(pack_script(entry, options=PackOptions(with_packages=[spec])).output_path)
    assert _literal_values(tmp_path, runner, "PwshPyUvArgs") == ["--with", spec]


#: Entry names that end a quoted literal unless escaped: the ASCII quote, and a typographic one.
_QUOTED_ENTRY_NAMES = ("it's.py", f"it{chr(0x2019)}s.py")


@pytest.mark.parametrize("name", _QUOTED_ENTRY_NAMES, ids=["ascii-quote", "typographic-quote"])
def test_an_entry_named_with_a_quote_runs_and_round_trips(tmp_path: Path, name: str) -> None:
    """[62]: the runner parses, records the name exactly, runs the script, and unpack reads the name back."""
    entry = tmp_path / name
    entry.write_text("import sys\nprint('ran:' + str(len(sys.argv)))\n", encoding="utf-8")
    manifest = pack_script(entry, tmp_path / "quoted.ps1")
    runner = Path(manifest.output_path)
    assert _literal_values(tmp_path, runner, "PwshPyEntry") == [name]
    result = _run(runner, env=_isolated_cache(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ran:1" in result.stdout, result.stdout
    assert unpack_script(runner, tmp_path / "out").entry == name


def test_an_entry_with_a_non_ascii_name_runs_on_every_powershell(tmp_path: Path) -> None:
    """Windows PowerShell 5.1 reads a script without a BOM as ANSI: the name, and the file list that
    verifies the cache, must still arrive as packed."""
    name = "gr" + chr(0xFC) + chr(0xDF) + "e.py"
    entry = tmp_path / name
    entry.write_text("print('ran')\n", encoding="utf-8")
    runner = Path(pack_script(entry, tmp_path / "umlaut.ps1").output_path)
    assert _literal_values(tmp_path, runner, "PwshPyEntry") == [name]
    result = _run(runner, env=_isolated_cache(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ran" in result.stdout, result.stdout


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize("name", NETWORK_VARIABLES)
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated-control"])
def test_an_elevated_run_lets_no_proxy_certificate_or_keyring_variable_reach_uv(
    tmp_path: Path, name: str, elevated: str
) -> None:
    """[X22 E1]: elevated, a proxy (uv 0.13 sends its requests through each of these spellings), a
    certificate bundle or directory (they replace uv's trust roots), a client certificate, the
    platform-store switches and a keyring helper are each dropped with a line on stderr, before uv
    python find already; the control passes each on unchanged and prints no line."""
    runner, uv, record = _recording_runner(tmp_path, elevated=elevated)
    value = "1" if name in ("UV_NATIVE_TLS", "UV_SYSTEM_CERTS") else str(tmp_path / f"given-{name}")
    result = _run_with_path(
        tmp_path, runner, "-PwshPyUv", str(uv), path_first=[], env={name: value}, cwd=_clean_cwd(tmp_path)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = _recorded_variables(record)
    dropped_line = _VARIABLE_DROPPED.format(name=name) in result.stderr
    if elevated == "$true":
        found = _recorded_variables(Path(f"{record}.python"))
        assert name not in recorded and name not in found and dropped_line, (recorded, found, result.stderr)
    else:
        assert recorded.get(name) == value and not dropped_line, (recorded, result.stderr)


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated-control"])
def test_an_elevated_run_lets_no_git_variable_reach_uv(tmp_path: Path, elevated: str) -> None:
    """[X23 G1]: git, which uv runs for a git source, reads every GIT_ variable (a GIT_CONFIG_* pair sets
    any config key, a command-valued one included) and runs SSH_ASKPASS for a password. Elevated, every
    variable whose name starts with GIT_ (one no git reads included: the drop is by prefix) and
    SSH_ASKPASS is dropped with a line each, before uv python find already; one holding GIT_ elsewhere
    in its name stays. [113]: the same for Git Credential Manager's GCM_ prefix and PASSWORD_STORE_DIR.
    The control passes each on unchanged and prints no line."""
    runner, uv, record = _recording_runner(tmp_path, elevated=elevated)
    kept = {NOT_A_GIT_VARIABLE: "kept", NOT_A_GCM_VARIABLE: "kept"}
    given = {**GIT_VARIABLES, **kept}
    result = _run_with_path(tmp_path, runner, "-PwshPyUv", str(uv), path_first=[], env=given, cwd=_clean_cwd(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = _recorded_variables(record)
    assert {name: recorded.get(name) for name in kept} == kept, recorded
    for name, value in GIT_VARIABLES.items():
        dropped_line = _VARIABLE_DROPPED.format(name=name) in result.stderr
        if elevated == "$true":
            found = _recorded_variables(Path(f"{record}.python"))
            assert name not in recorded and name not in found and dropped_line, (recorded, found, result.stderr)
        else:
            assert recorded.get(name) == value and not dropped_line, (recorded, result.stderr)


#: Prints the names of the variables the packed script got that git or its credential helper reads
#: (GIT_ and GCM_ prefix, SSH_ASKPASS, PASSWORD_STORE_DIR) or that hold GIT_ or GCM_ elsewhere,
#: upper-cased: Python upper-cases os.environ's names on Windows.
_ENTRY_REPORTING_GIT = """import os
exact = ("SSH_ASKPASS", "PASSWORD_STORE_DIR")
names = sorted(n.upper() for n in os.environ if "GIT_" in n.upper() or "GCM_" in n.upper() or n.upper() in exact)
print("git:" + ",".join(names))
"""


@pytest.mark.local_only
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated-control"])
def test_an_elevated_run_lets_no_git_variable_reach_the_script(tmp_path: Path, elevated: str) -> None:
    """[X23 G1], end to end with the real uv and on Windows too: elevated, no variable whose name
    starts with GIT_ and no SSH_ASKPASS reaches uv or the script it runs; on Windows the prefix matches
    in any case, as the environment's names do (Git_Mixed_Case is GIT_MIXED_CASE there). One holding
    GIT_ elsewhere in its name reaches it in both arms; the control passes every one on. [113]: the
    same for the GCM_ prefix (Gcm_Mixed_Case on Windows) and PASSWORD_STORE_DIR."""
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated=elevated, source=_ENTRY_REPORTING_GIT)
    kept = {NOT_A_GIT_VARIABLE.upper(), NOT_A_GCM_VARIABLE.upper()}
    given = {**GIT_VARIABLES, **dict.fromkeys(kept, "kept")}
    if sys.platform == "win32":
        given["Git_Mixed_Case"] = "1"
        given["Gcm_Mixed_Case"] = "1"
    environment = {**interpreter_probe_env(tmp_path, managed=managed), **given}
    result = _run_with_path(tmp_path, runner, path_first=head, env=environment, cwd=_clean_cwd(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    (line,) = [line for line in result.stdout.splitlines() if line.startswith("git:")]
    reached = set(filter(None, line[4:].strip().split(",")))
    assert kept <= reached, result.stdout
    if elevated == "$true":
        # GIT_CONFIG_GLOBAL is the runner's own ([X24 G2]), set after the drop.
        assert reached == {*kept, "GIT_CONFIG_GLOBAL"}, (reached, result.stderr)
    else:
        assert {name.upper() for name in given} <= reached, (reached, result.stderr)
        assert "GIT_CONFIG_GLOBAL" not in reached, (reached, result.stderr)


# --- [X24 G2] git's global config file: HOME, USERPROFILE and XDG_CONFIG_HOME are the caller's ---------

#: What an elevated run on POSIX points git's global config at: a file only root can change. (Windows
#: names an empty file of its own: [117] below.)
_GIT_GLOBAL_SET = "pwshpy: setting GIT_CONFIG_GLOBAL=/dev/null"


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize("given", [None, "/caller/gitconfig"], ids=["caller-sets-none", "caller-sets-one"])
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated-control"])
def test_an_elevated_run_hands_uv_git_config_global_as_dev_null(
    tmp_path: Path, elevated: str, given: str | None
) -> None:
    """[X24 G2]: elevated, GIT_CONFIG_GLOBAL reaches uv as /dev/null, before uv python find already, with a
    line on stderr, whether the caller set none or one of its own (that one is dropped with the other
    GIT_ variables first). The control passes the caller's value, or none, on unchanged and prints no
    line."""
    runner, uv, record = _recording_runner(tmp_path, elevated=elevated)
    environment = {} if given is None else {"GIT_CONFIG_GLOBAL": given}
    result = _run_with_path(
        tmp_path, runner, "-PwshPyUv", str(uv), path_first=[], env=environment, cwd=_clean_cwd(tmp_path)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = _recorded_variables(record)
    if elevated == "$true":
        found = _recorded_variables(Path(f"{record}.python"))
        assert recorded.get("GIT_CONFIG_GLOBAL") == "/dev/null", (recorded, result.stderr)
        assert found.get("GIT_CONFIG_GLOBAL") == "/dev/null", (found, result.stderr)
        assert _GIT_GLOBAL_SET in result.stderr, result.stderr
        assert (_VARIABLE_DROPPED.format(name="GIT_CONFIG_GLOBAL") in result.stderr) is (given is not None)
    else:
        assert recorded.get("GIT_CONFIG_GLOBAL") == given, (recorded, result.stderr)
        assert _GIT_GLOBAL_SET not in result.stderr, result.stderr


@pytest.mark.local_only
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize("where", ["profile-gitconfig", "xdg-config-home"])
@pytest.mark.parametrize("elevated", ["$true", "$false"], ids=["elevated", "not-elevated-control"])
def test_an_elevated_run_never_runs_a_command_from_the_callers_global_git_config(
    tmp_path: Path, elevated: str, where: str
) -> None:
    """[X24 G2], end to end with the real uv and git, on Windows too: a core.fsmonitor command in the
    global git config below the profile the run was given (HOME, and USERPROFILE on Windows) or below
    its XDG_CONFIG_HOME runs from uv's git fetch of a --with git source (the control runs it: the plant
    fires). Elevated, git reads GIT_CONFIG_GLOBAL (an empty file of the runner's on Windows, [117];
    /dev/null on POSIX) instead: uv still fetches the source (its fetch appears in the cache),
    and the command never runs. The profile is a scratch directory, never the real one. Offline, so
    both arms then fail to build it."""
    managed = _needs_managed_python()
    marker = tmp_path / "git-hook-ran.txt"
    hook = tmp_path / "hook.sh"
    # A sh script on Windows too: Git for Windows starts it through its own sh (measured with 2.55).
    hook.write_text(f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\nexit 1\n', encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    source = f"pwshpy-git-probe @ git+{write_git_source(tmp_path).as_uri()}"
    runner, head = _python_runner(tmp_path, elevated=elevated, with_packages=[source])
    profile = tmp_path / "profile"
    profile.mkdir()
    environment = {**interpreter_probe_env(tmp_path, managed=managed), "UV_OFFLINE": "1", "HOME": str(profile)}
    if sys.platform == "win32":
        environment["USERPROFILE"] = str(profile)
    if where == "profile-gitconfig":
        config = profile / ".gitconfig"
    else:
        environment["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
        config = tmp_path / "xdg" / "git" / "config"
        config.parent.mkdir(parents=True)
    config.write_text(f"[core]\n\tfsmonitor = {hook.as_posix()}\n", encoding="utf-8", newline="\n")
    result = _run_with_path(tmp_path, runner, path_first=head, env=environment, cwd=_clean_cwd(tmp_path))
    caches = (default_uv_cache_dir(tmp_path), tmp_path / "uv-cache")
    assert any(fetched_git_source(cache) for cache in caches), "uv fetched no git source: " + result.stderr
    ran = marker.exists()
    assert ran is (elevated == "$false"), "global git config command ran: " + str(ran) + result.stderr


# --- [117] Windows: GIT_CONFIG_GLOBAL names an empty file of the runner's, not the NUL device ------------
# An MSYS2 or Cygwin git may read a bare NUL as a file named NUL in its working directory, so an
# elevated run on Windows points git at an empty file below its account's own data directory instead,
# judged by the trust rule every other elevated path passes.

#: The packed script under test: prints the GIT_CONFIG_GLOBAL it got.
_ENTRY_REPORTING_GIT_GLOBAL = """import os

print("gitglobal:" + os.environ.get("GIT_CONFIG_GLOBAL", ""))
"""
#: The refusal of a git configuration file another account could change.
_GIT_GLOBAL_REFUSED = "pwshpy: refusing the git configuration file"


def _empty_git_config(tmp_path: Path) -> Path:
    """Where an elevated test run's empty global git configuration lives: below its scratch data home."""
    return data_home(tmp_path) / "pwshpy" / "empty.gitconfig"


def _reported_git_global(result: subprocess.CompletedProcess[str]) -> str | None:
    for line in result.stdout.splitlines():
        if line.startswith("gitglobal:"):
            return line[len("gitglobal:") :].strip()
    return None


def _global_git_listing(profile: Path, git_global: Path | None) -> subprocess.CompletedProcess[str]:
    """``git config --global --list`` with ``profile`` as HOME and USERPROFILE, and GIT_CONFIG_GLOBAL
    set to ``git_global`` (unset when None)."""
    git = shutil.which("git")
    assert git is not None
    environment = {**os.environ, "HOME": str(profile), "USERPROFILE": str(profile)}
    environment.pop("XDG_CONFIG_HOME", None)
    environment.pop("GIT_CONFIG_GLOBAL", None)
    if git_global is not None:
        environment["GIT_CONFIG_GLOBAL"] = str(git_global)
    return subprocess.run(  # noqa: S603 - fixed argv, git resolved by shutil.which
        [git, "config", "--global", "--list"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=60,
        check=False,
    )


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
@pytest.mark.parametrize("state", ["absent", "already-empty"])
def test_an_elevated_run_on_windows_points_git_config_global_at_an_empty_file_of_its_own(
    tmp_path: Path, state: str
) -> None:
    """[117]: elevated on Windows, GIT_CONFIG_GLOBAL reaches the packed script as an empty file below the
    account's data directory (created when absent, reused when already there and empty), with a line on
    stderr, rather than the NUL device a non-Git-for-Windows git may read as a file in its working
    directory. Git for Windows reads that file as an empty global configuration: with a planted
    .gitconfig in the profile it lists nothing, while the same listing without the variable lists the
    plant (the control that the listing reads the profile at all)."""
    managed = _needs_managed_python()
    empty = _empty_git_config(tmp_path)
    if state == "already-empty":
        empty.parent.mkdir(parents=True)
        empty.touch()
    runner, head = _python_runner(tmp_path, elevated="$true", source=_ENTRY_REPORTING_GIT_GLOBAL)
    environment = interpreter_probe_env(tmp_path, managed=managed)
    result = _run_with_path(tmp_path, runner, path_first=head, env=environment, cwd=_clean_cwd(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    reported = _reported_git_global(result)
    assert reported is not None and Path(reported) == empty, (reported, result.stdout)
    assert empty.is_file() and empty.stat().st_size == 0, empty
    assert f"pwshpy: setting GIT_CONFIG_GLOBAL={empty}" in result.stderr, result.stderr
    if shutil.which("git") is None:
        return
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".gitconfig").write_text("[pwshpy]\n\tprobe = planted\n", encoding="utf-8", newline="\n")
    planted = _global_git_listing(profile, None)
    assert "pwshpy.probe=planted" in planted.stdout, planted.stdout + planted.stderr
    listing = _global_git_listing(profile, empty)
    # The exit code too ([131]): a git that cannot read the file prints nothing either, and exits 128.
    assert listing.returncode == 0, listing.stdout + listing.stderr
    assert listing.stdout == "", listing.stdout + listing.stderr


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
@pytest.mark.parametrize("where", ["directory-open", "file-open", "file-not-empty", "trusted-control"])
def test_an_elevated_run_on_windows_refuses_a_git_config_file_it_cannot_trust(tmp_path: Path, where: str) -> None:
    """[117]: the empty global git configuration an elevated run names is judged as every other elevated
    path is: when Users may modify its directory or the file, or the file is not empty, the run exits
    127 naming the file, and the packed script never runs. The control, the same file left trusted and
    empty, runs the script."""
    managed = _needs_managed_python()
    empty = _empty_git_config(tmp_path)
    empty.parent.mkdir(parents=True)
    empty.touch()
    if where == "directory-open":
        _icacls(empty.parent, "/grant", f"{_SID_USERS}:(M)")
    elif where == "file-open":
        _icacls(empty, "/grant", f"{_SID_USERS}:(M)")
    elif where == "file-not-empty":
        empty.write_text("[core]\n\tfsmonitor = planted\n", encoding="utf-8")
    runner, head = _python_runner(tmp_path, elevated="$true", source=_ENTRY_REPORTING_GIT_GLOBAL)
    environment = interpreter_probe_env(tmp_path, managed=managed)
    result = _run_with_path(tmp_path, runner, path_first=head, env=environment, cwd=_clean_cwd(tmp_path))
    if where == "trusted-control":
        assert result.returncode == 0, result.stdout + result.stderr
        assert _reported_git_global(result) is not None, result.stdout
        return
    assert result.returncode == 127, result.stdout + result.stderr
    assert _GIT_GLOBAL_REFUSED in result.stderr and str(empty) in result.stderr, result.stderr
    assert _reported_git_global(result) is None, result.stdout


def _junction(link: Path, target: Path) -> None:
    """Make ``link`` a directory junction to ``target`` (no privilege needed, unlike a symlink)."""
    result = subprocess.run(  # noqa: S603 - fixed argv; cmd is a system tool
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607 - resolved from System32
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
@pytest.mark.parametrize("where", ["pwshpy-is-a-file", "data-home-linked-to-an-open-directory", "dangling-link"])
def test_an_elevated_run_on_windows_creates_nothing_where_its_git_config_file_cannot_be_trusted(
    tmp_path: Path, where: str
) -> None:
    """[131]: the data directory is judged before the runner creates its ``pwshpy`` directory there, and
    a create that fails is refused like any other untrusted git configuration file: exit 127 naming the
    file, never the bare exception's 1 (or 70 in an elevated child). A data directory that is a junction
    to a directory Users may modify gets nothing created at the junction's target. A dangling link in
    the file's place, which Get-Item may not see, is refused as well."""
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated="$true", source=_ENTRY_REPORTING_GIT_GLOBAL)
    environment = interpreter_probe_env(tmp_path, managed=managed)
    home = data_home(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    empty = _empty_git_config(tmp_path)
    opened = tmp_path / "open-data-home"
    if where == "pwshpy-is-a-file":
        empty.parent.write_text("", encoding="utf-8")
    elif where == "dangling-link":
        empty.parent.mkdir()
        empty.symlink_to(tmp_path / "no-such-file")
    else:
        shutil.move(str(home), str(opened))
        _icacls(opened, "/grant", f"{_SID_USERS}:(OI)(CI)M")
        _junction(home, opened)
    result = _run_with_path(tmp_path, runner, path_first=head, env=environment, cwd=_clean_cwd(tmp_path))
    assert result.returncode == 127, result.stdout + result.stderr
    assert _GIT_GLOBAL_REFUSED in result.stderr and str(empty) in result.stderr, result.stderr
    assert _reported_git_global(result) is None, result.stdout
    assert not (opened / "pwshpy").exists(), "the runner created its directory at the junction's target"


def _cache_root(path: Path, *, others_can_modify: bool) -> Path:
    """A pack-cache root: one another account can modify, or one only this account (and the admins) can.

    POSIX: mode 0777, or 0755 whatever the umask (an elevated test run counts this account as root).
    """
    if others_can_modify:
        return _user_writable_dir(path)
    if sys.platform == "win32":
        return _private_dir(path)
    path.mkdir(parents=True)
    path.chmod(0o755)
    return path


def _open_to_others(path: Path) -> None:
    """Let every account modify the file ``path`` (Users on Windows, world write on POSIX)."""
    if sys.platform == "win32":
        _icacls(path, "/grant", f"{_SID_USERS}:M")
    else:
        path.chmod(0o666)


def _cache_trust_runner(tmp_path: Path) -> tuple[Path, Path, str]:
    """An elevated stand-in whose cache check is the shipped one, the marker its script writes, its key.

    The packed script writes the marker when it runs. The key is the cache directory's name.
    """
    marker = tmp_path / "ran.marker"
    entry = tmp_path / "mark.py"
    entry.write_text(f"open({str(marker)!r}, 'w').close()\n")
    manifest = pack_script(entry)
    runner = _elevation_stand_in(
        Path(manifest.output_path),
        tmp_path / "cache_stand_in.ps1",
        elevated="$true",
        trust_my_uid=True,
        judge_cache=True,
    )
    return runner, marker, manifest.payload_sha256[:16]


def _cache_trust_run(tmp_path: Path, runner: Path, root: Path) -> subprocess.CompletedProcess[str]:
    """Run the stand-in from :func:`_cache_trust_runner` with its pack cache below ``root``.

    No managed Python is offered, so a run that gets past the cache check stops later: these tests are
    about whether the cache was judged, not about the run that follows. Under umask 022, as sudo runs
    root (its default umask is the union of the caller's and 022): a host's 002 would make the unpacked
    tree group-writable, which the root rule refuses.
    """
    env = {"PWSHPY_PACK_CACHE": str(root), **managed_python_env(tmp_path, managed=None)}
    saved = os.umask(0o022)
    try:
        return _run_with_path(tmp_path, runner, path_first=[_UvLocation.directory], env=env)
    finally:
        os.umask(saved)


#: What an elevated run says when it refuses its pack cache ([98]).
_CACHE_REFUSAL = "pwshpy: refusing the cache directory "


@pytest.mark.parametrize("others_can_modify", [False, True], ids=["private-root", "root-others-can-modify"])
def test_an_elevated_run_refuses_a_pack_cache_another_account_can_modify(
    tmp_path: Path, others_can_modify: bool
) -> None:
    """[98]: an elevated run judges the cache root before it writes anything there, and refuses with 127.

    The private root is the liveness arm: the same run gets past the check and unpacks there.
    """
    root = _cache_root(tmp_path / "cache-root", others_can_modify=others_can_modify)
    runner, marker, key = _cache_trust_runner(tmp_path)
    result = _cache_trust_run(tmp_path, runner, root)
    cache = root / key
    if not others_can_modify:
        assert _CACHE_REFUSAL not in result.stderr, result.stderr
        assert (cache / "mark.py").is_file(), result.stdout + result.stderr
        return
    assert result.returncode == 127, result.stdout + result.stderr
    assert f"{_CACHE_REFUSAL}{cache}: " in result.stderr, result.stderr
    assert not cache.exists(), "the elevated run unpacked into a cache another account can modify"
    assert not marker.exists()


def test_an_elevated_run_refuses_a_cached_file_another_account_can_modify(tmp_path: Path) -> None:
    """[98]: a file below the cache directory that another account can modify refuses a warm run too.

    Its content still matches its recorded hash, so the per-file check alone would run it.
    """
    root = _cache_root(tmp_path / "cache-root", others_can_modify=False)
    runner, marker, key = _cache_trust_runner(tmp_path)
    cache = root / key
    first = _cache_trust_run(tmp_path, runner, root)
    assert (cache / "mark.py").is_file(), first.stdout + first.stderr
    _open_to_others(cache / "mark.py")
    result = _cache_trust_run(tmp_path, runner, root)
    assert result.returncode == 127, result.stdout + result.stderr
    assert _CACHE_REFUSAL in result.stderr and "mark.py" in result.stderr, result.stderr
    assert not marker.exists()


# --- [156] elevated .ps1 runs of a script with a git source refuse a git older than 2.32 ----------------

#: The start of the refusal an elevated run prints for a git that may ignore GIT_CONFIG_GLOBAL.
_OLD_GIT_REFUSED = "pwshpy: refusing git at "
#: The remedy that refusal names.
_OLD_GIT_REMEDY = "upgrade git, or run the script without elevation"
#: A git source the recording double never fetches (POSIX) and a real uv cannot reach (Windows).
_GIT_SOURCE_PACKAGE = "pwshpy-git-probe @ git+https://example.invalid/r.git"
#: Where every Windows host keeps the .NET Framework C# compiler, which builds a git.exe double.
_CSC = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"


def _posix_git_double(directory: Path, marker: Path, *, version_output: str, git_exit: int) -> Path:
    """A ``git`` (POSIX) that records it ran, prints ``version_output`` and exits ``git_exit``."""
    _marker_double(directory, "git", marker, f"printf '%s\\n' '{version_output}'\nexit {git_exit}")
    return directory / "git"


def _windows_git_double(directory: Path, marker: Path, *, version_output: str) -> Path:
    """A ``git.exe`` (Windows) that records it ran and prints ``version_output``, built with csc.

    The runner looks for git.exe by name, so a script double cannot stand in for it.
    """
    source = directory / "git_double.cs"
    source.write_text(
        "class GitDouble { static int Main() { System.IO.File.AppendAllText("
        f'@"{marker}", "ran\\n"); System.Console.WriteLine("{version_output}"); return 0; }} }}\n',
        encoding="utf-8",
    )
    target = directory / "git.exe"
    result = subprocess.run(  # noqa: S603 - fixed argv; csc ships with the .NET Framework on every Windows
        [str(_CSC), "/nologo", f"/out:{target}", str(source)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    source.unlink()
    return target


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize(
    ("version_output", "git_exit", "refused_version"),
    [
        pytest.param("git version 2.31.0", 0, "2.31.0", id="2.31-refused"),
        pytest.param("git version 1.99.9", 0, "1.99.9", id="older-major-refused"),
        pytest.param("git version 2.32.0", 0, None, id="2.32-runs"),
        pytest.param("git version 2.43.1.windows.1", 0, None, id="windows-suffix-runs"),
        pytest.param("git version 2.39.3 (Apple Git-145)", 0, None, id="apple-suffix-runs"),
        pytest.param("git version 10.0.0", 0, None, id="two-digit-major-runs"),
        pytest.param("not git at all", 0, "", id="unparsable-refused"),
        pytest.param("git version 2", 0, "", id="no-minor-refused"),
        pytest.param("git version 2.40.0", 1, "", id="failing-git-refused"),
    ],
)
def test_an_elevated_run_of_a_git_source_needs_git_2_32(
    tmp_path: Path, version_output: str, git_exit: int, refused_version: str | None
) -> None:
    """[156]: as the .sh root run ([116]), an elevated .ps1 run of a script with a git source asks the
    git uv will run for its version and exits 127 when it is older than 2.32, unreadable, or the command
    fails, before uv is asked for a managed Python or runs the script; 2.32 or later runs."""
    runner, uv, record = _recording_runner(tmp_path, elevated="$true", with_packages=[_GIT_SOURCE_PACKAGE])
    marker = tmp_path / "git-ran.txt"
    git = _posix_git_double(uv.parent, marker, version_output=version_output, git_exit=git_exit)
    result = _run_with_path(
        tmp_path, runner, "-PwshPyUv", str(uv), path_first=[uv.parent], env={}, cwd=_clean_cwd(tmp_path)
    )
    assert marker.exists(), "the runner never asked git for its version: " + result.stderr
    refusals = [line for line in result.stderr.splitlines() if line.startswith(_OLD_GIT_REFUSED)]
    if refused_version is None:
        assert result.returncode == 0, result.stdout + result.stderr
        assert refusals == [], result.stderr
        assert "run" in record.read_text(encoding="utf-8").splitlines(), "uv never ran the script"
        assert Path(f"{record}.python").exists(), "uv was never asked for a Python"
        return
    assert result.returncode == 127, result.stdout + result.stderr
    assert not record.exists(), "uv ran the script"
    assert not Path(f"{record}.python").exists(), "uv was asked for a Python"
    assert len(refusals) == 1, result.stderr
    (refusal,) = refusals
    assert refusal.startswith(f"{_OLD_GIT_REFUSED}{git}: "), refusal
    if refused_version:
        assert f": git {refused_version} ignores GIT_CONFIG_GLOBAL" in refusal, refusal
    assert "GIT_CONFIG_GLOBAL" in refusal and "2.32 or later" in refusal, refusal
    assert refusal.endswith(_OLD_GIT_REMEDY), refusal


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
@pytest.mark.parametrize(
    ("elevated", "git_source"),
    [("$true", False), ("$false", True)],
    ids=["elevated-without-git-source", "not-elevated"],
)
def test_an_old_git_is_neither_asked_nor_refused_without_elevation_and_a_git_source(
    tmp_path: Path, elevated: str, git_source: bool
) -> None:
    """[156]: only an elevated run of a script with a git source asks git; the others run on an old git."""
    packages = [_GIT_SOURCE_PACKAGE] if git_source else []
    runner, uv, record = _recording_runner(tmp_path, elevated=elevated, with_packages=packages)
    marker = tmp_path / "git-ran.txt"
    _posix_git_double(uv.parent, marker, version_output="git version 2.25.1", git_exit=0)
    result = _run_with_path(
        tmp_path, runner, "-PwshPyUv", str(uv), path_first=[uv.parent], env={}, cwd=_clean_cwd(tmp_path)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists(), "the runner ran git --version"
    assert _OLD_GIT_REFUSED not in result.stderr, result.stderr
    assert "run" in record.read_text(encoding="utf-8").splitlines(), "uv never ran the script"


@pytest.mark.local_only
@pytest.mark.os_windows
@_WINDOWS_ONLY
@pytest.mark.skipif(not _CSC.is_file(), reason="needs the .NET Framework C# compiler to build a git.exe double")
@pytest.mark.parametrize(
    ("version_output", "refused"),
    [
        pytest.param("git version 2.31.1.windows.1", True, id="2.31-refused"),
        pytest.param("git version 2.32.0.windows.1", False, id="2.32-passes-the-check"),
    ],
)
def test_an_elevated_windows_run_of_a_git_source_needs_git_2_32(
    tmp_path: Path, version_output: str, refused: bool
) -> None:
    """[156] on Windows (pwsh 7 and, with PWSHPY_TEST_POWERSHELL=powershell, Windows PowerShell 5.1): a
    git.exe double first on a trusted PATH directory answers the version. Refused, the run exits 127 with
    the remedy and uv never runs the script; 2.32 passes the check (uv then fails to fetch the source
    from the double, which is not under test)."""
    managed = _needs_managed_python()
    runner, head = _python_runner(tmp_path, elevated="$true", with_packages=[_GIT_SOURCE_PACKAGE])
    marker = tmp_path / "git-ran.txt"
    git = _windows_git_double(_private_dir(tmp_path / "git-bin"), marker, version_output=version_output)
    environment = interpreter_probe_env(tmp_path, managed=managed)
    result = _run_with_path(tmp_path, runner, path_first=[git.parent, *head], env=environment, cwd=_clean_cwd(tmp_path))
    assert marker.exists(), "the runner never asked git for its version: " + result.stderr
    refusals = [line for line in result.stderr.splitlines() if line.startswith(_OLD_GIT_REFUSED)]
    if not refused:
        assert refusals == [], result.stderr
        assert result.returncode != 127, result.stdout + result.stderr
        return
    assert result.returncode == 127, result.stdout + result.stderr
    assert len(refusals) == 1, result.stderr
    assert refusals[0].startswith(f"{_OLD_GIT_REFUSED}{git}: git 2.31.1.windows.1 ignores GIT_CONFIG_GLOBAL"), refusals
    assert refusals[0].endswith(_OLD_GIT_REMEDY), refusals
    assert "python:" not in result.stdout, result.stdout


@pytest.mark.parametrize("git_source", [True, False], ids=["git-source", "none"])
def test_info_names_whether_the_script_has_a_git_source(tmp_path: Path, git_source: bool) -> None:
    """[156]: -PwshPyInfo shows what the pack recorded, so a refused elevated run can be explained."""
    entry = tmp_path / "app.py"
    entry.write_text("print('hi')\n")
    packages = [_GIT_SOURCE_PACKAGE] if git_source else []
    manifest = pack_script(entry, options=PackOptions(with_packages=packages))
    result = _run(Path(manifest.output_path), "-PwshPyInfo", env=_isolated_cache(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    expected = "git source : yes (an elevated run needs git 2.32 or later)" if git_source else "git source : no"
    assert expected in result.stdout.splitlines(), result.stdout
