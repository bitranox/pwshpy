"""End-to-end: pack a script to a POSIX ``.sh`` and RUN it under every available major shell.

The `.sh` runner is strict POSIX, so it must behave identically under whatever ``/bin/sh`` is -
dash, busybox ash, bash-as-sh, zsh - not only bash.  Each installed interpreter runs the SAME
artefact and must produce the same result: the payload unpacks, uv resolves the PEP 723 deps, the
local submodule imports, arguments arrive intact, and the script's exit code comes back.

``local_only``: needs a POSIX shell + uv, and the dependency cases reach PyPI.  Skips cleanly on
Windows (no POSIX ``sh``) and when uv is absent.

Root-safe ([92]): every run pins ``UV_PYTHON_DOWNLOADS=never``, so no test downloads a Python into
the real ``~/.local/share/uv/python`` of whoever runs the suite. A test about what a run that is NOT
root does gets a runner whose root probe answers "not root" when the suite itself runs as root (one
counted substitution, the mirror of the forced-root stand-in), so it tests the same path on every
host; a root run that executes the script needs a uv-managed Python and skips without one.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from elevation_doubles import (
    LOCAL_WHEEL_DISTRIBUTION,
    MANAGED_PYTHON_VARIABLES,
    RUNNING_AS_ROOT,
    link_tools,
    managed_python_env,
    owned_by_another_account,
    planted_managed_python,
    session_uv_cache,
    trusted_managed_python,
    with_data_home,
    without_python_dir_check,
    write_fake_sudo,
    write_local_wheel,
    write_planted_environment,
    write_planted_python,
    write_planted_venv,
    write_recording_uv,
)

from pwshpy.adapters.native.packer import pack_script, unpack_script
from pwshpy.domain.enums import RunnerFormat
from pwshpy.domain.packing import SHIM_MODULE, PackOptions

if sys.platform != "win32":
    import pwd

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


#: The runner's root probe, what a forced-root stand-in has in its place, and what a stand-in for a
#: run that is not root has (used when the suite itself runs as root).
_ROOT_PROBE = ('if [ "$(pwshpy_euid)" = "0" ]; then\n    PWSHPY_AS_ROOT=1', "if true; then\n    PWSHPY_AS_ROOT=1")
_NOT_ROOT_PROBE = "if false; then\n    PWSHPY_AS_ROOT=1"


def _force_probe(runner: Path, *, as_root: bool | None, real_data_home: bool = False) -> None:
    """Make ``runner``'s root probe answer ``as_root`` (one counted substitution); None keeps the real one.

    False only changes anything when the suite runs as root, where the real probe would say root.
    A runner forced to root also takes ``data_home(runner.parent)`` for its account's data directory
    (see :func:`elevation_doubles.with_data_home`) unless ``real_data_home`` keeps the passwd lookup;
    that real directory is then taken as trusted (:func:`elevation_doubles.without_python_dir_check`).
    """
    if as_root is None or (not as_root and not RUNNING_AS_ROOT):
        return
    text = runner.read_text(encoding="utf-8")
    assert text.count(_ROOT_PROBE[0]) == 1
    text = text.replace(_ROOT_PROBE[0], _ROOT_PROBE[1] if as_root else _NOT_ROOT_PROBE)
    if as_root and not real_data_home:
        text = with_data_home(text, runner.parent, powershell=False)
    elif as_root:
        text = without_python_dir_check(text, powershell=False)
    runner.write_text(text, encoding="utf-8")


def _pack_sh(
    entry: Path, dest: Path, *, as_root: bool | None = False, real_data_home: bool = False, **opts: object
) -> Path:
    """Pack ``entry`` to a .sh runner whose root probe answers ``as_root`` (see :func:`_force_probe`)."""
    manifest = pack_script(entry, dest, options=PackOptions(format=RunnerFormat.SH, **opts))  # type: ignore[arg-type]
    runner = Path(manifest.output_path)
    _force_probe(runner, as_root=as_root, real_data_home=real_data_home)
    return runner


def _env(tmp_path: Path) -> dict[str, str]:
    child = {**os.environ, "PYTHONIOENCODING": "utf-8", "PWSHPY_PACK_CACHE": str(tmp_path / "cache")}
    # Pin the uv the test found so a blank PATH never exercises the network installer.
    child["PATH"] = os.pathsep.join([str(Path(_UV or "uv").parent), child.get("PATH", "")])
    # Never a Python download into a real user directory ([92]): uv uses what is installed.
    child["UV_PYTHON_DOWNLOADS"] = "never"
    # Nor a script environment in the real user's uv cache ([93]).
    child["UV_CACHE_DIR"] = str(session_uv_cache())
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


#: The runner's trusted owners (root alone), and the list a test widens it to so its own files can
#: stand in for root-owned ones: no unprivileged test can create those.
_TRUSTED_UIDS = "PWSHPY_TRUSTED_UIDS=0\n"


def _trust_runner(
    tmp_path: Path, *, hide_system_uv: bool, as_root: bool | None = False, trust_my_uid: bool = False
) -> Path:
    """A packed which-uv script, its fixed /usr/local/bin/uv probe hidden, its root probe forced.

    ``as_root`` as for :func:`_force_probe` (None keeps the real probe, for the tests of the probe
    itself). ``trust_my_uid`` also counts this test's own uid as root for ownership, so a directory
    layout the test builds can pass or fail on everything BUT its owner (group, other, sticky, ACL,
    ancestors). All are exact, counted substitutions; the rest is the shipped runner.
    """
    entry = tmp_path / "which_uv.py"
    entry.write_text(_ENTRY_REPORTING_UV)
    runner = _pack_sh(entry, tmp_path / "which_uv.sh", as_root=as_root)
    text = runner.read_text(encoding="utf-8")
    if hide_system_uv:
        assert text.count(str(_SYSTEM_WIDE_UV)) == 1
        text = text.replace(str(_SYSTEM_WIDE_UV), str(tmp_path / "no-system-uv"))
    if trust_my_uid:
        assert text.count(_TRUSTED_UIDS) == 1
        text = text.replace(_TRUSTED_UIDS, f"PWSHPY_TRUSTED_UIDS='0 {tmp_path.stat().st_uid}'\n")
    runner.write_text(text, encoding="utf-8", newline="\n")
    return runner


def _trust_env(tmp_path: Path, *path_first: Path, no_managed: bool = False) -> dict[str, str]:
    """An environment whose PATH is ``path_first`` plus the system dirs, with a barren HOME.

    Python downloads are off, so a root run (which takes only a uv-managed Python) never reaches
    the network: it uses the host's managed Pythons, read only, or fails when there are none (or
    when ``no_managed`` hides them).
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
    managed = None if no_managed else trusted_managed_python()
    environment.update(managed_python_env(tmp_path, managed=managed))
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
    owned_by_another_account(owned)
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
    _needs_managed_python()
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
    _needs_managed_python()
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
    ``run`` - proof the link passed the trust rule and was the program started. This test's own
    managed-Python directory is not root's, so its check is taken as passed (the pin is the subject).
    """
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True)
    runner.write_text(without_python_dir_check(runner.read_text(encoding="utf-8"), powershell=False), encoding="utf-8")
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
    owned_by_another_account(planted)
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


@pytest.mark.skipif(
    RUNNING_AS_ROOT, reason="as root the real id answers 0, so a planted id answering 0 changes nothing to observe"
)
@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_the_root_probe_never_asks_a_planted_id(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """A planted id first on PATH is never asked: answering "not root" would switch the rule off."""
    marker = tmp_path / "ran.txt"
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    _marker_tool(fake, "id", marker, body="echo 0")
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=None)
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
    _needs_managed_python()
    uv = _own_tree(tmp_path, ancestor_mode=ancestor_mode)
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True, trust_my_uid=True)
    result = _run(shell, runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, uv.parent))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _reported_uv(result) == str(uv), result.stdout + result.stderr
    assert _UV_SKIPPED not in result.stderr, result.stderr


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_a_symlinked_path_directory_is_resolved(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """A PATH entry that is a directory symlink is followed, not judged by the link's own mode."""
    _needs_managed_python()
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

    python3 stands in for uv and fails on the file ``run``; the refusal message must be absent. This
    test's own managed-Python directory is not root's, so its check is taken as passed.
    """
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=True)
    runner.write_text(without_python_dir_check(runner.read_text(encoding="utf-8"), powershell=False), encoding="utf-8")
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
    runner = _trust_runner(tmp_path, hide_system_uv=True, as_root=None)
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


# --- [89] as root only a uv-managed Python runs ------------------------------------------------------
# uv picks the interpreter, and EXECUTES every candidate it considers (it asks each for its version),
# so a python planted in a PATH directory another account can write would run as root. As root the
# runner passes --python-preference only-managed, moves uv's project discovery to /, and drops the
# variables that name an interpreter outright. Downloads are off in every run (_trust_env), so nothing
# reaches the network.

_ENTRY_REPORTING_PYTHON = """import sys

