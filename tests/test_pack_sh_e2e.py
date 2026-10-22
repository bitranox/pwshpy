"""End-to-end: pack a script to a POSIX ``.sh`` and RUN it under every available major shell.

The `.sh` runner is strict POSIX, so it must behave identically under whatever ``/bin/sh`` is -
dash, busybox ash, bash-as-sh, zsh - not only bash.  Each installed interpreter runs the SAME
artefact and must produce the same result: the payload unpacks, uv resolves the PEP 723 deps, the
local submodule imports, arguments arrive intact, and the script's exit code comes back.

``local_only``: needs a POSIX shell + uv, and the dependency cases reach PyPI.  Skips cleanly on
Windows (no POSIX ``sh``) and when uv is absent.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from elevation_doubles import (
    RUNNING_AS_ROOT,
    link_tools,
    managed_python_dir,
    write_fake_sudo,
    write_recording_uv,
)

from pwshpy.adapters.native.packer import pack_script, unpack_script
from pwshpy.domain.enums import RunnerFormat
from pwshpy.domain.packing import PackOptions

pytestmark = [pytest.mark.local_only, pytest.mark.os_posix]

_UV = shutil.which("uv")

if _UV is None or os.name != "posix":  # pragma: no cover - environment gate
    pytest.skip("the .sh packer end-to-end test needs a POSIX shell and uv", allow_module_level=True)


def _available_shells() -> list[tuple[str, list[str]]]:
    """The major POSIX shells present on this host, as (id, argv-prefix) pairs.

    Every one interprets the same strict-POSIX runner; running under each proves the artefact is
    not bash-only.  ``busybox ash`` is the strictest (Alpine/containers), ``dash`` is Debian's
    ``/bin/sh``.
    """
    found: list[tuple[str, list[str]]] = []
    for name, argv in (("sh", ["sh"]), ("dash", ["dash"]), ("bash", ["bash"]), ("zsh", ["zsh"])):
        if shutil.which(name):
            found.append((name, argv))
    if shutil.which("busybox"):
        found.append(("busybox-ash", ["busybox", "sh"]))
    return found


_SHELLS = _available_shells()

_ENTRY = """import sys
from pkg import helper

