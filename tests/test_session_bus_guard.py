"""The default test run can never reach the desktop session bus (and so never the keyring).

``tests/conftest.py`` points ``DBUS_SESSION_BUS_ADDRESS`` at an address nobody listens on unless
``PWSHPY_TEST_SECRET_SERVICE=1`` opts in. Any contact with ``org.freedesktop.secrets`` on the real
session bus D-Bus-activates the user's keyring daemon, and a locked login keyring then pops a
password dialog on the desktop - once per gate run. These tests pin that the guard is in force for
the test process itself AND for every child it spawns.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from session_bus_isolation import GUARD_MARKER as _MARKER
from session_bus_isolation import PROCESS_OPTED_IN
from session_bus_isolation import SECRET_SERVICE_OPT_IN as _OPT_IN

from pwshpy.adapters.native.secret_service import SecretServiceCredentialStore
from pwshpy.domain.errors import NativeCallError

pytestmark = pytest.mark.skipif(PROCESS_OPTED_IN, reason=f"{_OPT_IN}=1 opts the run in to a (private) session bus")


def _session_address() -> str | None:
    return os.environ.get("DBUS_SESSION_BUS_ADDRESS")


@pytest.mark.os_agnostic
def test_session_bus_address_is_the_dead_guard_address() -> None:
    """The inherited session-bus address is replaced, not just removed.

    Removing the variable is not enough: libdbus, sd-bus and GDBus clients then fall back to
    ``$XDG_RUNTIME_DIR/bus`` (or X11 autolaunch), which on a desktop is a live session bus that can
    activate the keyring too. A set-but-unreachable address disables every fallback.
    """
    address = _session_address()
    assert address is not None
    assert _MARKER in address


@pytest.mark.os_agnostic
def test_spawned_children_inherit_the_guard() -> None:
    """A subprocess a test spawns sees the guard address too (it lives in os.environ)."""
    probe = "import os; print(os.environ.get('DBUS_SESSION_BUS_ADDRESS', '<unset>'))"
    result = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    )
    assert _MARKER in result.stdout


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the session bus is a Linux desktop concept")
def test_session_bus_is_unreachable() -> None:
    """Connecting to the session bus fails, so no D-Bus activation can be requested."""
    blocking = pytest.importorskip("jeepney.io.blocking")
    assert _MARKER in (_session_address() or "")  # never open a connection to a live bus
    with pytest.raises(OSError):
        blocking.open_dbus_connection(bus="SESSION").close()


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Secret Service store is the Linux backend")
def test_secret_service_store_cannot_reach_a_keyring() -> None:
    """The production Secret Service adapter degrades to its clear 'no session bus' error."""
    pytest.importorskip("jeepney")
    assert _MARKER in (_session_address() or "")  # never run the save below against a live bus
    with pytest.raises(NativeCallError, match="no session D-Bus"):
        SecretServiceCredentialStore().save("pwshpy-guard-probe", username="u", secret="s")


_DOTENV_PROBE = """
import os


def test_probe() -> None:
    # Read into locals so a failure prints these two values, never the whole environment.
    opt_in = os.environ.get("PWSHPY_TEST_SECRET_SERVICE")
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    assert opt_in == "1"  # control: the .env line WAS loaded
    assert "pwshpy-tests-no-session-bus" in address
"""


@pytest.mark.os_agnostic
def test_a_dotenv_only_opt_in_does_not_switch_the_guard_off(tmp_path: Path) -> None:
    """``PWSHPY_TEST_SECRET_SERVICE=1`` in the repo's ``.env`` alone leaves the guard in force.

    The conftest loads ``.env`` at import, before ``pytest_configure``; only the process
    environment may opt in. This runs the REAL conftest in a child pytest: copied into a scratch
    project whose root ``.env`` carries the opt-in line (the conftest finds ``.env`` next to its
    own ``tests/`` dir), with the opt-in absent from the child's environment. The child inherits a
    dead address WITHOUT the guard marker, so even a broken guard leaves it off every bus, and the
    probe sees the marker only if the conftest replaced that address.
    """
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    here = Path(__file__).parent
    for helper in ("conftest.py", "session_bus_isolation.py"):
        shutil.copyfile(here / helper, tests_dir / helper)
    (tests_dir / "test_probe.py").write_text(_DOTENV_PROBE, encoding="utf-8", newline="\n")
    (tmp_path / ".env").write_text(f"{_OPT_IN}=1\n", encoding="utf-8", newline="\n")
    child_env = {key: value for key, value in os.environ.items() if key != _OPT_IN}
    child_env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:abstract=pwshpy-inherited-{uuid.uuid4().hex}"
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--no-cov", "-q", "tests/test_probe.py"]
    result = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        argv,
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