print("python:" + sys.executable)
"""
#: uv's refusal when `uv python find --system --python-preference only-managed` finds nothing, which an
#: elevated run passes on (measured with uv 0.13.0).
_NO_MANAGED_PYTHON = "No interpreter found in managed installations"


def _python_runner(
    tmp_path: Path,
    *,
    as_root: bool,
    source: str = _ENTRY_REPORTING_PYTHON,
    with_packages: Sequence[str] = (),
    real_data_home: bool = False,
    entry_name: str = "which_python.py",
) -> tuple[Path, Path]:
    """A which-python .sh runner (root probe forced when ``as_root``) and a uv it trusts, in a tree this
    test owns that counts as root's. ``source`` replaces the packed script, ``entry_name`` names it;
    ``real_data_home`` as for :func:`_force_probe`."""
    entry = tmp_path / entry_name
    entry.write_text(source)
    runner = _pack_sh(
        entry,
        tmp_path / "which_python.sh",
        as_root=as_root,
        with_packages=list(with_packages),
        real_data_home=real_data_home,
    )
    text = runner.read_text(encoding="utf-8")
    assert text.count(str(_SYSTEM_WIDE_UV)) == 1
    text = text.replace(str(_SYSTEM_WIDE_UV), str(tmp_path / "no-system-uv"))
    assert text.count(_TRUSTED_UIDS) == 1
    text = text.replace(_TRUSTED_UIDS, f"PWSHPY_TRUSTED_UIDS='0 {tmp_path.stat().st_uid}'\n")
    runner.write_text(text, encoding="utf-8", newline="\n")
    return runner, _own_tree(tmp_path, ancestor_mode=0o755)


def _reported_python(result: subprocess.CompletedProcess[str]) -> str | None:
    for line in result.stdout.splitlines():
        if line.startswith("python:") and line[7:]:
            return line[7:].strip()
    return None


def _needs_managed_python() -> Path:
    managed = trusted_managed_python()
    if managed is None:
        pytest.skip("this host has no uv-managed Python (and the test must not download one)")
    return managed


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize(("as_root", "planted_runs"), [(True, False), (False, True)], ids=["root", "not-root"])
def test_as_root_a_python_planted_first_on_path_never_runs(
    tmp_path: Path, shell_id: str, shell: list[str], as_root: bool, planted_runs: bool
) -> None:
    """[89]: with no managed Python, a root run refuses rather than run the PATH python; not root
    (the control), uv runs the planted one."""
    runner, uv = _python_runner(tmp_path, as_root=as_root)
    marker = tmp_path / "planted-ran.txt"
    planted = write_planted_python(tmp_path / "planted-bin", marker)
    environment = _trust_env(tmp_path, uv.parent, planted.parent, no_managed=True)
    result = _run_in(shell, runner, "--pwshpy-no-install-uv", env=environment, cwd=_work_dir(tmp_path))
    if planted_runs:
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.is_file(), "the control never ran the planted python"
        return
    assert not marker.exists(), "the root run executed the planted python: " + marker.read_text()
    assert _reported_python(result) is None, result.stdout
    assert _NO_MANAGED_PYTHON in result.stderr, result.stderr


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_an_installed_managed_python_runs(tmp_path: Path, shell_id: str, shell: list[str]) -> None:
    """[89]: the positive control - with a managed Python installed, the root run runs on it."""
    managed = _needs_managed_python()
    runner, uv = _python_runner(tmp_path, as_root=True)
    marker = tmp_path / "planted-ran.txt"
    planted = write_planted_python(tmp_path / "planted-bin", marker)
    result = _run_in(
        shell,
        runner,
        "--pwshpy-no-install-uv",
        env=_trust_env(tmp_path, uv.parent, planted.parent),
        cwd=_work_dir(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reported = _reported_python(result)
    assert reported is not None and managed.resolve() in Path(reported).resolve().parents, result.stdout
    assert not marker.exists(), "the planted python ran"


@pytest.mark.parametrize(("as_root", "planted_runs"), [(True, False), (False, True)], ids=["root", "not-root"])
def test_as_root_a_python_version_file_above_the_cache_is_ignored(
    tmp_path: Path, as_root: bool, planted_runs: bool
) -> None:
    """[89]: uv reads .python-version from the script's directory up, and a path there beats only-managed."""
    _needs_managed_python()
    runner, uv = _python_runner(tmp_path, as_root=as_root)
    marker = tmp_path / "planted-ran.txt"
    planted = write_planted_python(tmp_path / "planted-bin", marker)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / ".python-version").write_text(f"{planted}\n", encoding="utf-8")
    result = _run_in(
        ["sh"], runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, uv.parent), cwd=_work_dir(tmp_path)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file() is planted_runs, result.stdout + result.stderr


