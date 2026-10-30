"""The tests that pin what the shared ``sudo`` double (:func:`elevation_doubles.write_fake_sudo`) models.

The runners and ``ps.elevate`` ask for the password once with ``sudo -v``, then confirm with
``sudo -n -v`` that a credential was cached, and relaunch with ``sudo -n``, so sudo can never prompt a
second time ([136]). The double tells those calls apart: ``sudo -v`` is the prompt, ``-n`` succeeds
only while a credential is cached (none is on a ``timestamp_timeout=0`` setup; under
``timestamp_type=ppid`` only for the parent process that prompted), and a command without ``-n`` is
a second prompt, logged as ``PROMPT <command>``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from elevation_doubles import second_prompts, write_fake_sudo

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the sudo double is a POSIX sh script")


def _call(sudo: Path, *args: str) -> int:
    return subprocess.run([str(sudo), *args], capture_output=True, check=False).returncode  # noqa: S603 - the double


@_POSIX_ONLY
@pytest.mark.os_posix
@pytest.mark.parametrize("cached", [True, False], ids=["cached", "timestamp-timeout-0"])
def test_the_double_grants_the_noninteractive_calls_only_while_cached(tmp_path: Path, cached: bool) -> None:
    """``-v`` prompts and grants; ``-n -v`` and ``-n <cmd>`` follow the cache; a bare command is logged."""
    sudo = write_fake_sudo(tmp_path, grant=True, cached=cached)
    assert _call(sudo, "-v") == 0
    assert _call(sudo, "-n", "-v") == (0 if cached else 1)
    assert _call(sudo, "-n", "true") == (0 if cached else 1)
    assert second_prompts(tmp_path / "sudo-calls.txt") == []
    assert _call(sudo, "true") == 0
    assert second_prompts(tmp_path / "sudo-calls.txt") == ["PROMPT true"]


@_POSIX_ONLY
@pytest.mark.os_posix
def test_the_double_refuses_the_prompt_when_not_granted(tmp_path: Path) -> None:
    """A refused prompt: ``-v`` and a bare command exit 1."""
    sudo = write_fake_sudo(tmp_path, grant=False)
    assert _call(sudo, "-v") == 1
    assert _call(sudo, "true") == 1


@_POSIX_ONLY
@pytest.mark.os_posix
@pytest.mark.parametrize("interrupt", [True, False], ids=["interrupted", "not-interrupted-control"])
def test_an_interrupted_prompt_signals_the_caller_and_grants_nothing(tmp_path: Path, interrupt: bool) -> None:
    """``interrupt_prompt``: ``-v`` sends SIGINT to its caller and exits 1 without recording a credential,
    so a ``per_parent`` ``-n`` from the same shell fails; the control grants and signals nothing."""
    sudo = write_fake_sudo(tmp_path, grant=True, per_parent=True, interrupt_prompt=interrupt)
    script = f"trap 'echo INT; exit 7' INT; '{sudo}' -v; echo \"v=$?\"; '{sudo}' -n true; echo \"n=$?\""
    result = subprocess.run(  # noqa: S603 - fixed argv, the double
        ["/bin/sh", "-c", script], capture_output=True, text=True, check=False, timeout=30
    )
    if interrupt:
        assert (result.returncode, result.stdout) == (7, "INT\n"), result.stderr
        assert (tmp_path / "sudo-calls.txt").read_text(encoding="utf-8").splitlines() == ["-v"]
        assert not (tmp_path / "sudo-credential-parent").exists()
    else:
        assert (result.returncode, result.stdout) == (0, "v=0\nn=0\n"), result.stderr


@_POSIX_ONLY
@pytest.mark.os_posix
def test_per_parent_the_credential_belongs_to_the_process_that_prompted(tmp_path: Path) -> None:
    """``timestamp_type=ppid``: ``-n`` from the shell that ran ``-v`` finds the credential, ``-n`` from a
    different parent (this test process, or a shell exec'd into sudo) does not."""
    sudo = write_fake_sudo(tmp_path, grant=True, per_parent=True)
    same_parent = subprocess.run(  # noqa: S603 - fixed argv, the double
        ["/bin/sh", "-c", f"'{sudo}' -v && '{sudo}' -n -v && '{sudo}' -n true"], capture_output=True, check=False
    )
    assert same_parent.returncode == 0, same_parent.stderr
    assert _call(sudo, "-n", "true") == 1
    exec_away = subprocess.run(  # noqa: S603 - fixed argv, the double
        ["/bin/sh", "-c", f"'{sudo}' -v && exec '{sudo}' -n true"], capture_output=True, check=False
    )
    assert exec_away.returncode == 1, "an exec'd sudo -n has the shell's parent as its own"


@_POSIX_ONLY
@pytest.mark.os_posix
@pytest.mark.parametrize("nopasswd", ["ALL", "/bin/sh"], ids=["nopasswd-all", "nopasswd-one-program"])
def test_a_nopasswd_rule_beside_a_password_rule_runs_only_n_commands_it_covers(tmp_path: Path, nopasswd: str) -> None:
    """[280] ``nopasswd`` (sudo's ``verifypw=all`` with mixed rules): ``-n <program>`` runs without any
    credential when the NOPASSWD rule covers the program, ``-v`` and ``-n -v`` still want the password
    the user does not have, and a program the rule does not cover is refused like any ``-n``.

    Both programs exist on every POSIX host: macOS has no ``/bin/true``, so a double that runs it
    exits 126 there whatever the rule says."""
    sudo = write_fake_sudo(tmp_path, grant=False, cached=False, nopasswd=nopasswd)
    assert _call(sudo, "-n", "/bin/sh", "-c", ":") == 0
    assert _call(sudo, "-n", sys.executable, "-c", "pass") == (0 if nopasswd == "ALL" else 1)
    assert _call(sudo, "-n", "-v") == 1
    assert _call(sudo, "-v") == 1
    assert second_prompts(tmp_path / "sudo-calls.txt") == []


@_POSIX_ONLY
@pytest.mark.os_posix
@pytest.mark.parametrize(
    ("env_keep", "expected"),
    [
        (None, {"HOME": "caller-home", "KEPT": "k", "DROPPED": "d"}),
        ((), {"HOME": "reset-home", "KEPT": None, "DROPPED": None}),
        (("KEPT", "HOME", "UNSET"), {"HOME": "caller-home", "KEPT": "k", "DROPPED": None}),
    ],
    ids=["sudo-E", "env-reset", "env-keep"],
)
def test_the_double_models_env_reset_and_env_keep(
    tmp_path: Path, env_keep: tuple[str, ...] | None, expected: dict[str, str | None]
) -> None:
    """[220] ``env_keep``: None passes the caller's whole environment (``sudo -E``); a tuple starts the
    command with only ``HOME=reset_home``, the caller's ``PATH`` and each listed name the caller has set
    (a kept ``HOME`` wins, an unset listed name stays unset), as sudo's ``env_reset`` with ``env_keep``."""
    sudo = write_fake_sudo(
        tmp_path, grant=False, cached=False, nopasswd="ALL", env_keep=env_keep, reset_home="reset-home"
    )
    caller = {"PATH": "/usr/bin:/bin", "HOME": "caller-home", "KEPT": "k", "DROPPED": "d"}
    seen = subprocess.run(  # noqa: S603 - the double
        [str(sudo), "-n", "/usr/bin/env"], capture_output=True, text=True, check=True, env=caller
    ).stdout.splitlines()
    variables = dict(line.split("=", 1) for line in seen)
    assert {name: variables.get(name) for name in expected} == expected, variables
    assert "UNSET" not in variables
    assert variables["PATH"] == caller["PATH"]
