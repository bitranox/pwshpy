"""A refusal systemd sends over D-Bus is a NativeAccessDeniedError, like every other native refusal.

systemd names a refusal in the reply's ``error_name`` header field: ``AccessDenied`` (a bus
policy refusal, or a host without polkit) or ``InteractiveAuthorizationRequired`` (polkit would
have to ask; measured on a polkit host as a non-root caller). Any other D-Bus error stays a plain
NativeCallError. The fake bus stands in for the system D-Bus (the external edge); one live test
sends a refused StopUnit for a unit that does not exist, which changes nothing on the host.
"""

from __future__ import annotations

import os
import sys

import pytest
from systemd_fake_bus import (
    ACCESS_DENIED,
    INTERACTIVE_AUTHORIZATION_REQUIRED,
    Address,
    FakeConn,
    FakeCore,
    Message,
    Reply,
    use_bus,
)

from pwshpy.adapters.native import systemd_dbus
from pwshpy.adapters.native.systemd_services import SystemdServiceController
from pwshpy.domain.errors import NativeAccessDeniedError, NativeCallError


class _OneReplyConn:
    """A bus connection answering every call with one fixed reply."""

    def __init__(self, reply: Reply) -> None:
        self._reply = reply

    def send_and_get_reply(self, _msg: Message) -> Reply:
        return self._reply


def _bus(reply: Reply) -> systemd_dbus.Bus:
    return systemd_dbus.Bus(conn=_OneReplyConn(reply), core=FakeCore(), mgr=Address("/org/freedesktop/systemd1"))


@pytest.mark.os_agnostic
@pytest.mark.parametrize("name", [ACCESS_DENIED, INTERACTIVE_AUTHORIZATION_REQUIRED])
def test_a_refusal_reply_is_native_access_denied(name: str) -> None:
    """Both refusal names map to NativeAccessDeniedError (a PermissionError: CLI exit 13)."""
    reply = Reply(("Access denied",), error=True, error_name=name)
    with pytest.raises(NativeAccessDeniedError) as caught:
        systemd_dbus.call(_bus(reply), "StopUnit", "ss", ("x.service", "replace"))
    assert isinstance(caught.value, PermissionError)
    assert name in str(caught.value)
    assert "StopUnit" in str(caught.value)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "name", ["org.freedesktop.systemd1.NoSuchUnit", "org.freedesktop.DBus.Error.UnknownMethod", ""]
)
def test_any_other_error_reply_stays_a_plain_native_call_error(name: str) -> None:
    """A D-Bus error that is no refusal (or carries no name) is a NativeCallError, not a denial."""
    reply = Reply(("something else",), error=True, error_name=name)
    with pytest.raises(NativeCallError) as caught:
        systemd_dbus.call(_bus(reply), "GetUnit", "s", ("x.service",))
    assert not isinstance(caught.value, NativeAccessDeniedError)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("name", [ACCESS_DENIED, INTERACTIVE_AUTHORIZATION_REQUIRED])
def test_a_refused_stop_raises_native_access_denied(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """stop_service as non-root: systemd refuses StopUnit, the controller raises NativeAccessDeniedError."""
    conn = FakeConn([], {}, frozenset({"x.service"}), denied={"StopUnit": name})
    use_bus(monkeypatch, conn)
    with pytest.raises(NativeAccessDeniedError):
        SystemdServiceController().stop("x.service")
    assert conn.verbs == ["StopUnit"]


def _is_root() -> bool:
    if sys.platform == "win32":
        return False
    return os.geteuid() == 0


@pytest.mark.local_only
@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd is Linux")
def test_a_live_refused_stop_as_non_root_is_native_access_denied() -> None:
    """Against the real system bus: a non-root StopUnit is refused as a NativeAccessDeniedError.

    The unit does not exist, so even a caller systemd lets through changes nothing (it answers
    "not loaded"). Run it as a non-root user on a systemd host; root is let through, so it skips.
    """
    if _is_root():
        pytest.skip("root is allowed to send StopUnit; the refusal needs a non-root caller")
    try:
        bus = systemd_dbus.open_bus()
    except NativeCallError as exc:
        pytest.skip(f"no system D-Bus here: {exc}")
    bus.conn.close()
    with pytest.raises(NativeAccessDeniedError):
        SystemdServiceController().stop("pwshpy-refusal-probe-7f3a.service", wait=False)
