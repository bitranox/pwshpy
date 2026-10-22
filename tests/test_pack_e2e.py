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

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import cast

import pytest
from elevation_doubles import RUNNING_AS_ROOT, link_tools, write_fake_sudo

from pwshpy.adapters.native.packer import pack_script, unpack_script
from pwshpy.domain.packing import PackOptions

pytestmark = [pytest.mark.os_agnostic]

_PWSH = shutil.which("pwsh") or shutil.which("powershell")
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
    return subprocess.run(  # noqa: S603 - fixed argv built from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(runner), _NEVER_INSTALL_UV, *arguments],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
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
    manifest = pack_script(_project(tmp_path))
    environment = {
        **os.environ,
        "PATH": os.pathsep.join([str(_UvLocation.directory), os.environ.get("PATH", "")]),
        **_isolated_cache(tmp_path),
    }
    assert _PWSH is not None
    argv = [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(manifest.output_path), _NEVER_INSTALL_UV, "concurrent"]
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
    write_fake_sudo(bin_dir, grant=False)
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
    write_fake_sudo(bin_dir, grant=True)
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


def _whole_runner_stand_in(runner: Path, output: Path, *, elevated: str = "$false") -> Path:
    """Turn a whole packed runner into a Windows-relaunch stand-in that runs its REAL body.

    Three exact, counted substitutions and nothing else: the host is declared Windows so the UAC
    branch of Invoke-PwshPyElevated is taken; the 'runas' verb becomes '' because no test can answer
    a UAC prompt; and the elevation probe answers ``elevated`` (a PowerShell expression, "not
    elevated" by default: the Windows token API does not exist off Windows). The child the relaunch
    starts is this same file, so it unpacks, finds uv and fails exactly where the shipped runner
    would. ``[bool]$PwshPyRelay`` answers "elevated" in the relaunched child only, as UAC would.
    """
    text = runner.read_text(encoding="utf-8")
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


#: The runner's POSIX trusted owners (root alone); a test widens it so its own files stand in for
#: root-owned ones, which no unprivileged test can create.
_TRUSTED_UIDS = "$PwshPyTrustedUids = @('0')"


def _elevation_stand_in(
    runner: Path, output: Path, *, elevated: str, running_sid: str | None = None, trust_my_uid: bool = False
) -> Path:
    """The whole runner with its elevation probe answering ``elevated`` (a PowerShell expression).

    One exact substitution and nothing else, so the uv lookup, the pin and the run are the shipped
    code. Without -PwshPyElevate nothing relaunches, so this probes the uv trust rule alone.
    ``running_sid`` also makes the runner believe it runs as that SID ('S-1-5-18' stands in for a
    SYSTEM scheduled task, which no test can start without changing the host). ``trust_my_uid``
    (POSIX) counts this test's uid as root for ownership, so a layout the test builds can pass or
    fail on everything but its owner.
    """
    text = runner.read_text(encoding="utf-8")
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
    tmp_path: Path, runner: Path, *arguments: str, path_first: Sequence[Path], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a runner with exactly ``path_first`` ahead of the inherited PATH (no real uv added)."""
    assert _PWSH is not None
    child_env = {**os.environ, **_isolated_cache(tmp_path), "PYTHONIOENCODING": "utf-8", **(env or {})}
    child_env.pop("PWSHPY_TEST_UV", None)
    child_env["PATH"] = os.pathsep.join([*map(str, path_first), os.environ.get("PATH", "")])
    return subprocess.run(  # noqa: S603 - fixed argv built from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(runner), _NEVER_INSTALL_UV, *arguments],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
        env=child_env,
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
        expected_other = f"its directory {other} is owned by uid {os.getuid()}, not root"
    other_uv = _uv_stand_in(other)
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
    the argument ``run`` with its own exit code 1.
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


@pytest.mark.local_only
@pytest.mark.os_posix
@_POSIX_ONLY
def test_the_posix_root_probe_never_asks_a_planted_id(tmp_path: Path) -> None:
    """The .ps1's euid comes from an id at a fixed path: a planted id answering 1000 would switch the rule off."""
    marker = tmp_path / "ran.txt"
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    _marker_double(fake, "id", marker, "echo 0")
    entry = tmp_path / "which_uv.py"
    entry.write_text(_ENTRY_REPORTING_UV)
    manifest = pack_script(entry)
    result = _run_with_path(tmp_path, Path(manifest.output_path), path_first=[fake, Path(_UV or "uv").parent])
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
    """/bin/python3 resolves through /bin -> usr/bin and python3's own link and is accepted, then run."""
    runner = _trust_runner(tmp_path, elevated="$true")
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
    replacement = "@(" + ", ".join(f"'{fixed / f'id{n}'}'" for n in range(6)) + ")"
    runner.write_text(text.replace(_FIXED_IDS, replacement), encoding="utf-8")
    result = _run_with_path(tmp_path, runner, path_first=[planted.parent])
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
    # decoy? does not exist, so it is no candidate at all - not one read as matching decoyX.
    assert str(pattern) not in result.stderr, result.stderr


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
