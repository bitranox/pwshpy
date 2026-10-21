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

import importlib.util
import sys

import pytest
from session_bus_isolation import PROCESS_OPTED_IN
from session_bus_isolation import SECRET_SERVICE_OPT_IN as _OPT_IN

from pwshpy.adapters.native.secret_service import SecretServiceCredentialStore, scoped_attributes
from pwshpy.application.ports import CredentialStore
from pwshpy.domain.errors import NativeCallError
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
