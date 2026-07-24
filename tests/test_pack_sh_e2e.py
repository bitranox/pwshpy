"""End-to-end: pack a script to a POSIX ``.sh`` and RUN it under every available major shell.

The `.sh` runner is strict POSIX, so it must behave identically under whatever ``/bin/sh`` is -
dash, busybox ash, bash-as-sh, zsh - not only bash.  Each installed interpreter runs the SAME
artefact and must produce the same result: the payload unpacks, uv resolves the PEP 723 deps, the
local submodule imports, arguments arrive intact, and the script's exit code comes back.

``local_only``: needs a POSIX shell + uv, and the dependency cases reach PyPI.  Skips cleanly on
Windows (no POSIX ``sh``) and when uv is absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

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
    """Two cold-cache runs of the same pack must both succeed (mkdir-lock serializes extraction)."""
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
    start = next(i for i, line in enumerate(lines) if line.strip() == "__PWSHPY_PAYLOAD_B64__")
    body = lines[start + 1]
    lines[start + 1] = ("B" if body[:1] != "B" else "C") + body[1:]
    runner.write_text("\n".join(lines) + "\n")
    result = _run(["sh"], runner, env=_env(tmp_path))
    assert result.returncode != 0
    assert "corrupt" in (result.stdout + result.stderr).lower()


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