print("greeting:" + helper.greet())
print("argv:" + repr(sys.argv[1:]))
sys.exit(helper.EXIT_CODE)
"""


def _project(root: Path, *, exit_code: int = 0) -> Path:
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "helper.py").write_text(
        f'EXIT_CODE = {exit_code}\n\n\ndef greet():\n    return "hello from the submodule"\n'
    )
    entry = root / "app.py"
    entry.write_text(_ENTRY)
    return entry


def _pack_sh(entry: Path, dest: Path, **opts: object) -> Path:
    manifest = pack_script(entry, dest, options=PackOptions(format=RunnerFormat.SH, **opts))  # type: ignore[arg-type]
    return Path(manifest.output_path)


def _env(tmp_path: Path) -> dict[str, str]:
    child = {**os.environ, "PYTHONIOENCODING": "utf-8", "PWSHPY_PACK_CACHE": str(tmp_path / "cache")}
    # Pin the uv the test found so a blank PATH never exercises the network installer.
    child["PATH"] = os.pathsep.join([str(Path(_UV or "uv").parent), child.get("PATH", "")])
    return child


def _run(shell: list[str], runner: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv from resolved shell + our own artefact
        [*shell, str(runner), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
        env=env,
    )


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_runs_under_each_major_shell(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """The same `.sh` artefact runs, imports its submodule, and returns its exit code under each shell."""
    runner = _pack_sh(_project(tmp_path, exit_code=7), tmp_path / "app.sh")
    result = _run(shell, runner, env=_env(tmp_path))
    assert result.returncode == 7, result.stderr
    assert "greeting:hello from the submodule" in result.stdout


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["plain", "two"], id="plain"),
        pytest.param(["with space", "trailing "], id="spaces"),
        pytest.param(['embedded "quotes"', "it's", "a$b`c"], id="quotes-and-dollars"),
        pytest.param(["", "after-empty"], id="empty-string"),
        pytest.param(["-v", "--flag", "--other=x"], id="flags"),
        pytest.param(["grüße", "日本語", "emoji-\U0001f600"], id="unicode"),
    ],
)
def test_arguments_arrive_exactly_as_typed(
    tmp_path: Path, shell_id: str, shell: list[str], arguments: list[str]
) -> None:
    """POSIX ``sh`` forwards ``"$@"`` intact - no re-quoting, no shim needed."""
    runner = _pack_sh(_project(tmp_path), tmp_path / "app.sh")
    result = _run(shell, runner, *arguments, env=_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert f"argv:{arguments!r}" in result.stdout


def test_pep723_dependency_is_resolved_by_uv(tmp_path: Path) -> None:
    entry = tmp_path / "app.py"
    entry.write_text(
        '# /// script\n# dependencies = ["cowsay"]\n# ///\nimport cowsay\n\nprint("dep:" + cowsay.__name__)\n'
    )
    runner = _pack_sh(entry, tmp_path / "app.sh")
    result = _run(["sh"], runner, env=_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "dep:cowsay" in result.stdout


def test_with_packages_dependency_is_resolved(tmp_path: Path) -> None:
    entry = tmp_path / "app.py"
    entry.write_text('import cowsay\n\nprint("dep:" + cowsay.__name__)\n')
    runner = _pack_sh(entry, tmp_path / "app.sh", with_packages=["cowsay"])
    result = _run(["sh"], runner, env=_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "dep:cowsay" in result.stdout


def test_second_run_reuses_the_cache(tmp_path: Path) -> None:
    runner = _pack_sh(_project(tmp_path), tmp_path / "app.sh")
    environment = _env(tmp_path)
    manifest = pack_script(tmp_path / "app.py", tmp_path / "again.sh", options=PackOptions(format=RunnerFormat.SH))
    assert _run(["sh"], runner, env=environment).returncode == 0
    witness = tmp_path / "cache" / manifest.payload_sha256[:16] / "witness.txt"
    witness.write_text("planted")
    assert _run(["sh"], runner, env=environment).returncode == 0
    assert witness.exists(), "the payload was extracted a second time"


def test_concurrent_cold_starts_do_not_race(tmp_path: Path) -> None:
    """Two cold-cache runs of the same pack must both succeed (the lock serializes extraction)."""
    runner = _pack_sh(_project(tmp_path), tmp_path / "app.sh")
    environment = _env(tmp_path)
    argv = ["sh", str(runner), "concurrent"]
    first = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)  # noqa: S603
    second = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)  # noqa: S603
    out_a, err_a = first.communicate(timeout=300)
    out_b, err_b = second.communicate(timeout=300)
    assert first.returncode == 0, err_a
    assert second.returncode == 0, err_b
    assert "greeting:hello from the submodule" in out_a
    assert "greeting:hello from the submodule" in out_b


def _shadow_tools(tmp_path: Path, counter: Path) -> Path:
    """A PATH directory whose ``mkdir`` never refuses an existing directory and whose ``tar`` counts.

    The ``mkdir`` copies uutils coreutils 0.8.0 (Ubuntu 26.04's ``/usr/bin/mkdir``), which lets two
    racing calls on one path both exit 0. The ``tar`` records every extraction and holds the
    window open for a second, so two cold starts that both pass the lock always overlap.
    """
    real_mkdir = shutil.which("mkdir")
    real_tar = shutil.which("tar")
    assert real_mkdir is not None
    assert real_tar is not None
    shadow = tmp_path / "shadow-bin"
    shadow.mkdir()
    mkdir = shadow / "mkdir"
    mkdir.write_text(f'#!/bin/sh\nexec "{real_mkdir}" -p "$@"\n', newline="\n")
    tar = shadow / "tar"
    tar.write_text(f'#!/bin/sh\necho x >> "{counter}"\nsleep 1\nexec "{real_tar}" "$@"\n', newline="\n")
    for tool in (mkdir, tar):
        tool.chmod(0o755)
    return shadow


def test_concurrent_cold_starts_extract_once_when_mkdir_is_not_exclusive(tmp_path: Path) -> None:
    """The extraction lock must not rest on ``mkdir`` refusing an existing directory."""
    runner = _pack_sh(_project(tmp_path), tmp_path / "app.sh")
    counter = tmp_path / "extractions.txt"
    environment = _env(tmp_path)
    environment["PATH"] = os.pathsep.join([str(_shadow_tools(tmp_path, counter)), environment["PATH"]])
    argv = ["sh", str(runner)]
    first = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)  # noqa: S603
    second = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)  # noqa: S603
    _, err_a = first.communicate(timeout=300)
    _, err_b = second.communicate(timeout=300)

    assert first.returncode == 0, err_a
    assert second.returncode == 0, err_b
    assert counter.read_text().count("x") == 1, "both cold starts extracted the payload"


def test_tampered_cache_is_detected_and_replaced(tmp_path: Path) -> None:
    runner = _pack_sh(_project(tmp_path), tmp_path / "app.sh")
    environment = _env(tmp_path)
    manifest = pack_script(tmp_path / "app.py", tmp_path / "again.sh", options=PackOptions(format=RunnerFormat.SH))
    assert _run(["sh"], runner, env=environment).returncode == 0
    planted = tmp_path / "cache" / manifest.payload_sha256[:16] / "pkg" / "helper.py"
    planted.write_text('EXIT_CODE = 0\n\n\ndef greet():\n    return "TAMPERED"\n')
    result = _run(["sh"], runner, env=environment)
    assert result.returncode == 0, result.stderr
    assert "TAMPERED" not in result.stdout


def test_corrupt_payload_is_rejected(tmp_path: Path) -> None:
    runner = _pack_sh(_project(tmp_path), tmp_path / "app.sh")
    lines = runner.read_text().splitlines()
    # The heredoc OPENER, not its terminator: the line after the terminator is shell code, and
    # damaging it fails the runner with a syntax error rather than a corrupt payload.
    start = next(i for i, line in enumerate(lines) if line.rstrip().endswith("<<'__PWSHPY_PAYLOAD_B64__'"))
    body = lines[start + 1]
    lines[start + 1] = ("B" if body[:1] != "B" else "C") + body[1:]
    runner.write_text("\n".join(lines) + "\n")
    result = _run(["sh"], runner, env=_env(tmp_path))
    assert result.returncode != 0
    # The exact sentence: a bare "corrupt" also matches this test's own name in any tmp path printed.
    assert "pwshpy pack: the embedded payload is corrupt" in result.stderr
    assert not list((tmp_path / "cache").glob("*.lock")), "the failed run left its lock behind"


def test_info_switch_reports_manifest_without_running(tmp_path: Path) -> None:
    manifest = pack_script(_project(tmp_path), tmp_path / "app.sh", options=PackOptions(format=RunnerFormat.SH))
    result = _run(["sh"], Path(manifest.output_path), "--pwshpy-info", env=_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert manifest.payload_sha256 in result.stdout
    assert "pkg/helper.py" in result.stdout
    assert "greeting:" not in result.stdout


def test_help_switch_explains_unpack_and_repack(tmp_path: Path) -> None:
    runner = _pack_sh(_project(tmp_path), tmp_path / "app.sh")
    result = _run(["sh"], runner, "--pwshpy-help", env=_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "uvx pwshpy unpack" in result.stdout
    assert "greeting:" not in result.stdout


def test_no_install_uv_fails_cleanly_when_uv_absent(tmp_path: Path) -> None:
    # uv genuinely absent, but the base64/sha256/tar coreutils the runner needs ARE present: a
    # blank PATH would trip the decoder probe (exit 1) instead of the uv check this exercises.
    if Path("/usr/local/bin/uv").exists():  # a fixed fallback the runner probes regardless of PATH
        pytest.skip("uv is installed at a fixed fallback path; cannot simulate a uv-absent host")
    runner = _pack_sh(_project(tmp_path), tmp_path / "app.sh")
    barren = tmp_path / "home"
    barren.mkdir()
    sh_bin = shutil.which("sh") or "/bin/sh"
    result = subprocess.run(  # noqa: S603 - fixed argv, resolved sh path
        [sh_bin, str(runner), "--pwshpy-no-install-uv"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",  # coreutils present; uv lives elsewhere (~/.local/bin)
            "HOME": str(barren),
            "XDG_BIN_HOME": str(barren / "xdgbin"),
            "CARGO_HOME": str(barren / "cargo"),
            "PWSHPY_PACK_CACHE": str(tmp_path / "cache"),
        },
    )
    assert result.returncode == 127, result.stderr
    assert "uv" in result.stderr


def test_unpack_of_a_freshly_packed_sh_round_trips(tmp_path: Path) -> None:
    manifest = pack_script(_project(tmp_path), tmp_path / "app.sh", options=PackOptions(format=RunnerFormat.SH))
    restored = unpack_script(manifest.output_path, tmp_path / "out")
    assert restored.files == ["app.py", "pkg/__init__.py", "pkg/helper.py"]
    assert (tmp_path / "out" / "pkg" / "helper.py").read_bytes() == (tmp_path / "pkg" / "helper.py").read_bytes()


# --- --pwshpy-elevate ----------------------------------------------------------------------
# A sudo double on PATH stands in for a real password prompt (see elevation_doubles).

_needs_non_root = pytest.mark.skipif(RUNNING_AS_ROOT, reason="the runner skips elevation when already root")

#: What a runner needs on PATH before it reaches the elevate block; sudo deliberately absent.
_PRE_ELEVATE_TOOLS = ("base64", "sha256sum", "shasum", "cut", "id", "tr", "sed", "cat")

_ELEVATE_ENTRY = """import os
import sys

marker = os.environ.get("PWSHPY_TEST_MARKER")
if marker:
    open(marker, "w").close()
print("argv:" + repr(sys.argv[1:]))
sys.exit(9)
"""


def _elevate_setup(tmp_path: Path, *, grant: bool) -> tuple[Path, Path, dict[str, str]]:
    """A packed marker-writing script, a sudo double on PATH, and the marker the script writes.

    The double logs every call to ``sudo-calls.txt`` in ``tmp_path``.
    """
    entry = tmp_path / "who.py"
    entry.write_text(_ELEVATE_ENTRY)
    runner = _pack_sh(entry, tmp_path / "who.sh")
    # Executable, so each test isolates its own defect; the non-executable case clears the bit.
    runner.chmod(0o755)
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    write_fake_sudo(bin_dir, grant=grant, calls=tmp_path / "sudo-calls.txt")
    marker = tmp_path / "ran.marker"
    environment = _env(tmp_path)
    environment["PATH"] = os.pathsep.join([str(bin_dir), environment["PATH"]])
    environment["PWSHPY_TEST_MARKER"] = str(marker)
    return runner, marker, environment


@_needs_non_root
def test_info_with_elevate_prints_the_manifest_and_never_elevates(tmp_path: Path) -> None:
    """--pwshpy-info needs no root: with --pwshpy-elevate it still only prints the manifest."""
    runner, marker, environment = _elevate_setup(tmp_path, grant=True)
    result = _run(["sh"], runner, "--pwshpy-info", "--pwshpy-elevate", env=environment)
    assert result.returncode == 0, result.stderr
    assert "files      :" in result.stdout
    assert not (tmp_path / "sudo-calls.txt").exists(), "sudo was called for --pwshpy-info"
    assert not marker.exists(), "the packed script ran for --pwshpy-info"


@_needs_non_root
def test_elevate_without_sudo_exits_127_not_77(tmp_path: Path) -> None:
    """A host with no sudo is a missing tool (127), not a refused elevation (77)."""
    runner, marker, environment = _elevate_setup(tmp_path, grant=True)
    environment["PATH"] = str(link_tools(tmp_path / "no-sudo-bin", _PRE_ELEVATE_TOOLS))
    sh_bin = shutil.which("sh") or "/bin/sh"
    result = _run([sh_bin], runner, "--pwshpy-elevate", env=environment)
    assert result.returncode == 127, result.stderr
    assert "pwshpy: sudo not found" in result.stderr
    assert not marker.exists()


@_needs_non_root
def test_elevate_refused_exits_77_and_never_runs_the_script(tmp_path: Path) -> None:
    """A refused sudo is exit 77 (EX_NOPERM), told apart from the script's own exit 1."""
    runner, marker, environment = _elevate_setup(tmp_path, grant=False)
    result = _run(["sh"], runner, "--pwshpy-elevate", env=environment)
    assert result.returncode == 77, result.stderr
    assert "pwshpy: elevation declined" in result.stderr
    assert not marker.exists(), "the packed script ran although sudo refused"


@_needs_non_root
def test_elevate_relaunches_a_runner_that_is_not_executable(tmp_path: Path) -> None:
    """The relaunch goes through sh, so a packed file without the exec bit still runs elevated."""
    runner, marker, environment = _elevate_setup(tmp_path, grant=True)
    runner.chmod(0o644)
    result = _run(["sh"], runner, "--pwshpy-elevate", env=environment)
    assert result.returncode == 9, result.stderr
    assert marker.exists()


@_needs_non_root
def test_elevate_keeps_the_double_dash_boundary(tmp_path: Path) -> None:
    """After -- a wrapper-looking argument is the script's own, also after the elevated relaunch."""
    runner, _, environment = _elevate_setup(tmp_path, grant=True)
    result = _run(["sh"], runner, "--pwshpy-elevate", "--", "--pwshpy-help", env=environment)
    assert result.returncode == 9, result.stderr
    assert "argv:['--pwshpy-help']" in result.stdout


@_needs_non_root
def test_elevate_forwards_the_clean_switch(tmp_path: Path) -> None:
    """--pwshpy-clean survives the relaunch: a stray file in the cached tree is gone afterwards."""
    runner, _, environment = _elevate_setup(tmp_path, grant=True)
    assert _run(["sh"], runner, env=environment).returncode == 9
    caches = list((tmp_path / "cache").iterdir())
    assert len(caches) == 1
    # Not a payload file, so the per-file hash check alone never removes it; only a clean does.
    stray = caches[0] / "stray.txt"
    stray.write_text("left over")
    result = _run(["sh"], runner, "--pwshpy-elevate", "--pwshpy-clean", env=environment)
    assert result.returncode == 9, result.stderr
    assert not stray.exists(), "--pwshpy-clean was dropped by the elevated relaunch"


@_needs_non_root
def test_elevate_forwards_the_no_install_uv_switch(tmp_path: Path) -> None:
    """--pwshpy-no-install-uv survives the relaunch: uv absent is exit 127, no installer fetched."""
    runner, _, _ = _elevate_setup(tmp_path, grant=True)
    barren = tmp_path / "home"
    barren.mkdir()
    # The runner probes a fixed system-wide uv whatever PATH says; point that probe into the barren
    # home so the test also runs on a machine that has uv installed there.
    text = runner.read_text(encoding="utf-8")
    assert "/usr/local/bin/uv" in text
    runner.write_text(text.replace("/usr/local/bin/uv", str(barren / "system-uv")), encoding="utf-8", newline="\n")
    bin_dir = tmp_path / "fake-bin"
    fetched = tmp_path / "fetched.marker"
    for tool in ("curl", "wget"):
        double = bin_dir / tool
        double.write_text(f'#!/bin/sh\n: > "{fetched}"\nexit 1\n', encoding="utf-8", newline="\n")
        double.chmod(0o755)
    sh_bin = shutil.which("sh") or "/bin/sh"
    result = subprocess.run(  # noqa: S603 - fixed argv, resolved sh path
        [sh_bin, str(runner), "--pwshpy-elevate", "--pwshpy-no-install-uv"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={
            "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
            "HOME": str(barren),
            "XDG_BIN_HOME": str(barren / "xdgbin"),
            "CARGO_HOME": str(barren / "cargo"),
            "PWSHPY_PACK_CACHE": str(tmp_path / "cache"),
        },
    )
    assert result.returncode == 127, result.stderr
    assert not fetched.exists(), "--pwshpy-no-install-uv was dropped by the elevated relaunch"


# --- which uv a root run trusts ([56]) -------------------------------------------------------
# As root the runner executes the uv it finds with full rights, so it accepts a uv only when root
# owns the file and every directory hop to it and no one else can write them. One counted text
# substitution forces that branch here (this box is not root and sudo is not used; an ``id`` double
# on PATH would not do, because busybox ash runs its own ``id`` applet whatever PATH says), and a
# wrapper uv that names itself shows which uv actually ran.

#: The system-wide uv the runner probes whatever PATH says; a root-owned one is the positive control.
_SYSTEM_WIDE_UV = Path("/usr/local/bin/uv")
#: The line the runner prints for each uv a root run skips.
_UV_SKIPPED = "pwshpy: skipping uv at"
#: The refusal for a pinned uv a root run cannot trust.
_PIN_REFUSED = "pwshpy: refusing the uv given with"

_ENTRY_REPORTING_UV = """import os
import sys

print("uv:" + (os.environ.get("PWSHPY_TEST_UV") or os.environ.get("UV") or ""))
sys.exit(0)
"""


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


_needs_trusted_system_uv = pytest.mark.skipif(
    not (_SYSTEM_WIDE_UV.is_file() and _root_only_writable(_SYSTEM_WIDE_UV)),
    reason=f"needs a root-owned {_SYSTEM_WIDE_UV} that only root can modify",
)


def _planted_uv(tmp_path: Path, name: str = "planted-bin") -> Path:
    """A world-writable directory holding a uv wrapper that names itself, then runs the real uv."""
    directory = tmp_path / name
    directory.mkdir()
    directory.chmod(0o777)
    wrapper = directory / "uv"
    wrapper.write_text(
        f'#!/bin/sh\nPWSHPY_TEST_UV="$0"\nexport PWSHPY_TEST_UV\nexec "{_UV}" "$@"\n', encoding="utf-8", newline="\n"
    )
    wrapper.chmod(0o755)
    return wrapper


#: The runner's root probe for the trust rule, and what a forced-root runner has in its place.
_ROOT_PROBE = ('if [ "$(pwshpy_euid)" = "0" ]; then\n    PWSHPY_AS_ROOT=1', "if true; then\n    PWSHPY_AS_ROOT=1")
#: The runner's trusted owners (root alone), and the list a test widens it to so its own files can
#: stand in for root-owned ones: no unprivileged test can create those.
_TRUSTED_UIDS = "PWSHPY_TRUSTED_UIDS=0\n"


def _trust_runner(tmp_path: Path, *, hide_system_uv: bool, as_root: bool = False, trust_my_uid: bool = False) -> Path:
    """A packed which-uv script, its fixed /usr/local/bin/uv probe hidden, its root probe forced.

    ``trust_my_uid`` also counts this test's own uid as root for ownership, so a directory layout
    the test builds can pass or fail on everything BUT its owner (group, other, sticky, ACL,
    ancestors). All are exact, counted substitutions; the rest is the shipped runner.
    """
    entry = tmp_path / "which_uv.py"
    entry.write_text(_ENTRY_REPORTING_UV)
    runner = _pack_sh(entry, tmp_path / "which_uv.sh")
    text = runner.read_text(encoding="utf-8")
    if hide_system_uv:
        assert text.count(str(_SYSTEM_WIDE_UV)) == 1
        text = text.replace(str(_SYSTEM_WIDE_UV), str(tmp_path / "no-system-uv"))
    if as_root:
        assert text.count(_ROOT_PROBE[0]) == 1
        text = text.replace(*_ROOT_PROBE)
    if trust_my_uid:
        assert text.count(_TRUSTED_UIDS) == 1
        text = text.replace(_TRUSTED_UIDS, f"PWSHPY_TRUSTED_UIDS='0 {tmp_path.stat().st_uid}'\n")
    runner.write_text(text, encoding="utf-8", newline="\n")
    return runner


def _trust_env(tmp_path: Path, *path_first: Path) -> dict[str, str]:
    """An environment whose PATH is ``path_first`` plus the system dirs, with a barren HOME.

    Python downloads are off, so a root run (which takes only a uv-managed Python) never reaches
    the network: it uses the host's managed Pythons, read only, or fails when there are none.
    """
    barren = tmp_path / "home"
    barren.mkdir(exist_ok=True)
    environment = {
        "PATH": os.pathsep.join([*map(str, path_first), "/usr/bin", "/bin"]),
        "HOME": str(barren),
        "XDG_BIN_HOME": str(barren / "xdgbin"),
        "CARGO_HOME": str(barren / "cargo"),
        "PWSHPY_PACK_CACHE": str(tmp_path / "cache"),
        "PYTHONIOENCODING": "utf-8",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    managed = managed_python_dir()
    if managed is not None:
        environment["UV_PYTHON_INSTALL_DIR"] = str(managed)
    return environment


def _reported_uv(result: subprocess.CompletedProcess[str]) -> str | None:
    for line in result.stdout.splitlines():
        if line.startswith("uv:") and line[3:]:
            return line[3:].strip()
    return None


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_a_writable_uv_first_on_path_is_skipped(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """[56]: a uv planted first on PATH in a world-writable directory never runs as root."""
    planted = _planted_uv(tmp_path)
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True)
    environment = _trust_env(tmp_path, planted.parent)
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=environment)
    assert _reported_uv(result) is None, result.stdout + result.stderr
    assert result.returncode == 127, result.stdout + result.stderr
    assert f"{_UV_SKIPPED} {planted}" in result.stderr, result.stderr


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_each_skipped_uv_names_its_reason(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """Every PATH hit is judged and named: a world-writable directory, then one this uid owns.

    Pins the parsed owner too: a reason with an empty uid would mean the listing was misread.
    """
    planted = _planted_uv(tmp_path)
    owned = tmp_path / "owned-bin"
    owned.mkdir()
    owned.chmod(0o755)
    owned_uv = owned / "uv"
    owned_uv.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8", newline="\n")
    owned_uv.chmod(0o755)
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True)
    environment = _trust_env(tmp_path, planted.parent, owned)
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=environment)
    assert result.returncode == 127, result.stdout + result.stderr
    assert f"{_UV_SKIPPED} {planted}: its directory {planted.parent} is writable by every account" in result.stderr
    owner = owned.stat().st_uid
    assert f"{_UV_SKIPPED} {owned_uv}: its directory {owned} is owned by uid {owner}, not root" in result.stderr


@_needs_trusted_system_uv
@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_a_root_owned_uv_still_runs(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """The trusted fallback: after skipping the planted one, the root-owned system uv runs."""
    planted = _planted_uv(tmp_path)
    runner = _trust_runner(tmp_path, hide_system_uv=False, as_root=True)
    environment = _trust_env(tmp_path, planted.parent)
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _reported_uv(result) == str(_SYSTEM_WIDE_UV), result.stdout + result.stderr
    assert f"{_UV_SKIPPED} {planted}" in result.stderr, result.stderr


def test_not_root_the_first_uv_on_path_still_runs(tmp_path: Path) -> None:
    """Control: without root the lookup is unchanged - the first uv on PATH runs, trusted or not."""
    planted = _planted_uv(tmp_path)
    runner = _trust_runner(tmp_path, hide_system_uv=True)
    result = _run(["sh"], runner, env=_trust_env(tmp_path, planted.parent))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _reported_uv(result) == str(planted), result.stdout + result.stderr
    assert _UV_SKIPPED not in result.stderr, result.stderr


def test_not_root_the_pinned_uv_is_used_as_is(tmp_path: Path) -> None:
    """--pwshpy-uv names the uv outright: without root it is taken as given, ahead of PATH."""
    planted = _planted_uv(tmp_path)
    runner = _trust_runner(tmp_path, hide_system_uv=True)
    environment = _trust_env(tmp_path, Path(_UV or "uv").parent)
    result = _run(["sh"], runner, "--pwshpy-uv", str(planted), env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _reported_uv(result) == str(planted), result.stdout + result.stderr


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_an_untrusted_pinned_uv_is_refused(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """As root a pin anyone can modify is refused with exit 127, and nothing else is tried."""
    planted = _planted_uv(tmp_path)
    runner = _trust_runner(tmp_path, hide_system_uv=False, as_root=True)
    environment = _trust_env(tmp_path)
    result = _run(shell, runner, "--pwshpy-uv", str(planted), env=environment)
    assert result.returncode == 127, result.stdout + result.stderr
    assert f"{_PIN_REFUSED} --pwshpy-uv: {planted}" in result.stderr, result.stderr
    assert _reported_uv(result) is None, result.stdout


@_needs_trusted_system_uv
def test_as_root_a_trusted_pinned_uv_runs(tmp_path: Path) -> None:
    """As root a root-owned pin is used, even with a planted uv first on PATH."""
    planted = _planted_uv(tmp_path)
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True)
    environment = _trust_env(tmp_path, planted.parent)
    result = _run(["sh"], runner, "--pwshpy-uv", str(_SYSTEM_WIDE_UV), env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _reported_uv(result) == str(_SYSTEM_WIDE_UV), result.stdout + result.stderr


@pytest.mark.skipif(
    not (Path("/usr/bin/python3").is_symlink() and _root_only_writable(Path("/usr/bin/python3").resolve())),
    reason="needs /usr/bin/python3 as a root-owned symlink to a root-owned interpreter",
)
def test_as_root_a_trusted_symlinked_pin_is_followed_and_accepted(tmp_path: Path) -> None:
    """A root-owned symlink in a root-owned dir is resolved hop by hop and accepted.

    No unprivileged test can create such a link, so a system one stands in for uv: the runner
    accepts it and execs it as ``python3 run --no-project ...``, which fails on the missing file
    ``run`` - proof the link passed the trust rule and was the program started.
    """
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True)
    environment = _trust_env(tmp_path)
    result = _run(["sh"], runner, "--pwshpy-no-install-uv", "--pwshpy-uv", "/usr/bin/python3", env=environment)
    assert _PIN_REFUSED not in result.stderr, result.stderr
    assert result.returncode == 2, result.stdout + result.stderr
    assert "can't open file" in result.stderr, result.stderr


@pytest.mark.parametrize(
    "arguments",
    [pytest.param(["--pwshpy-uv", "relative/uv"], id="relative"), pytest.param(["--pwshpy-uv"], id="missing")],
)
def test_a_pinned_uv_must_be_an_absolute_path(tmp_path: Path, arguments: list[str]) -> None:
    """A relative or missing --pwshpy-uv value is a usage error (64), before anything is unpacked."""
    runner = _trust_runner(tmp_path, hide_system_uv=True)
    result = _run(["sh"], runner, *arguments, env=_trust_env(tmp_path, Path(_UV or "uv").parent))
    assert result.returncode == 64, result.stdout + result.stderr
    assert "--pwshpy-uv" in result.stderr, result.stderr
    assert not (tmp_path / "cache").exists(), "the payload was unpacked for a malformed switch"


@_needs_non_root
def test_elevate_forwards_the_pinned_uv(tmp_path: Path) -> None:
    """--pwshpy-uv survives the sudo relaunch, a path with a space included."""
    planted = _planted_uv(tmp_path, name="planted bin")
    runner = _trust_runner(tmp_path, hide_system_uv=True)
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    write_fake_sudo(bin_dir, grant=True, calls=tmp_path / "sudo-calls.txt")
    environment = _trust_env(tmp_path, bin_dir, Path(_UV or "uv").parent)
    result = _run(["sh"], runner, "--pwshpy-elevate", "--pwshpy-uv", str(planted), "--", "a b", env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _reported_uv(result) == str(planted), result.stdout + result.stderr
    assert (tmp_path / "sudo-calls.txt").exists(), "the runner never relaunched through sudo"


# --- root runs: helpers, ancestors, ACLs, group write (fix round 2) ----------------------------


def _marker_tool(directory: Path, name: str, marker: Path, *, body: str) -> None:
    """A tool double that records it ran (appends its name to ``marker``) and then does ``body``."""
    tool = directory / name
    tool.write_text(f'#!/bin/sh\necho {name} >> "{marker}"\n{body}\n', encoding="utf-8", newline="\n")
    tool.chmod(0o755)


def _real(name: str) -> str:
    found = shutil.which(name, path="/usr/bin:/bin")
    assert found is not None, name
    return found


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_planted_helpers_on_path_never_run(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """[87]: as root the runner runs ls, readlink, tar, base64 only from the fixed system dirs.

    A world-writable directory first on PATH holds a uv (a symlink, so readlink is asked), an ls
    that lists every path as root-owned 0755, and recording tar/base64/readlink doubles. None may
    run, and the uv stays refused. (busybox ash runs its own applets whatever PATH says, so it
    passes this either way; sh, dash and bash are the ones the fix is for.)
    """
    planted = _planted_uv(tmp_path)
    (planted.parent / "uv-real").write_bytes(planted.read_bytes())
    (planted.parent / "uv-real").chmod(0o755)
    planted.unlink()
    planted.symlink_to("uv-real")
    marker = tmp_path / "ran.txt"
    _marker_tool(planted.parent, "ls", marker, body="echo 'drwxr-xr-x 2 0 0 4096 Jan  1 00:00 x'")
    for name in ("tar", "base64", "readlink"):
        _marker_tool(planted.parent, name, marker, body=f'exec "{_real(name)}" "$@"')
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True)
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, planted.parent))
    assert not marker.exists(), f"planted helpers ran as root: {marker.read_text()}"
    assert _reported_uv(result) is None, result.stdout + result.stderr
    assert result.returncode == 127, result.stdout + result.stderr
    # The link is this user's, which the walk meets (and refuses) before the world-writable directory.
    assert (
        f"{_UV_SKIPPED} {planted}: the symlink {planted} is owned by uid {planted.lstat().st_uid}, not root"
        in result.stderr
    )


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_the_root_probe_never_asks_a_planted_id(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """A planted id first on PATH is never asked: answering "not root" would switch the rule off."""
    marker = tmp_path / "ran.txt"
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    _marker_tool(fake, "id", marker, body="echo 0")
    runner = _trust_runner(tmp_path, hide_system_uv=True)
    result = _run(shell, runner, env=_trust_env(tmp_path, fake, Path(_UV or "uv").parent))
    assert not marker.exists(), "the runner asked the planted id"
    assert result.returncode == 0, result.stdout + result.stderr
    assert _UV_SKIPPED not in result.stderr, result.stderr


def _own_tree(tmp_path: Path, *, ancestor_mode: int, leaf_mode: int = 0o755) -> Path:
    """tmp/anc (``ancestor_mode``) / bin (``leaf_mode``) / uv - all owned by this test's uid."""
    ancestor = tmp_path / "anc"
    ancestor.mkdir()
    leaf = ancestor / "bin"
    leaf.mkdir()
    uv = _uv_stand_in_at(leaf)
    leaf.chmod(leaf_mode)
    ancestor.chmod(ancestor_mode)
    return uv


def _uv_stand_in_at(directory: Path) -> Path:
    wrapper = directory / "uv"
    wrapper.write_text(
        f'#!/bin/sh\nPWSHPY_TEST_UV="$0"\nexport PWSHPY_TEST_UV\nexec "{_UV}" "$@"\n', encoding="utf-8", newline="\n"
    )
    wrapper.chmod(0o755)
    return wrapper


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize(
    ("ancestor_mode", "reason"),
    [
        pytest.param(0o777, "is writable by every account and not sticky", id="other-write"),
        pytest.param(0o775, "is writable by group {gid} and not sticky", id="group-write"),
    ],
)
def test_as_root_a_swappable_ancestor_refuses_the_uv(
    tmp_path: Path, shell_id: str, shell: list[str], ancestor_mode: int, reason: str
) -> None:
    """A locked leaf under an ancestor others can write (no sticky bit) is refused: they could rename
    the leaf away and put another in its place between the check and the exec."""
    uv = _own_tree(tmp_path, ancestor_mode=ancestor_mode)
    ancestor = uv.parent.parent
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True, trust_my_uid=True)
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, uv.parent))
    assert result.returncode == 127, result.stdout + result.stderr
    expected = f"{_UV_SKIPPED} {uv}: its ancestor {ancestor} " + reason.format(gid=ancestor.stat().st_gid)
    assert expected in result.stderr, result.stderr


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize("ancestor_mode", [0o755, 0o1777], ids=["locked", "sticky-world-writable"])
def test_as_root_a_uv_under_trusted_ancestors_runs(
    tmp_path: Path, shell_id: str, shell: list[str], ancestor_mode: int
) -> None:
    """Positive control for the PATH search: a locked leaf under locked or sticky ancestors runs."""
    uv = _own_tree(tmp_path, ancestor_mode=ancestor_mode)
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True, trust_my_uid=True)
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, uv.parent))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _reported_uv(result) == str(uv), result.stdout + result.stderr
    assert _UV_SKIPPED not in result.stderr, result.stderr


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_a_symlinked_path_directory_is_resolved(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """A PATH entry that is a directory symlink is followed, not judged by the link's own mode."""
    uv = _own_tree(tmp_path, ancestor_mode=0o755)
    link = tmp_path / "linked-bin"
    link.symlink_to(uv.parent)
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True, trust_my_uid=True)
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, link))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _reported_uv(result) == str(link / "uv"), result.stdout + result.stderr


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_group_write_is_refused_for_any_group(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """Group write on the directory holding uv is refused, whatever the group (here this user's)."""
    uv = _own_tree(tmp_path, ancestor_mode=0o755, leaf_mode=0o775)
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True, trust_my_uid=True)
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, uv.parent))
    assert result.returncode == 127, result.stdout + result.stderr
    gid = uv.parent.stat().st_gid
    assert f"{_UV_SKIPPED} {uv}: its directory {uv.parent} is writable by group {gid}" in result.stderr


