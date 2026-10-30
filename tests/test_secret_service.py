"""Credential store via the freedesktop Secret Service: pure attrs (os_agnostic) + a live round-trip.

The attribute schema is a pure function, so it runs everywhere. The live save/load/delete round-trip
needs a running, UNLOCKED keyring on the session bus, so it runs ONLY when
``PWSHPY_TEST_SECRET_SERVICE=1`` opts in: by default ``tests/conftest.py`` points the session bus at
a dead address, because touching the desktop keyring from a test pops an unlock dialog on a locked
login keyring. Opt in only on a private bus with a throwaway keyring in a throwaway HOME - the
recipe (still UNVERIFIED) is under "No test touches the desktop keyring" in DEVELOPMENT.md. A
private bus alone is NOT enough: a gnome-keyring-daemon started there still reads and writes the
real ``~/.local/share/keyrings``. Under the opt-in, a keyring that refuses the save FAILS the
round-trip instead of skipping it, so an opted-in run cannot look green without having run it.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from typing import Any

import pytest
from session_bus_isolation import PROCESS_OPTED_IN
from session_bus_isolation import SECRET_SERVICE_OPT_IN as _OPT_IN

from pwshpy.adapters.native.secret_service import SecretServiceCredentialStore, scoped_attributes
from pwshpy.application.ports import CredentialStore
from pwshpy.domain.errors import NativeAccessDeniedError, NativeCallError
from pwshpy.domain.records import Credential

_HAS_JEEPNEY = importlib.util.find_spec("jeepney") is not None
_TARGET = "pwshpy-pytest-cred"


@pytest.mark.os_agnostic
def test_scoped_attributes() -> None:
    """The search attributes scope an item to pwshpy and the target."""
    assert scoped_attributes("db") == {"application": "pwshpy", "target": "db"}


@pytest.mark.os_agnostic
def test_store_is_constructible() -> None:
    """The store constructs without touching D-Bus (I/O happens per call, not at build)."""
    assert callable(SecretServiceCredentialStore().save)


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux") or not _HAS_JEEPNEY, reason="needs Linux + jeepney")
@pytest.mark.skipif(
    not PROCESS_OPTED_IN,
    reason=f"touches a real keyring: set {_OPT_IN}=1 inside a private dbus-run-session (see DEVELOPMENT.md)",
)
def test_save_load_delete_roundtrip() -> None:
    """save then load returns the same secret+username; delete makes load return None.

    Opt-in only (the skip above is evaluated before anything here can reach the bus). The opt-in
    is the caller explicitly asking for the real round-trip, so a keyring that refuses it (no
    default collection, a locked one) FAILS the test instead of skipping it: a skip would let the
    run look green while the round-trip never executed.
    """
    _exercise_roundtrip(SecretServiceCredentialStore())


class _RefusingStore:
    """A credential store whose keyring refuses every save, as a locked or absent one does."""

    def save(self, target: str, username: str, secret: str) -> Credential:
        raise NativeCallError(f"the default keyring is locked (refused {target!r})")

    def load(self, target: str) -> Credential | None:
        raise AssertionError(f"load({target!r}) must not run after a refused save")

    def delete(self, target: str) -> None:
        raise AssertionError(f"delete({target!r}) must not run after a refused save")


@pytest.mark.os_agnostic
def test_an_opted_in_roundtrip_fails_when_the_keyring_refuses() -> None:
    """Under the opt-in a refused save is a FAILURE, never a skip; no bus is involved here."""
    with pytest.raises(pytest.fail.Exception, match="keyring refused the opted-in round-trip"):
        _exercise_roundtrip(_RefusingStore())


def _exercise_roundtrip(store: CredentialStore) -> None:
    """Save, load and delete one item, failing (not skipping) when the keyring refuses the save."""
    try:
        saved = store.save(_TARGET, username="alice", secret="hunter2")
    except NativeCallError as exc:
        pytest.fail(f"the keyring refused the opted-in round-trip (no unlocked default collection?): {exc}")
    try:
        assert isinstance(saved, Credential)
        assert saved.secret.get_secret_value() == "hunter2"

        loaded = store.load(_TARGET)
        assert loaded is not None
        assert loaded.username == "alice"
        assert loaded.secret.get_secret_value() == "hunter2"
    finally:
        store.delete(_TARGET)

    assert store.load(_TARGET) is None


_ACCESS_DENIED = "org.freedesktop.DBus.Error.AccessDenied"
_NO_SUCH_OBJECT = "org.freedesktop.Secret.Error.NoSuchObject"


class _RefusingSessionBus:
    """A session bus whose Secret Service opens a session, then answers every other call with one error.

    The replies are built with jeepney's own message constructors (``new_method_return`` /
    ``new_error``), so the adapter reads the error name from jeepney's real header fields.
    """

    def __init__(self, error_name: str) -> None:
        self._jeepney = importlib.import_module("jeepney")
        self._error_name = error_name
        self.methods: list[str] = []
        self.closed = False

    def send_and_get_reply(self, msg: Any) -> Any:
        method = str(msg.header.fields[self._jeepney.HeaderFields.member])
        self.methods.append(method)
        if method == "OpenSession":
            return self._jeepney.new_method_return(msg, "vo", (("s", ""), "/org/freedesktop/secrets/session/1"))
        return self._jeepney.new_error(msg, self._error_name, "s", ("refused by the test bus",))

    def close(self) -> None:
        self.closed = True


def _use_session_bus(monkeypatch: pytest.MonkeyPatch, bus: _RefusingSessionBus) -> None:
    # jeepney's connect call is the external edge onto the session D-Bus; the adapter is unchanged.
    blocking = importlib.import_module("jeepney.io.blocking")

    def open_dbus_connection(**_kwargs: object) -> _RefusingSessionBus:
        return bus

    monkeypatch.setattr(blocking, "open_dbus_connection", open_dbus_connection)


@pytest.mark.os_linux
@pytest.mark.skipif(not _HAS_JEEPNEY, reason="needs jeepney (a Linux-only dependency)")
def test_a_refused_secret_service_call_is_native_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Secret Service call answered with AccessDenied is a NativeAccessDeniedError (CLI exit 13)."""
    bus = _RefusingSessionBus(_ACCESS_DENIED)
    _use_session_bus(monkeypatch, bus)
    with pytest.raises(NativeAccessDeniedError) as caught:
        SecretServiceCredentialStore().load(_TARGET)
    assert isinstance(caught.value, PermissionError)
    assert _ACCESS_DENIED in str(caught.value)
    assert bus.methods == ["OpenSession", "SearchItems"]
    assert bus.closed


@pytest.mark.os_linux
@pytest.mark.skipif(not _HAS_JEEPNEY, reason="needs jeepney (a Linux-only dependency)")
def test_any_other_secret_service_error_stays_a_plain_native_call_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An error reply that is no refusal stays a NativeCallError naming the error, never a denial."""
    _use_session_bus(monkeypatch, _RefusingSessionBus(_NO_SUCH_OBJECT))
    with pytest.raises(NativeCallError) as caught:
        SecretServiceCredentialStore().load(_TARGET)
    assert not isinstance(caught.value, NativeAccessDeniedError)
    assert _NO_SUCH_OBJECT in str(caught.value)
