"""Live systemd mutating controller test - on a SCRATCH unit, root only.

Creates throwaway ``pwshpy-pytest*.service`` units (a ``sleep`` unit, a slow-stopping one, a
oneshot one), drives start/stop/restart/set_startup through SystemdServiceController or the ``ps``
facade, and removes them again. It never touches a
real service. local_only + Linux + root, so it runs on the dev box, never in CI.
"""
# subprocess drives systemctl for the unit-file lifecycle (create/reload/cleanup); fixed argv.

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pwshpy.adapters.native.systemd_services import SystemdServiceController
from pwshpy.composition.facade import build_ps
from pwshpy.domain.enums import ServiceStartType, ServiceState
from pwshpy.domain.errors import OperationTimeoutError

_UNIT = "pwshpy-pytest.service"
_UNIT_PATH = Path("/etc/systemd/system") / _UNIT
_UNIT_BODY = (
    "[Unit]\nDescription=pwshpy pytest scratch\n"
    "[Service]\nType=simple\nExecStart=/bin/sleep 600\n"
    "[Install]\nWantedBy=multi-user.target\n"  # needs an [Install] section to be enable-able
)

pytestmark = [
    pytest.mark.local_only,
    pytest.mark.mutating,
    pytest.mark.os_linux,
    pytest.mark.skipif(
        not sys.platform.startswith("linux") or os.geteuid() != 0,  # type: ignore[attr-defined]
        reason="mutating systemd controller needs Linux + root and a scratch unit",
    ),
]


@pytest.fixture
def scratch_unit() -> object:
    """Install a throwaway .service unit; remove it (stopped + disabled) on teardown."""
    _UNIT_PATH.write_text(_UNIT_BODY)
    subprocess.run(["systemctl", "daemon-reload"], check=True)  # noqa: S607 - fixed argv
    try:
        yield _UNIT
    finally:
        subprocess.run(["systemctl", "stop", _UNIT], check=False)  # noqa: S603, S607
        subprocess.run(["systemctl", "disable", _UNIT], check=False)  # noqa: S603, S607
        _UNIT_PATH.unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=False)  # noqa: S607


def test_controller_start_stop_and_enable_disable(scratch_unit: str) -> None:
    """start -> Running, stop -> Stopped, set_startup Automatic -> enabled, Disabled -> disabled."""
    controller = SystemdServiceController()

    started = controller.start(scratch_unit)
    assert started.status is ServiceState.RUNNING

    stopped = controller.stop(scratch_unit)
    assert stopped.status is ServiceState.STOPPED

    enabled = controller.set_startup(scratch_unit, ServiceStartType.AUTOMATIC)
    assert enabled.start_type is ServiceStartType.AUTOMATIC

    disabled = controller.set_startup(scratch_unit, ServiceStartType.DISABLED)
    assert disabled.start_type is ServiceStartType.DISABLED


_SLOW_UNIT = "pwshpy-pytest-slowstop.service"
_SLOW_UNIT_PATH = Path("/etc/systemd/system") / _SLOW_UNIT
_SLOW_STOP_SECONDS = 8
# ExecStop holds the unit in "deactivating" for _SLOW_STOP_SECONDS, the systemd twin of a Windows
# service sitting in StopPending (wuauserv), so the no-wait and timeout paths are observable.
_SLOW_UNIT_BODY = (
    "[Unit]\nDescription=pwshpy pytest slow-stop scratch\n"
    f"[Service]\nType=simple\nExecStart=/bin/sleep 600\nExecStop=/bin/sleep {_SLOW_STOP_SECONDS}\n"
)


@pytest.fixture
def slow_stop_unit() -> object:
    """Install a throwaway unit whose stop takes _SLOW_STOP_SECONDS; remove it on teardown."""
    _SLOW_UNIT_PATH.write_text(_SLOW_UNIT_BODY)
    subprocess.run(["systemctl", "daemon-reload"], check=True)  # noqa: S607 - fixed argv
    try:
        yield _SLOW_UNIT
    finally:
        subprocess.run(["systemctl", "kill", _SLOW_UNIT], check=False)  # noqa: S603, S607
        subprocess.run(["systemctl", "stop", _SLOW_UNIT], check=False)  # noqa: S603, S607
        _SLOW_UNIT_PATH.unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=False)  # noqa: S607


def test_stop_without_wait_returns_before_a_slow_stop_finishes(slow_stop_unit: str) -> None:
    """wait=False returns while the unit deactivates; a short wait times out; a long one settles."""
    controller = SystemdServiceController()
    assert controller.start(slow_stop_unit).status is ServiceState.RUNNING

    started = time.monotonic()
    issued = controller.stop(slow_stop_unit, wait=False)
    # Well under the ExecStop duration: the call did not wait for inactive.
    assert time.monotonic() - started < _SLOW_STOP_SECONDS / 2
    # Right after StopUnit the job may not have begun yet, so active is possible; inactive is not.
    assert issued.status in {ServiceState.STOP_PENDING, ServiceState.RUNNING}

    with pytest.raises(OperationTimeoutError) as caught:
        controller.stop(slow_stop_unit, timeout=0.5)
    assert caught.value.timeout == 0.5

    assert controller.stop(slow_stop_unit, timeout=_SLOW_STOP_SECONDS * 4).status is ServiceState.STOPPED


_ONESHOT_UNIT = "pwshpy-pytest-oneshot.service"
_ONESHOT_UNIT_PATH = Path("/etc/systemd/system") / _ONESHOT_UNIT
# Type=oneshot without RemainAfterExit: the start job ends with the unit back at inactive, so a
# start that waited for "active" would sit out its whole timeout although the job succeeded.
_ONESHOT_UNIT_BODY = "[Unit]\nDescription=pwshpy pytest oneshot scratch\n[Service]\nType=oneshot\nExecStart=/bin/true\n"


@pytest.fixture
def oneshot_unit() -> object:
    """Install a throwaway oneshot unit; remove it on teardown."""
    _ONESHOT_UNIT_PATH.write_text(_ONESHOT_UNIT_BODY)
    subprocess.run(["systemctl", "daemon-reload"], check=True)  # noqa: S607 - fixed argv
    try:
        yield _ONESHOT_UNIT
    finally:
        subprocess.run(["systemctl", "stop", _ONESHOT_UNIT], check=False)  # noqa: S603, S607
        _ONESHOT_UNIT_PATH.unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=False)  # noqa: S607


def test_starting_a_oneshot_unit_returns_once_its_job_is_done(oneshot_unit: str) -> None:
    """ps.start_service / restart_service of a oneshot unit return promptly, reporting Stopped."""
    ps = build_ps()
    for verb in (ps.start_service, ps.restart_service):
        started = time.monotonic()
        info = verb(oneshot_unit, timeout=20.0)
        assert time.monotonic() - started < 5, verb.__name__
        assert info.status is ServiceState.STOPPED, verb.__name__