_GROUP0_WRITABLE = Path("/usr/local/bin")


@pytest.mark.skipif(
    not (
        _SYSTEM_WIDE_UV.is_file()
        and _GROUP0_WRITABLE.stat().st_uid == 0
        and _GROUP0_WRITABLE.stat().st_gid == 0
        and _GROUP0_WRITABLE.stat().st_mode & 0o020
    ),
    reason=f"needs {_SYSTEM_WIDE_UV} in a root:root group-writable {_GROUP0_WRITABLE}",
)
@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_group_write_by_group_0_is_refused(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """Group 0 gets no allowance any more: members of a root group are not all root."""
    runner = _trust_runner(tmp_path, hide_system_uv=False, as_root=True)
    result = _run(shell, runner, "--pwshpy-uv", str(_SYSTEM_WIDE_UV), env=_trust_env(tmp_path))
    assert result.returncode == 127, result.stdout + result.stderr
    assert f"its directory {_GROUP0_WRITABLE} is writable by group 0" in result.stderr, result.stderr


def _acl_dir(path: Path) -> Path | None:
    """A 0755 directory of this user's with a named-user write ACL, or None where ACLs are unsupported."""
    path.mkdir()
    path.chmod(0o755)
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        return None
    done = subprocess.run([setfacl, "-m", "u:65534:rwx", str(path)], capture_output=True, check=False)  # noqa: S603
    return path if done.returncode == 0 else None


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_a_directory_with_a_write_acl_is_refused(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """A named-user write ACL refuses the uv: as '+' where ls shows it, as the mask's group write on busybox."""
    acl = _acl_dir(tmp_path / "acl-bin")
    if acl is None:
        pytest.skip("setfacl is missing or this filesystem has no POSIX ACLs")
    uv = _uv_stand_in_at(acl)
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True, trust_my_uid=True)
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, acl))
    assert result.returncode == 127, result.stdout + result.stderr
    reasons = (f"its directory {acl} carries an ACL", f"its directory {acl} is writable by group {acl.stat().st_gid}")
    line = next((x for x in result.stderr.splitlines() if x.startswith(f"{_UV_SKIPPED} {uv}:")), "")
    if shell_id == "busybox-ash":
        assert any(reason in line for reason in reasons), result.stderr
    else:
        assert reasons[0] in line, result.stderr


