"""Credential store via the freedesktop Secret Service: pure attrs (os_agnostic) + a live round-trip.

The attribute schema is a pure function, so it runs everywhere. The live save/load/delete round-trip
needs a running, UNLOCKED desktop keyring on the session bus; it self-skips when none is reachable
(a bare server or CI), so it only really runs where a keyring is present.
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from pwshpy.adapters.native.secret_service import SecretServiceCredentialStore, scoped_attributes
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
def test_save_load_delete_roundtrip() -> None:
    """save then load returns the same secret+username; delete makes load return None.

    Skips (not fails) when there is no unlocked desktop keyring - a save on a locked or absent
    keyring raises a clear NativeCallError, which is exactly the graceful-degradation contract.
    """
    store = SecretServiceCredentialStore()
    try:
        saved = store.save(_TARGET, username="alice", secret="hunter2")
    except NativeCallError as exc:
        pytest.skip(f"no unlocked desktop keyring on the session bus: {exc}")
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
