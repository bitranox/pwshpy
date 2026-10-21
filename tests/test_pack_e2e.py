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

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from elevation_doubles import RUNNING_AS_ROOT, link_tools, write_fake_sudo

from pwshpy.adapters.native.packer import pack_script, unpack_script
from pwshpy.domain.packing import PackOptions

pytestmark = [pytest.mark.os_agnostic]

_PWSH = shutil.which("pwsh") or shutil.which("powershell")
_UV = shutil.which("uv")
#: The POSIX path the runner probes whatever the environment says (pack_runner.ps1).
_SYSTEM_WIDE_UV = "/usr/local/bin/uv"
#: Variables that point the runner's uv lookup at an install directory.
_UV_LOCATION_VARIABLES = ("XDG_BIN_HOME", "CARGO_HOME")

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
    # Pin the packed script's stdout to UTF-8 and decode it as UTF-8, so a non-ASCII argument
    # round-trips through the assertion regardless of the host's console code page. Without this,
    # `text=True` decodes the child's UTF-8 output as cp1252 on Windows and mangles it - a capture
    # artefact, not a packer defect (the argument reaches sys.argv intact either way).
    child_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(  # noqa: S603 - fixed argv built from a resolved interpreter path
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(runner), *arguments],
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
        "PATH": os.pathsep.join([str(Path(_UV or "uv").parent), os.environ.get("PATH", "")]),
        **_isolated_cache(tmp_path),
    }
    assert _PWSH is not None
    argv = [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(manifest.output_path), "concurrent"]
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
