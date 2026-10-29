"""One deadline bounds a whole POSIX local-account verb, however many shadow-utils commands it runs.

``new_user`` with a password and ``disabled`` runs ``useradd``, ``chpasswd`` and ``usermod``.
Stand-in commands on PATH (the real seam: ``run_process`` resolves argv[0] on PATH) each sleep
less than ``timeout`` but together longer, so a per-command bound lets the verb run for about three
times ``timeout`` while one overall deadline stops it. A deadline that passes after ``useradd``
still lets ``usermod --lock`` run for a disabled account, and the message names what was done.
Nothing touches a real account: the stand-ins only sleep and log their argv, and the name read
back afterwards is ``root``, which every POSIX host has.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from pwshpy.adapters.native.posix_account_control import PosixLocalAccountController
from pwshpy.domain.errors import NativeCallError, OperationTimeoutError

pytestmark = [
    pytest.mark.os_posix,
    pytest.mark.skipif(sys.platform == "win32", reason="shadow-utils stand-ins are POSIX shell scripts"),
]

_STEP_SECONDS = 0.5
_TIMEOUT = 1.2  # more than one step, less than the three steps of new_user together


@pytest.fixture
def slow_shadow_utils(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put useradd / chpasswd / usermod stand-ins first on PATH; each sleeps one step, then logs its argv.

    Only chpasswd drains stdin (it is the one fed the password): a stand-in reading stdin it is
    never given would wait on the terminal under ``pytest -s``. A stand-in killed while it sleeps
    logs nothing, so ``calls.log`` lists exactly the commands that finished.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for command in ("useradd", "chpasswd", "usermod"):
        drain = "cat >/dev/null\n" if command == "chpasswd" else ""
        script = bin_dir / command
        script.write_text(f'#!/bin/sh\n{drain}sleep {_STEP_SECONDS}\necho "{command} $*" >>"{log}"\n', encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return log


def _calls(log: Path) -> list[str]:
    """The stand-in commands that ran to the end, in order."""
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


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


def test_a_timed_out_disabled_new_user_is_still_locked(slow_shadow_utils: Path) -> None:
    """useradd and chpasswd finish, usermod --lock is killed: the lock still runs, within a grace.

    Without it the account requested disabled is left ENABLED with a known password.
    """
    with pytest.raises(OperationTimeoutError) as caught:
        PosixLocalAccountController().new_user("root", password="pw", disabled=True, timeout=_TIMEOUT)
    assert _calls(slow_shadow_utils) == ["useradd root", "chpasswd ", "usermod --lock root"]
    message = str(caught.value)
    assert "'root' was created" in message
    assert "its password was set" in message
    assert "the account was locked" in message
    assert caught.value.timeout == _TIMEOUT


def test_a_killed_chpasswd_is_named_and_the_account_still_locked(slow_shadow_utils: Path) -> None:
    """chpasswd killed by the deadline: the message says the password may not be set; the lock still runs."""
    with pytest.raises(OperationTimeoutError) as caught:
        PosixLocalAccountController().new_user("root", password="pw", disabled=True, timeout=0.8)
    assert _calls(slow_shadow_utils) == ["useradd root", "usermod --lock root"]
    message = str(caught.value)
    assert "'root' was created" in message
    assert "its password may not be set" in message
    assert "the account was locked" in message


def test_a_timed_out_enabled_new_user_is_not_locked(slow_shadow_utils: Path) -> None:
    """Not disabled: nothing extra runs after the deadline; the message still names what was done."""
    with pytest.raises(OperationTimeoutError) as caught:
        PosixLocalAccountController().new_user("root", password="pw", timeout=0.8)
    assert _calls(slow_shadow_utils) == ["useradd root"]
    message = str(caught.value)
    assert "'root' was created" in message
    assert "its password may not be set" in message
    assert "locked" not in message


@pytest.fixture
def failing_chpasswd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Quick useradd / usermod stand-ins that log their argv, and a chpasswd that fails (exit 1)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for command in ("useradd", "usermod"):
        script = bin_dir / command
        script.write_text(f'#!/bin/sh\necho "{command} $*" >>"{log}"\n', encoding="utf-8")
        script.chmod(0o755)
    chpasswd = bin_dir / "chpasswd"
    chpasswd.write_text(
        "#!/bin/sh\ncat >/dev/null\necho 'chpasswd: PAM: Authentication failure' >&2\nexit 1\n", encoding="utf-8"
    )
    chpasswd.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return log


def test_a_failed_chpasswd_still_locks_a_disabled_new_user(failing_chpasswd: Path) -> None:
    """Not a timeout: chpasswd exits 1. The account requested disabled is still locked, and the
    error names the partial state, as the timeout path does."""
    with pytest.raises(NativeCallError) as caught:
        PosixLocalAccountController().new_user("root", password="pw", disabled=True, timeout=10.0)
    assert not isinstance(caught.value, OperationTimeoutError)
    assert _calls(failing_chpasswd) == ["useradd root", "usermod --lock root"]
    message = str(caught.value)
    assert "chpasswd failed: chpasswd: PAM: Authentication failure" in message
    assert "'root' was created" in message
    assert "its password may not be set" in message
    assert "the account was locked" in message


def test_a_failed_chpasswd_on_an_enabled_new_user_locks_nothing(failing_chpasswd: Path) -> None:
    """Not disabled: no lock runs; the error still names what was done."""
    with pytest.raises(NativeCallError) as caught:
        PosixLocalAccountController().new_user("root", password="pw", timeout=10.0)
    assert _calls(failing_chpasswd) == ["useradd root"]
    assert "'root' was created" in str(caught.value)
    assert "locked" not in str(caught.value)
