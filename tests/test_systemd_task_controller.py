"""Live systemd scheduled-task controller test - on SCRATCH units, root only.

Two tasks, two mechanisms:
  * an on-demand ``.service`` created by ``register`` (run/stop/unregister), and
  * a scratch ``.timer`` (enable arms it, disable disarms it).
Neither touches a real task. local_only + Linux + root, so it runs on the dev box, never in CI.
"""
# subprocess writes/reloads the scratch .timer unit files and is the belt-and-braces teardown.

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from pwshpy.adapters.native.systemd_timers import SystemdScheduledTaskController
from pwshpy.domain.enums import TaskState
from pwshpy.domain.errors import NativeCallError

_UNIT_DIR = Path("/etc/systemd/system")
_ONDEMAND = "pwshpy-pytest-task"
_TIMER = "pwshpy-pytest-timer"
_TIMER_SERVICE_UNIT = "[Unit]\nDescription=pwshpy pytest timer job\n[Service]\nType=oneshot\nExecStart=/bin/true\n"
_TIMER_UNIT = (
    "[Unit]\nDescription=pwshpy pytest timer\n[Timer]\nOnCalendar=*-*-* 03:00:00\n[Install]\nWantedBy=timers.target\n"
)

pytestmark = [
    pytest.mark.local_only,
    pytest.mark.mutating,
    pytest.mark.os_linux,
    pytest.mark.skipif(
        not sys.platform.startswith("linux") or os.geteuid() != 0,  # type: ignore[attr-defined]
        reason="mutating systemd task controller needs Linux + root",
    ),
]


def _reload() -> None:
    subprocess.run(["systemctl", "daemon-reload"], check=False)  # noqa: S607 - fixed argv


@pytest.fixture
def ondemand_task() -> Iterator[str]:
    """Yield an on-demand task name; on teardown remove its unit file and reload."""
    try:
        yield _ONDEMAND
    finally:
        (_UNIT_DIR / f"{_ONDEMAND}.service").unlink(missing_ok=True)
        _reload()


@pytest.fixture
def timer_task() -> Iterator[str]:
    """Install a scratch .timer + .service pair; on teardown stop, disable and remove them."""
    (_UNIT_DIR / f"{_TIMER}.service").write_text(_TIMER_SERVICE_UNIT)
    (_UNIT_DIR / f"{_TIMER}.timer").write_text(_TIMER_UNIT)
    _reload()
    try:
        yield _TIMER
    finally:
        subprocess.run(["systemctl", "stop", f"{_TIMER}.timer"], check=False)  # noqa: S603, S607
        subprocess.run(["systemctl", "disable", f"{_TIMER}.timer"], check=False)  # noqa: S603, S607
        (_UNIT_DIR / f"{_TIMER}.timer").unlink(missing_ok=True)
        (_UNIT_DIR / f"{_TIMER}.service").unlink(missing_ok=True)
        _reload()


def test_register_run_unregister_ondemand(ondemand_task: str) -> None:
    """register -> Ready+enabled, run (oneshot exits), enable/disable rejected, unregister -> gone."""
    controller = SystemdScheduledTaskController()

    registered = controller.register(ondemand_task, program="/bin/true", description="pwshpy pytest task")
    assert registered.task_name == ondemand_task
    assert registered.state is TaskState.READY
    assert registered.enabled is True
    assert (_UNIT_DIR / f"{ondemand_task}.service").exists()

    controller.run(ondemand_task)  # oneshot /bin/true starts and exits; no exception

    # an on-demand task has no schedule, so enable/disable are a clear error, not a silent no-op
    with pytest.raises(NativeCallError, match="on-demand"):
        controller.disable(ondemand_task)

    controller.unregister(ondemand_task)
    assert not (_UNIT_DIR / f"{ondemand_task}.service").exists()


def test_enable_disable_timer(timer_task: str) -> None:
    """disable disarms the .timer (enabled False), enable arms it (enabled True, Ready)."""
    controller = SystemdScheduledTaskController()

    enabled = controller.enable(timer_task)
    assert enabled.enabled is True
    assert enabled.state is TaskState.READY  # armed, waiting to fire

    disabled = controller.disable(timer_task)
    assert disabled.enabled is False