@pytest.mark.skipif(
    not (Path("/bin").is_symlink() and Path("/usr/bin/python3").exists()),
    reason="needs a merged-/usr host (/bin -> usr/bin) with /usr/bin/python3",
)
@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_a_pin_through_merged_usr_is_accepted(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """/bin/python3 passes the /bin -> usr/bin link and python3's own link and is accepted, then run.

    python3 stands in for uv and fails on the file ``run``; the refusal message must be absent.
    """
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True)
    result = _run(shell, runner, "--pwshpy-no-install-uv", "--pwshpy-uv", "/bin/python3", env=_trust_env(tmp_path))
    assert _PIN_REFUSED not in result.stderr, result.stderr
    assert result.returncode == 2, result.stdout + result.stderr
    assert "can't open file" in result.stderr, result.stderr


# --- the euid probe fails closed (fix round 3) ----------------------------------------------------

#: The runner's fixed id locations, and what a test puts in their place to simulate a system with
#: id at none of them (one counted substitution).
_FIXED_IDS = (
    "/usr/bin/id /bin/id /usr/sbin/id /sbin/id /run/current-system/sw/bin/id \\\n"
    "        /run/current-system/profile/bin/id"
)


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize("probe", ["planted-id-on-path", "no-id", "id-answers-garbage"])
def test_an_unanswered_euid_probe_applies_the_root_rule(
    tmp_path: Path, shell_id: str, shell: list[str], probe: str
) -> None:
    """With no id at a fixed path that answers a decimal uid, the run is treated as root.

    Never a PATH lookup: a planted id answering 1000 would turn a root run into "not root" and
    switch the trust rule (and the fixed helper PATH) off. Treated as root, the planted uv first on
    PATH is skipped - the strict rule applies - and the planted id never runs.
    """
    planted = _planted_uv(tmp_path)
    marker = tmp_path / "ran.txt"
    _marker_tool(planted.parent, "id", marker, body="echo 1000")
    fixed = tmp_path / "fixed"
    fixed.mkdir()
    replacement = " ".join(str(fixed / f"id{n}") for n in range(6))
    if probe == "id-answers-garbage":
        _marker_tool(fixed, "id0", tmp_path / "garbage.txt", body="echo not-a-uid")
    runner = _trust_runner(tmp_path, hide_system_uv=True)
    text = runner.read_text(encoding="utf-8")
    assert text.count(_FIXED_IDS) == 1
    runner.write_text(text.replace(_FIXED_IDS, replacement), encoding="utf-8", newline="\n")
    environment = _trust_env(tmp_path, planted.parent)
    if probe == "no-id":
        # No id anywhere the runner could reach: the planted one goes, and PATH holds only the
        # planted directory plus the helpers the runner needs before it resets PATH.
        (planted.parent / "id").unlink()
        helpers = link_tools(tmp_path / "no-id-bin", ("base64", "sha256sum", "cut", "tr", "sed", "cat"))
        environment["PATH"] = os.pathsep.join([str(planted.parent), str(helpers)])
    # The shell by full path: the test's own PATH may no longer hold it.
    shell = [shutil.which(shell[0]) or shell[0], *shell[1:]]
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=environment)
    assert not marker.exists(), "the runner asked an id on PATH"
    assert _reported_uv(result) is None, result.stdout + result.stderr
    assert f"{_UV_SKIPPED} {planted}: its directory {planted.parent} is writable by every account" in result.stderr


