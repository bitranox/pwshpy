"""One deadline bounds a whole POSIX local-account verb, however many shadow-utils commands it runs.

``new_user`` with a password and ``disabled`` runs ``useradd``, ``chpasswd`` and ``usermod``.
Stand-in commands on PATH (the real seam: ``run_process`` resolves argv[0] on PATH) each sleep
less than ``timeout`` but together longer, so a per-command bound lets the verb run for about three
times ``timeout`` while one overall deadline stops it. Nothing touches a real account: the
stand-ins only sleep, and the name read back afterwards is ``root``, which every POSIX host has.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from pwshpy.adapters.native.posix_account_control import PosixLocalAccountController
from pwshpy.domain.errors import OperationTimeoutError

pytestmark = [
    pytest.mark.os_posix,
    pytest.mark.skipif(sys.platform == "win32", reason="shadow-utils stand-ins are POSIX shell scripts"),
]

_STEP_SECONDS = 0.5
_TIMEOUT = 1.2  # more than one step, less than the three steps of new_user together


@pytest.fixture
def slow_shadow_utils(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put useradd / chpasswd / usermod stand-ins first on PATH; each drains stdin and sleeps one step."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ("useradd", "chpasswd", "usermod"):
        script = bin_dir / command
        script.write_text(f"#!/bin/sh\ncat >/dev/null\nsleep {_STEP_SECONDS}\n", encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return bin_dir


def test_new_user_stops_at_one_overall_deadline(slow_shadow_utils: Path) -> None:
    """Three commands that each fit in ``timeout`` but not together raise OperationTimeoutError."""
    started = time.monotonic()
    with pytest.raises(OperationTimeoutError) as caught:
        PosixLocalAccountController().new_user("root", password="pw", disabled=True, timeout=_TIMEOUT)
    elapsed = time.monotonic() - started
    assert caught.value.timeout == _TIMEOUT  # the verb's limit, not what was left for the last command
    assert elapsed < _TIMEOUT + 1.0  # bounded by the one deadline, not 3 x timeout


def test_new_user_within_the_deadline_completes(slow_shadow_utils: Path) -> None:
    """Negative control: the same three commands under a generous timeout finish and read back."""
    user = PosixLocalAccountController().new_user("root", password="pw", disabled=True, timeout=10.0)
    assert user.name == "root"
    assert user.enabled is False
