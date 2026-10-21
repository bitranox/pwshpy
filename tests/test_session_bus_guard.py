"""The default test run can never reach the desktop session bus (and so never the keyring).

``tests/conftest.py`` points ``DBUS_SESSION_BUS_ADDRESS`` at an address nobody listens on unless
``PWSHPY_TEST_SECRET_SERVICE=1`` opts in. Any contact with ``org.freedesktop.secrets`` on the real
session bus D-Bus-activates the user's keyring daemon, and a locked login keyring then pops a
password dialog on the desktop - once per gate run. These tests pin that the guard is in force for
the test process itself AND for every child it spawns.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pwshpy.adapters.native.secret_service import SecretServiceCredentialStore
from pwshpy.domain.errors import NativeCallError

_OPT_IN = "PWSHPY_TEST_SECRET_SERVICE"
_MARKER = "pwshpy-tests-no-session-bus"

pytestmark = pytest.mark.skipif(
    os.environ.get(_OPT_IN) == "1", reason=f"{_OPT_IN}=1 opts the run in to a (private) session bus"
)


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