#: The PATH a root run gives its own helpers (user decision): system directories only, no /usr/local.
_ROOT_HELPER_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/run/current-system/sw/bin:/run/current-system/profile/bin"


def test_the_root_helper_path_is_system_dirs_only(tmp_path: Path) -> None:
    """The fixed helper PATH is exactly the decided list: /usr/local is not trusted for root's helpers."""
    runner = _trust_runner(tmp_path, hide_system_uv=False)
    text = runner.read_text(encoding="utf-8")
    assert text.count(f"PATH={_ROOT_HELPER_PATH}\n") == 1
    assert "/usr/local/sbin:" not in text
    assert "/usr/local/bin:" not in text


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_a_helper_in_usr_local_bin_is_not_used(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """A tar/ls/base64 in /usr/local/bin is not root's helper. A temp dir stands in for /usr/local/bin.

    It sits first on the caller's PATH, as /usr/local/bin does on most hosts; the root run resets
    PATH to the system directories, so none of its tools runs (busybox ash runs its own applets
    either way).
    """
    usr_local_bin = tmp_path / "usr-local-bin"
    usr_local_bin.mkdir()
    marker = tmp_path / "ran.txt"
    _marker_tool(usr_local_bin, "ls", marker, body="echo 'drwxr-xr-x 2 0 0 4096 Jan  1 00:00 x'")
    for name in ("tar", "base64", "sha256sum", "cut", "sed"):
        _marker_tool(usr_local_bin, name, marker, body=f'exec "{_real(name)}" "$@"')
    planted = _planted_uv(tmp_path)
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True)
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, usr_local_bin, planted.parent))
    assert not marker.exists(), f"helpers from the /usr/local/bin stand-in ran as root: {marker.read_text()}"
    assert result.returncode == 127, result.stdout + result.stderr
    assert f"{_UV_SKIPPED} {planted}: its directory {planted.parent} is writable by every account" in result.stderr


