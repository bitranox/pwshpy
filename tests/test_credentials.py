"""native credentials: portable prompt + Windows-vault store logic.

The prompt and the store's save/load/delete logic are verified os_agnostically by
faking ``getpass`` and the ``win32cred`` backend (an in-memory vault), so no real
credential is ever written on the dev box.  A ``mutating`` real round-trip against
the Windows Credential Manager runs only on the throwaway VM.
"""
# The fake mirrors the win32cred API, whose functions are PascalCase.
# ruff: noqa: N802

from __future__ import annotations

import sys
from typing import Any

import pytest

from pwshpy.adapters.native import credentials
from pwshpy.adapters.native.credentials import NativeCredentialStore, prompt_credential
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import Credential

#: Win32 ERROR_NO_SUCH_LOGON_SESSION - Credential Manager needs an INTERACTIVE logon
#: session; a pubkey-SSH (network, type 3) logon has none, so CredWrite/Read/Delete fail
#: with this. The vault code is fine; it simply cannot be exercised over SSH.
_ERROR_NO_LOGON_SESSION = "1312"


@pytest.mark.os_agnostic
def test_prompt_credential_masks_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """prompt_credential reads without echo and returns a masked Credential."""
    monkeypatch.setattr(credentials.getpass, "getpass", lambda _prompt="": "hunter2")
    cred = prompt_credential("svc", target="db")
    assert isinstance(cred, Credential)
    assert cred.username == "svc"
    assert cred.target == "db"
    assert cred.to_dict()["secret"] == "**********"  # never leaks in serialization
    assert cred.secret.get_secret_value() == "hunter2"  # retrievable deliberately


@pytest.mark.os_agnostic
def test_prompt_credential_asks_for_username(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing username is read interactively."""
    monkeypatch.setattr(credentials.getpass, "getpass", lambda _prompt="": "pw")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "typed-user")
    cred = prompt_credential()
    assert cred.username == "typed-user"


class _NotFoundError(Exception):
    winerror = 1168  # ERROR_NOT_FOUND


class _FakeWin32Cred:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}

    def CredWrite(self, credential: dict[str, Any], _flags: int) -> None:
        self.store[credential["TargetName"]] = credential

    def CredRead(self, target: str, _cred_type: int, _flags: int) -> dict[str, Any]:
        if target not in self.store:
            raise _NotFoundError
        return self.store[target]

    def CredDelete(self, target: str, _cred_type: int, _flags: int) -> None:
        self.store.pop(target, None)


def _with_fake_vault(monkeypatch: pytest.MonkeyPatch) -> _FakeWin32Cred:
    fake = _FakeWin32Cred()
    monkeypatch.setattr(credentials, "_load_win32cred", lambda: fake)
    return fake


@pytest.mark.os_agnostic
def test_store_save_load_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """save then load returns the same username/secret; the secret stays masked."""
    _with_fake_vault(monkeypatch)
    store = NativeCredentialStore()
    saved = store.save("db", "svc", "p@ss")
    assert saved.to_dict()["secret"] == "**********"
    loaded = store.load("db")
    assert loaded is not None
    assert loaded.username == "svc"
    assert loaded.secret.get_secret_value() == "p@ss"


@pytest.mark.os_agnostic
def test_store_load_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """load of an absent target returns None (ERROR_NOT_FOUND is not an error here)."""
    _with_fake_vault(monkeypatch)
    assert NativeCredentialStore().load("nope") is None


@pytest.mark.os_agnostic
def test_store_delete_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete removes a stored credential."""
    _with_fake_vault(monkeypatch)
    store = NativeCredentialStore()
    store.save("db", "svc", "p@ss")
    store.delete("db")
    assert store.load("db") is None


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Credential Manager is Windows-only")
def test_credential_store_real_roundtrip() -> None:
    """Real Windows Credential Manager round-trip (self-cleaning).

    Requires an INTERACTIVE logon session; skips (not fails) under an SSH network
    logon, where the Credential Manager raises ERROR_NO_SUCH_LOGON_SESSION (1312).
    """
    store = NativeCredentialStore()
    target = "pwshpy_test_credential_DELETEME"
    secret = "p@ss w/ spaces & ünïcode"
    try:
        store.save(target, "tester", secret)
    except NativeCallError as exc:
        if _ERROR_NO_LOGON_SESSION in str(exc):
            pytest.skip("Credential Manager needs an interactive logon session (unavailable over SSH network logon)")
        raise
    try:
        loaded = store.load(target)
        assert loaded is not None
        assert loaded.username == "tester"
        assert loaded.secret.get_secret_value() == secret
    finally:
        store.delete(target)
    assert store.load(target) is None
