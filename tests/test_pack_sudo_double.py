"""A ``sudo`` double for the pack runners' elevate path, and the tests that pin what it models.

The runners ask for the password once with ``sudo -v``, then confirm with ``sudo -n -v`` that a
credential was cached, and relaunch with ``sudo -n``, so sudo can never prompt a second time
([136-sh]). The double tells those calls apart: ``sudo -v`` is the prompt, ``-n`` succeeds only
while a credential is cached (none is on a ``timestamp_timeout=0`` setup), and a command without
``-n`` is a second prompt, logged as ``PROMPT <command>``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the sudo double is a POSIX sh script")


def write_sudo_double(bin_dir: Path, *, grant: bool, cached: bool = True, calls: Path | None = None) -> Path:
    """Write a ``sudo`` double into ``bin_dir`` (put it first on PATH); its path.

    Args:
        bin_dir: Directory to write the double into.
        grant: Whether the prompt (``sudo -v``, or a command without ``-n``) succeeds.
        cached: Whether ``-n`` finds a cached credential; False models ``timestamp_timeout=0``.
        calls: Log of every call's arguments, one line each (default ``<bin_dir>/sudo-calls.txt``).

    Returns:
        The written double.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     write_sudo_double(Path(d), grant=True).name
        'sudo'
    """
    log = calls if calls is not None else bin_dir / "sudo-calls.txt"
    lines = [
        "#!/bin/sh",
        f"printf '%s\\n' \"$*\" >> '{log}'",
        'if [ "$1" = "-n" ]; then',
        "    shift",
        f'    if [ "$1" = "-v" ]; then exit {0 if cached else 1}; fi',
        f"    if [ {int(cached)} != 1 ]; then echo 'sudo: a password is required' >&2; exit 1; fi",
        '    exec "$@"',
        "fi",
        f'if [ "$1" = "-v" ]; then exit {0 if grant else 1}; fi',
        f"printf 'PROMPT %s\\n' \"$*\" >> '{log}'",
        f"if [ {int(grant)} != 1 ]; then exit 1; fi",
        'exec "$@"',
    ]
    script = bin_dir / "sudo"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    script.chmod(0o755)
    return script


def second_prompts(calls: Path) -> list[str]:
    """The logged calls that would have prompted for a password a second time."""
    if not calls.exists():
        return []
    return [line for line in calls.read_text(encoding="utf-8").splitlines() if line.startswith("PROMPT ")]


def _call(sudo: Path, *args: str) -> int:
    return subprocess.run([str(sudo), *args], capture_output=True, check=False).returncode  # noqa: S603 - the double


@_POSIX_ONLY
@pytest.mark.os_posix
@pytest.mark.parametrize("cached", [True, False], ids=["cached", "timestamp-timeout-0"])
def test_the_double_grants_the_noninteractive_calls_only_while_cached(tmp_path: Path, cached: bool) -> None:
    """``-v`` prompts and grants; ``-n -v`` and ``-n <cmd>`` follow the cache; a bare command is logged."""
    sudo = write_sudo_double(tmp_path, grant=True, cached=cached)
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
    sudo = write_sudo_double(tmp_path, grant=False)
    assert _call(sudo, "-v") == 1
    assert _call(sudo, "true") == 1
