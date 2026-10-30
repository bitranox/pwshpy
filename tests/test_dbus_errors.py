"""The shared D-Bus error mapping: a refusal is a NativeAccessDeniedError, decided by the error NAME.

Two kinds of reply: the fake bus's hand-built one (runs on every OS), and a reply built by jeepney's
own ``new_error`` (Linux only, where jeepney is installed), which proves the name is read from
jeepney's real header fields and not only from the fake's copy of them.
"""

from __future__ import annotations

import importlib
import importlib.util

import pytest
from systemd_fake_bus import ACCESS_DENIED, INTERACTIVE_AUTHORIZATION_REQUIRED, Reply

from pwshpy.adapters.native import dbus_errors, systemd_dbus
from pwshpy.domain.errors import NativeAccessDeniedError, NativeCallError

_HAS_JEEPNEY = importlib.util.find_spec("jeepney") is not None
_OTHER_ERROR = "org.freedesktop.DBus.Error.UnknownMethod"


@pytest.mark.os_agnostic
@pytest.mark.parametrize("name", [ACCESS_DENIED, INTERACTIVE_AUTHORIZATION_REQUIRED])
def test_a_refusal_name_maps_to_native_access_denied(name: str) -> None:
    """Both refusal names give a NativeAccessDeniedError whose message names the call and the error."""
    error = dbus_errors.reply_error("Some Call", Reply(("not allowed",), error=True, error_name=name))
    assert isinstance(error, NativeAccessDeniedError)
    assert str(error) == f"Some Call failed: {name}: not allowed"


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("name", "message"), [(_OTHER_ERROR, f"X failed: {_OTHER_ERROR}: boom"), ("", "X failed: boom")]
)
def test_any_other_reply_maps_to_a_plain_native_call_error(name: str, message: str) -> None:
    """Another error name, or none at all, is a NativeCallError and never a denial."""
    error = dbus_errors.reply_error("X", Reply(("boom",), error=True, error_name=name))
    assert type(error) is NativeCallError
    assert str(error) == message


@pytest.mark.os_agnostic
def test_systemd_dbus_keeps_exporting_the_moved_helpers() -> None:
    """systemd_dbus re-exports the helpers it used to define, so its callers keep working."""
    assert systemd_dbus.reply_error is dbus_errors.reply_error
    assert systemd_dbus.error_name is dbus_errors.error_name


@pytest.mark.os_linux
@pytest.mark.skipif(not _HAS_JEEPNEY, reason="needs jeepney (a Linux-only dependency)")
@pytest.mark.parametrize(("name", "denied"), [(ACCESS_DENIED, True), (_OTHER_ERROR, False)])
def test_a_real_jeepney_error_reply_is_mapped_by_its_name(name: str, denied: bool) -> None:
    """An error reply built by jeepney itself carries its name where error_name reads it."""
    jeepney = importlib.import_module("jeepney")
    address = jeepney.DBusAddress(
        "/org/freedesktop/systemd1", bus_name="org.freedesktop.systemd1", interface="org.freedesktop.systemd1.Manager"
    )
    reply = jeepney.new_error(jeepney.new_method_call(address, "GetUnit", "s", ("x.service",)), name, "s", ("no",))
    assert dbus_errors.error_name(reply) == name
    assert isinstance(dbus_errors.reply_error("systemd GetUnit", reply), NativeAccessDeniedError) is denied