# --- [90] the .sh takes every value literally ---------------------------------------------------------


def _work_dir(tmp_path: Path) -> Path:
    """An empty working directory: uv queries a .venv in the cwd or above it, which would answer
    before the PATH python these tests plant."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return work


def _run_in(
    shell: list[str], runner: Path, *args: str, env: dict[str, str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv from resolved shell + our own artefact
        [*shell, str(runner), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
        env=env,
        cwd=cwd,
    )


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize(
    "spec", ["pkg$HOME", "pkg`echo x`", 'pkg"q', "pkg[ab]"], ids=["dollar", "backtick", "double-quote", "glob"]
)
def test_a_uv_arg_is_never_expanded_by_the_runner(tmp_path: Path, shell_id: str, shell: list[str], spec: str) -> None:
    """[90]: a --with spec reaches uv exactly as packed: the runner once held the list in double quotes."""
    entry = tmp_path / "app.py"
    entry.write_text("print('never run: uv is a recording double')\n")
    runner = _pack_sh(entry, tmp_path / "app.sh", with_packages=[spec])
    record = tmp_path / "argv.txt"
    uv = write_recording_uv(tmp_path, record)
    work = _work_dir(tmp_path)
    (work / "pkga").write_text("bait\n", encoding="utf-8")
    result = _run_in(shell, runner, "--pwshpy-uv", str(uv), env=_env(tmp_path), cwd=work)
    assert result.returncode == 0, result.stdout + result.stderr
    argv = record.read_text(encoding="utf-8").splitlines()
    assert argv[2:4] == ["--with", spec], argv


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_a_packed_file_name_with_wildcards_is_verified_literally(
    tmp_path: Path, shell_id: str, shell: list[str]
) -> None:
    """[90]: the hash list is never globbed, so a cwd file a 'd[1].json' line matches changes nothing."""
    entry = _project(tmp_path, exit_code=3)
    data = tmp_path / "d[1].json"
    data.write_text("{}\n", encoding="utf-8")
    runner = _pack_sh(entry, tmp_path / "app.sh", include=[str(data)])
    digest = hashlib.sha256(data.read_bytes()).hexdigest()
    work = _work_dir(tmp_path)
    (work / f"{digest}  d1.json").write_text("bait\n", encoding="utf-8")
    result = _run_in(shell, runner, env=_env(tmp_path), cwd=work)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "greeting:hello from the submodule" in result.stdout
