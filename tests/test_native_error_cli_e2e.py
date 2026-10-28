"""A refused native read surfaces as a permission error end to end (Linux, real CLI process).

The path runs through the real adapter (``posix_acl.iter_acl`` stats the file, the kernel answers
EACCES because the parent directory has mode 000) and the real CLI boundary
(``lib_cli_exit_tools`` maps the exception to the exit code). Only the private temp directory is
touched, so the test is safe in CI's ubuntu lane. Root reads through mode 000, so it skips there.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import lib_cli_exit_tools
import pytest

from pwshpy import NativeAccessDeniedError, NativeCallError, ps

_LINUX = sys.platform.startswith("linux")
_os: Any = os  # funnel the POSIX-only os.geteuid past the type checker's platform pruning

pytestmark = [
    pytest.mark.os_posix,
    pytest.mark.os_linux,
    pytest.mark.skipif(not _LINUX, reason="the refused read is staged with Linux mode bits"),
    pytest.mark.skipif(_LINUX and _os.geteuid() == 0, reason="root reads through a mode-000 directory"),
]


@pytest.fixture
def denied_file(tmp_path: Path) -> Iterator[Path]:
    """A file inside a directory whose mode 000 makes every stat of the file fail with EACCES."""
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "secret.txt"
    target.write_bytes(b"x\n")
    locked.chmod(0o000)
    try:
        yield target
    finally:
        # Restore the mode so pytest can clean tmp_path up.
        locked.chmod(0o700)


def test_cli_get_acl_on_a_refused_path_exits_with_the_permission_error_code(denied_file: Path) -> None:
    # env= is omitted: the child inherits the full environment unchanged.
    result = subprocess.run(  # noqa: S603 - fixed argv: this interpreter running pwshpy itself
        [sys.executable, "-m", "pwshpy", "get_acl", str(denied_file), "--json"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )

    assert result.returncode == lib_cli_exit_tools.get_system_exit_code(PermissionError())
    assert result.returncode != lib_cli_exit_tools.get_system_exit_code(RuntimeError())
    assert result.stdout == ""
    assert "NativeAccessDeniedError: cannot stat" in result.stderr


def test_library_get_acl_on_a_refused_path_raises_a_permission_error_that_is_a_native_call_error(
    denied_file: Path,
) -> None:
    with pytest.raises(PermissionError) as excinfo:
        ps.get_acl(str(denied_file)).to_list()

    assert isinstance(excinfo.value, NativeCallError)
    assert isinstance(excinfo.value, NativeAccessDeniedError)
    assert excinfo.value.errno == errno.EACCES
    assert excinfo.value.hresult is None