@pytest.mark.parametrize("variable", ["UV_PYTHON", "VIRTUAL_ENV", "CONDA_PREFIX"])
@pytest.mark.parametrize(("as_root", "planted_runs"), [(True, False), (False, True)], ids=["root", "not-root"])
def test_as_root_a_variable_that_names_an_interpreter_is_dropped(
    tmp_path: Path, variable: str, as_root: bool, planted_runs: bool
) -> None:
    """[89]: UV_PYTHON naming a path, or an active virtual or conda environment, each beats
    only-managed; as root the runner drops the variable and says so, not root it reaches uv."""
    _needs_managed_python()
    runner, uv = _python_runner(tmp_path, as_root=as_root)
    marker = tmp_path / "planted-ran.txt"
    if variable == "UV_PYTHON":
        value = str(write_planted_python(tmp_path / "planted-bin", marker))
    else:
        value = str(write_planted_environment(tmp_path / "planted-env", marker))
    environment = {**_trust_env(tmp_path, uv.parent), variable: value}
    result = _run_in(["sh"], runner, "--pwshpy-no-install-uv", env=environment, cwd=_work_dir(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file() is planted_runs, result.stdout + result.stderr
    assert (f"pwshpy: ignoring {variable}" in result.stderr) is not planted_runs, result.stderr


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize("as_root", [True, False], ids=["root", "not-root"])
def test_the_uv_command_line_carries_only_managed_exactly_as_root(
    tmp_path: Path, shell_id: str, shell: list[str], as_root: bool
) -> None:
    """[89]: the argv uv receives - only-managed and the / project anchor as root, else neither."""
    runner, _ = _python_runner(tmp_path, as_root=as_root)
    leaf = tmp_path / "rec" / "bin"
    leaf.mkdir(parents=True)
    record = tmp_path / "argv.txt"
    uv = write_recording_uv(leaf, record)
    leaf.chmod(0o755)
    (tmp_path / "rec").chmod(0o755)
    result = _run_in(shell, runner, "--pwshpy-uv", str(uv), env=_trust_env(tmp_path), cwd=_work_dir(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    argv = record.read_text(encoding="utf-8").splitlines()
    if as_root:
        python = str(uv.parent / "python")
        managed = [
            "--no-config",
            "--python-preference",
            "only-managed",
            "--python",
            python,
            "--project",
            "/",
            "--directory",
            "/",
        ]
        assert argv[:11] == ["run", "--no-project", *managed], argv
        assert argv[11].startswith("/") and argv[11].endswith("/" + SHIM_MODULE), argv
        found = [line for line in Path(f"{record}.python").read_text(encoding="utf-8").splitlines() if ":" not in line]
        assert found == [
            "python",
            "find",
            "--no-config",
            "--directory",
            "/",
            "--system",
            "--python-preference",
            "only-managed",
        ], found
    else:
        assert argv[:2] == ["run", "--no-project"], argv
        assert "--python-preference" not in argv and "--project" not in argv, argv
        # Not root, the runner is shim-free: uv runs the entry itself.
        assert "--directory" not in argv and argv[2].endswith("/which_python.py"), argv


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize("as_root", [True, False], ids=["root", "not-root-control"])
def test_as_root_the_variables_that_steer_the_managed_python_are_dropped(
    tmp_path: Path, shell_id: str, shell: list[str], as_root: bool
) -> None:
    """[59]: another install dir, config file, mirror or download list never reaches uv as root;
    not root (the control) each reaches it unchanged."""
    runner, _ = _python_runner(tmp_path, as_root=as_root)
    leaf = tmp_path / "rec" / "bin"
    leaf.mkdir(parents=True)
    record = tmp_path / "argv.txt"
    uv = write_recording_uv(leaf, record)
    leaf.chmod(0o755)
    (tmp_path / "rec").chmod(0o755)
    steering = {name: str(tmp_path / f"steer-{name.lower()}") for name in MANAGED_PYTHON_VARIABLES}
    environment = {**_trust_env(tmp_path), **steering}
    result = _run_in(shell, runner, "--pwshpy-uv", str(uv), env=environment, cwd=_work_dir(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    lines = record.read_text(encoding="utf-8").splitlines()
    recorded = dict(line[4:].split("=", 1) for line in lines if line.startswith("env:"))
    for name, value in steering.items():
        dropped_line = f"pwshpy: ignoring {name}" in result.stderr
        if as_root:
            # The runner names its own install directory (the account's), never the caller's.
            assert recorded.get(name) != value and dropped_line, (name, recorded, result.stderr)
        else:
            assert recorded.get(name) == value and not dropped_line, (name, recorded, result.stderr)


def _recording_python_runner(tmp_path: Path, *, as_root: bool, real_data_home: bool = False) -> tuple[Path, Path, Path]:
    """A which-python runner whose uv is a recording double in a tree counted as root's: the runner,
    the double, and the file it records the ``uv run`` call into."""
    runner, _ = _python_runner(tmp_path, as_root=as_root, real_data_home=real_data_home)
    leaf = tmp_path / "rec" / "bin"
    leaf.mkdir(parents=True)
    record = tmp_path / "argv.txt"
    uv = write_recording_uv(leaf, record)
    leaf.chmod(0o755)
    (tmp_path / "rec").chmod(0o755)
    return runner, uv, record


def _recorded_variables(record: Path) -> dict[str, str]:
    lines = record.read_text(encoding="utf-8").splitlines()
    return dict(line[4:].split("=", 1) for line in lines if line.startswith("env:"))


def _passwd_data_home() -> Path:
    """The data directory the passwd database gives this process's account (POSIX only)."""
    if sys.platform == "win32":  # pragma: no cover - this module is POSIX-only
        raise AssertionError("POSIX only")
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".local" / "share"


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize("as_root", [True, False], ids=["root", "not-root-control"])
def test_as_root_the_managed_python_dir_is_the_accounts_own(
    tmp_path: Path, shell_id: str, shell: list[str], as_root: bool
) -> None:
    """[93]: as root uv's managed-Python directory is the passwd home's, whatever HOME and XDG_DATA_HOME
    say, and PYTHONHOME / PYTHONPATH are dropped with a line each; not root (the control) the runner
    names no directory and both variables reach uv unchanged."""
    runner, uv, record = _recording_python_runner(tmp_path, as_root=as_root, real_data_home=True)
    library = {"PYTHONHOME": str(tmp_path / "planted-home"), "PYTHONPATH": str(tmp_path / "planted-path")}
    environment = {**_trust_env(tmp_path), **library, "XDG_DATA_HOME": str(tmp_path / "elsewhere")}
    environment.pop("UV_PYTHON_INSTALL_DIR")
    result = _run_in(shell, runner, "--pwshpy-uv", str(uv), env=environment, cwd=_work_dir(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = _recorded_variables(record)
    if as_root:
        assert recorded.get("UV_PYTHON_INSTALL_DIR") == str(_passwd_data_home() / "uv" / "python"), recorded
    else:
        assert "UV_PYTHON_INSTALL_DIR" not in recorded, recorded
    for name, value in library.items():
        dropped_line = f"pwshpy: ignoring {name}" in result.stderr
        if as_root:
            assert name not in recorded and dropped_line, (name, recorded, result.stderr)
        else:
            assert recorded.get(name) == value and not dropped_line, (name, recorded, result.stderr)


@pytest.mark.parametrize("as_root", [True, False], ids=["root", "not-root-control"])
def test_as_root_the_python_is_found_for_the_scripts_requires_python(tmp_path: Path, as_root: bool) -> None:
    """[A1]: uv run only warns when --python names an interpreter outside the script's requires-python,
    so a root run asks uv python find for one that matches; not root, uv run reads the block itself."""
    entry = tmp_path / "which_python.py"
    entry.write_text('# /// script\n# requires-python = ">=3.10,<4"\n# ///\nprint("never run")\n')
    runner = _pack_sh(entry, tmp_path / "which_python.sh", as_root=as_root)
    text = runner.read_text(encoding="utf-8").replace(
        _TRUSTED_UIDS, f"PWSHPY_TRUSTED_UIDS='0 {tmp_path.stat().st_uid}'\n"
    )
    runner.write_text(text, encoding="utf-8", newline="\n")
    leaf = tmp_path / "rec" / "bin"
    leaf.mkdir(parents=True)
    record = tmp_path / "argv.txt"
    uv = write_recording_uv(leaf, record)
    leaf.chmod(0o755)
    (tmp_path / "rec").chmod(0o755)
    result = _run_in(["sh"], runner, "--pwshpy-uv", str(uv), env=_trust_env(tmp_path), cwd=_work_dir(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    found = Path(f"{record}.python")
    if as_root:
        calls = [line for line in found.read_text(encoding="utf-8").splitlines() if ":" not in line]
        assert calls[-1] == ">=3.10,<4", calls
    else:
        assert not found.exists()


#: A key uv refuses to parse: a uv.toml holding it fails every uv call that reads it (exit 2, naming it).
_PLANTED_UV_KEY = "pwshpy-planted-key"


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize("scope", ["system", "user"])
@pytest.mark.parametrize("as_root", [True, False], ids=["root", "not-root-control"])
def test_as_root_no_uv_configuration_file_is_read(
    tmp_path: Path, shell_id: str, shell: list[str], scope: str, as_root: bool
) -> None:
    """[A2] (user decision: ignore all uv config): a uv.toml can name a cache directory, an index or a
    Python download mirror, so a root run passes --no-config to every uv call. The planted config holds
    a key uv refuses, so reading it fails the run with no side effect; the control shows uv reads it
    (system: XDG_CONFIG_DIRS, user: XDG_CONFIG_HOME)."""
    _needs_managed_python()
    runner, uv = _python_runner(tmp_path, as_root=as_root)
    config_root = tmp_path / f"config-{scope}"
    (config_root / "uv").mkdir(parents=True)
    (config_root / "uv" / "uv.toml").write_text(f"{_PLANTED_UV_KEY} = 1\n", encoding="utf-8")
    variable = "XDG_CONFIG_DIRS" if scope == "system" else "XDG_CONFIG_HOME"
    environment = {**_trust_env(tmp_path, uv.parent), variable: str(config_root)}
    result = _run_in(shell, runner, "--pwshpy-no-install-uv", env=environment, cwd=_work_dir(tmp_path))
    if as_root:
        assert result.returncode == 0, result.stdout + result.stderr
        assert _PLANTED_UV_KEY not in result.stderr, result.stderr
    else:
        assert result.returncode == 2 and _PLANTED_UV_KEY in result.stderr, result.stdout + result.stderr


# --- [57] as root uv starts in / and the shim returns to the caller's directory -------------------------
# uv probes a .venv in its working directory and every directory above, and under only-managed it even
# RUNS the script on that venv's python (measured with uv 0.13.0), so a root run started from a
# directory another account can write would run that account's interpreter. As root, uv starts in /
# (--directory) and runs the generated shim, which changes back to the caller's directory first.

_ENTRY_REPORTING_CWD = """import os
import sys

print("python:" + sys.executable)
print("cwd:" + os.getcwd())
print("argv:" + repr(sys.argv[1:]))
"""


def _reported_cwd(result: subprocess.CompletedProcess[str]) -> Path | None:
    for line in result.stdout.splitlines():
        if line.startswith("cwd:") and line[4:]:
            return Path(line[4:].strip())
    return None


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize(("as_root", "planted_runs"), [(True, False), (False, True)], ids=["root", "not-root-control"])
def test_as_root_a_venv_above_the_working_directory_never_runs(
    tmp_path: Path, shell_id: str, shell: list[str], as_root: bool, planted_runs: bool
) -> None:
    """[57]: the planted .venv's python never starts as root, and the script still sees the caller's
    cwd and its own arguments; not root (the control) uv starts in the caller's directory and runs it."""
    _needs_managed_python()
    runner, uv = _python_runner(tmp_path, as_root=as_root, source=_ENTRY_REPORTING_CWD)
    marker = tmp_path / "planted-venv-ran.txt"
    work = tmp_path / "project" / "work"
    write_planted_venv(tmp_path / "project" / ".venv", marker)
    work.mkdir()
    arguments = ["a b", "", "it's"]
    environment = _trust_env(tmp_path, uv.parent)
    result = _run_in(shell, runner, "--pwshpy-no-install-uv", *arguments, env=environment, cwd=work)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file() is planted_runs, result.stdout + result.stderr
    reported = _reported_cwd(result)
    assert reported is not None and reported.samefile(work), result.stdout
    assert f"argv:{arguments!r}" in result.stdout, result.stdout


#: The runner's uv anchor, and a test's stand-in for it (one counted substitution).
_ANCHOR = "PWSHPY_ANCHOR=/\n"


def _with_anchor(runner: Path, anchor: Path) -> None:
    """Make ``runner`` start uv in ``anchor`` instead of /: no test can plant a .venv above /."""
    text = runner.read_text(encoding="utf-8")
    assert text.count(_ANCHOR) == 1
    runner.write_text(text.replace(_ANCHOR, f"PWSHPY_ANCHOR='{anchor}'\n"), encoding="utf-8", newline="\n")


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize(("as_root", "planted_runs"), [(True, False), (False, True)], ids=["root", "not-root-control"])
def test_as_root_a_venv_above_the_anchor_never_runs(
    tmp_path: Path, shell_id: str, shell: list[str], as_root: bool, planted_runs: bool
) -> None:
    """[A1]: uv probes a .venv in its working directory and EVERY directory above it, so a .venv above
    the anchor would run as root; a root run names the interpreter outright. The control, not root and
    started in the anchor itself, shows the plant is reachable."""
    _needs_managed_python()
    runner, uv = _python_runner(tmp_path, as_root=as_root, source=_ENTRY_REPORTING_CWD)
    marker = tmp_path / "planted-venv-ran.txt"
    write_planted_venv(tmp_path / "anchor-parent" / ".venv", marker)
    anchor = tmp_path / "anchor-parent" / "anchor"
    anchor.mkdir()
    _with_anchor(runner, anchor)
    result = _run_in(shell, runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, uv.parent), cwd=anchor)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file() is planted_runs, result.stdout + result.stderr
    reported = _reported_cwd(result)
    assert reported is not None and reported.samefile(anchor), result.stdout


#: The refusal of uv's managed-Python directory, and the remedy every refused managed-Python path names.
_PYTHON_DIR_REFUSED = "refusing uv's managed-Python directory"
_PYTHON_REMEDY = "chown root, then chmod go-w"


def _planted_python_env(tmp_path: Path, uv_dir: Path, marker: Path) -> tuple[dict[str, str], Path]:
    """A root run's environment whose managed-Python directory holds a planted install: (env, directory)."""
    planted = planted_managed_python(tmp_path / "planted-managed", marker)
    if planted is None:
        pytest.skip("this host has no uv-managed Python (and the test must not download one)")
    environment = _trust_env(tmp_path, uv_dir)
    environment.update(managed_python_env(tmp_path, managed=planted))
    return environment, planted


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
@pytest.mark.parametrize("refused", [True, False], ids=["untrusted-directory", "trusted-directory-control"])
def test_as_root_an_untrusted_managed_python_dir_is_refused_before_uv_runs_a_python_in_it(
    tmp_path: Path, shell_id: str, shell: list[str], refused: bool
) -> None:
    """[X21 P1]: `uv python find` EXECUTES every interpreter it considers in uv's managed-Python
    directory, so a .pth planted there ran as root before the found path was judged. The directory is
    now judged before uv starts: every account may write it, the run exits 127 and the plant never
    runs. The control, the same root run with the directory left trusted, runs the plant."""
    runner, uv = _python_runner(tmp_path, as_root=True)
    marker = tmp_path / "planted-pth-ran.txt"
    environment, planted = _planted_python_env(tmp_path, uv.parent, marker)
    if refused:
        planted.chmod(0o777)
    result = _run_in(shell, runner, "--pwshpy-no-install-uv", env=environment, cwd=_work_dir(tmp_path))
    if refused:
        assert not marker.exists(), "uv ran a Python from the untrusted directory as root: " + marker.read_text()
        assert result.returncode == 127, result.stdout + result.stderr
        assert _PYTHON_DIR_REFUSED in result.stderr and _PYTHON_REMEDY in result.stderr, result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file(), "the control never ran the planted .pth: " + result.stderr
    assert _reported_python(result) is not None, result.stdout


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_a_python_whose_install_another_account_can_modify_is_refused(
    tmp_path: Path, shell_id: str, shell: list[str]
) -> None:
    """[A1]: the Python uv found must still pass the same check as uv itself, inside a trusted
    managed-Python directory too: an install every account can write refuses it (exit 127, the
    script never runs), and the refusal names the remedy."""
    runner, uv = _python_runner(tmp_path, as_root=True)
    environment, planted = _planted_python_env(tmp_path, uv.parent, tmp_path / "planted-pth-ran.txt")
    (install,) = [child for child in planted.iterdir() if child.is_dir()]
    install.chmod(0o777)
    result = _run_in(shell, runner, "--pwshpy-no-install-uv", env=environment, cwd=_work_dir(tmp_path))
    assert result.returncode == 127, result.stdout + result.stderr
    assert "refusing the Python uv found at" in result.stderr and _PYTHON_REMEDY in result.stderr, result.stderr
    assert _PYTHON_DIR_REFUSED not in result.stderr, result.stderr
    assert _reported_python(result) is None, result.stdout


@pytest.mark.parametrize("as_root", [True, False], ids=["root", "not-root"])
@pytest.mark.parametrize(
    "spec",
    ["./{wheel}", "{wheel}", "wheels/{wheel}", LOCAL_WHEEL_DISTRIBUTION + " @ ./{wheel}"],
    ids=["dot-slash", "bare-file", "subdirectory", "named-direct-reference"],
)
def test_a_relative_with_path_resolves_against_the_callers_directory(tmp_path: Path, as_root: bool, spec: str) -> None:
    """[57]: a --with path relative to where the runner is started still resolves when uv starts in /."""
    _needs_managed_python()
    work = tmp_path / "work"
    wheel = write_local_wheel(work / "wheels" if spec.startswith("wheels/") else work)
    source = f"import {LOCAL_WHEEL_DISTRIBUTION}\nprint('wheel:' + {LOCAL_WHEEL_DISTRIBUTION}.VALUE)\n"
    runner, uv = _python_runner(tmp_path, as_root=as_root, source=source, with_packages=[spec.format(wheel=wheel.name)])
    result = _run_in(["sh"], runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, uv.parent), cwd=work)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "wheel:from the wheel" in result.stdout, result.stdout


@pytest.mark.parametrize("as_root", [True, False], ids=["root", "not-root"])
def test_a_relative_cache_directory_still_runs_the_script(tmp_path: Path, as_root: bool) -> None:
    """[57]: PWSHPY_PACK_CACHE given relative to the caller's directory still names the script uv runs."""
    _needs_managed_python()
    runner, uv = _python_runner(tmp_path, as_root=as_root, source=_ENTRY_REPORTING_CWD)
    work = _work_dir(tmp_path)
    environment = {**_trust_env(tmp_path, uv.parent), "PWSHPY_PACK_CACHE": "relative-cache"}
    result = _run_in(["sh"], runner, "--pwshpy-no-install-uv", env=environment, cwd=work)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (work / "relative-cache").is_dir()
    reported = _reported_cwd(result)
    assert reported is not None and reported.samefile(work), result.stdout


# --- [62] names the .sh carries as literals ----------------------------------------------------------


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_an_entry_and_a_data_file_named_with_a_quote_run_and_round_trip(
    tmp_path: Path, shell_id: str, shell: list[str]
) -> None:
    """[62]: it's.py as the entry and d'x.json as an included file: the runner parses, verifies, runs."""
    entry = tmp_path / "it's.py"
    entry.write_text("import sys\nprint('ran:' + str(len(sys.argv)))\n", encoding="utf-8")
    data = tmp_path / "d'x.json"
    data.write_text("{}\n", encoding="utf-8")
    runner = _pack_sh(entry, tmp_path / "quoted.sh", include=[str(data)])
    result = _run(shell, runner, env=_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ran:1" in result.stdout, result.stdout
    info = _run(shell, runner, "--pwshpy-info", env=_env(tmp_path))
    assert "entry      : it's.py" in info.stdout, info.stdout
    assert unpack_script(runner, tmp_path / "out").entry == "it's.py"


@pytest.mark.parametrize("shell_id,shell", _SHELLS, ids=[s[0] for s in _SHELLS])
def test_as_root_an_entry_named_with_a_quote_runs_through_the_shim(
    tmp_path: Path, shell_id: str, shell: list[str]
) -> None:
    """[A5]: as root uv runs the generated shim, which names the entry as a Python string literal, so
    it's.py must survive that path too, the caller's directory and arguments included."""
    _needs_managed_python()
    runner, uv = _python_runner(tmp_path, as_root=True, source=_ENTRY_REPORTING_CWD, entry_name="it's.py")
    work = _work_dir(tmp_path)
    result = _run_in(shell, runner, "--pwshpy-no-install-uv", "a b", env=_trust_env(tmp_path, uv.parent), cwd=work)
    assert result.returncode == 0, result.stdout + result.stderr
    reported = _reported_cwd(result)
    assert reported is not None and reported.samefile(work), result.stdout
    assert "argv:['a b']" in result.stdout, result.stdout


#: The start of a root run's uv options; the control below removes --no-config from it.
_ROOT_RUN_OPTIONS = "set -- --no-config --python-preference only-managed"


@pytest.mark.parametrize("no_config", [True, False], ids=["root", "root-without-no-config-control"])
def test_as_root_a_uv_toml_directory_above_the_anchor_changes_nothing(tmp_path: Path, no_config: bool) -> None:
    """[A2]: uv run --project reads a uv.toml from the project directory and every one above it unless
    --no-config, so a DIRECTORY named uv.toml there (any account can create C:\\uv.toml above System32)
    failed every elevated run; now it changes nothing. The control, the same root run with --no-config
    taken off its uv run, fails on it: the plant is reachable."""
    _needs_managed_python()
    runner, uv = _python_runner(tmp_path, as_root=True)
    (tmp_path / "anchor-parent" / "uv.toml").mkdir(parents=True)
    anchor = tmp_path / "anchor-parent" / "anchor"
    anchor.mkdir()
    _with_anchor(runner, anchor)
    if not no_config:
        text = runner.read_text(encoding="utf-8")
        assert text.count(_ROOT_RUN_OPTIONS) == 1
        runner.write_text(text.replace(_ROOT_RUN_OPTIONS, "set -- --python-preference only-managed"), encoding="utf-8")
    result = _run_in(["sh"], runner, "--pwshpy-no-install-uv", env=_trust_env(tmp_path, uv.parent), cwd=anchor)
    if no_config:
        assert result.returncode == 0, result.stdout + result.stderr
        assert _reported_python(result) is not None, result.stdout
    else:
        assert result.returncode == 2 and "uv.toml" in result.stderr, result.stdout + result.stderr
