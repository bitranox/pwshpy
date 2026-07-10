"""Live systemd mutating controller test - on a SCRATCH unit, root only.

Creates a throwaway ``pwshpy-pytest.service`` (a ``sleep`` unit), drives start/stop/
set_startup through SystemdServiceController, and removes it again. It never touches a
real service. local_only + Linux + root, so it runs on the dev box, never in CI.
"""
# subprocess drives systemctl for the unit-file lifecycle (create/reload/cleanup); fixed argv.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pwshpy.adapters.native.systemd_services import SystemdServiceController
from pwshpy.domain.enums import ServiceStartType, ServiceState

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
