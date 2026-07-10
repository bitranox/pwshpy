"""native credentials - a non-echoing prompt and a Windows Credential Manager vault.

PowerShell's credential story is awkward: ``Get-Credential`` is interactive and
useless in a scheduler, and DPAPI-encrypted credential FILES are locked to one user
on one machine (so the service account cannot decrypt what an admin saved) - which is
why people fall back to plaintext passwords in scripts.  This subsystem offers a typed
alternative:

- :func:`prompt_credential` reads a password without echoing (portable ``getpass``)
  and returns a :class:`~pwshpy.domain.records.Credential` whose secret is masked.
- :class:`NativeCredentialStore` persists/reads/removes credentials in the Windows
  Credential Manager (``win32cred``) - the proper vault an unattended job should read
  from instead of a DPAPI file that will not decrypt under another account.

``win32cred`` is imported lazily so a portable install stays clean; the store raises
:class:`~pwshpy.domain.errors.PlatformUnsupportedError` off Windows.

Contents:
    * :func:`prompt_credential` - interactively read a credential (portable).
    * :class:`NativeCredentialStore` - save/load/delete in the Windows vault.
"""

from __future__ import annotations

import getpass
import importlib
from typing import Any

from pydantic import SecretStr

from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import Credential

#: Win32 ``ERROR_NOT_FOUND`` - CredRead raises this when the target is absent.
_ERROR_NOT_FOUND = 1168


def prompt_credential(username: str | None = None, *, prompt: str = "Password: ", target: str = "") -> Credential:
    """Interactively read a credential; the password is entered without echoing.

    The portable, typed equivalent of ``Get-Credential``.  ``username`` is prompted
    for when not supplied.

    Args:
        username: The account name; prompted for interactively when ``None``.
        prompt: The password prompt text.
        target: An optional label for the credential (e.g. the resource it unlocks).

    Returns:
        A :class:`~pwshpy.domain.records.Credential` with a masked secret.

    Example:
        >>> callable(prompt_credential)
        True
    """
    resolved_user = username if username is not None else input("Username: ")
    secret = getpass.getpass(prompt)
    return Credential(target=target, username=resolved_user, secret=SecretStr(secret))


class NativeCredentialStore:
    """Persist and retrieve generic credentials in the Windows Credential Manager.

    Uses ``win32cred`` (CredWrite/CredRead/CredDelete) with ``CRED_TYPE_GENERIC``.
    Windows-only: raises :class:`~pwshpy.domain.errors.PlatformUnsupportedError` off
    Windows.

    Example:
        >>> store = NativeCredentialStore()
        >>> callable(store.save)
        True
    """

    def save(self, target: str, username: str, secret: str) -> Credential:
        """Store (or overwrite) a credential under ``target``; return the stored record."""
        win32cred = _load_win32cred()
        blob = {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": target,
            "UserName": username,
            "CredentialBlob": secret,
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        }
        try:
            win32cred.CredWrite(blob, 0)
        except Exception as exc:  # pywintypes.error - keep the PwshPyError invariant
            raise NativeCallError(f"CredWrite({target!r}) failed: {exc}") from exc
        return Credential(target=target, username=username, secret=SecretStr(secret))

    def load(self, target: str) -> Credential | None:
        """Read the credential stored under ``target``; ``None`` if there is none."""
        win32cred = _load_win32cred()
        try:
            stored = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
        except Exception as exc:
            if getattr(exc, "winerror", None) == _ERROR_NOT_FOUND:
                return None
            raise NativeCallError(f"CredRead({target!r}) failed: {exc}") from exc
        raw = stored.get("CredentialBlob")
        secret = raw.decode("utf-16-le", errors="replace") if isinstance(raw, bytes) else str(raw or "")
        return Credential(target=target, username=stored.get("UserName") or "", secret=SecretStr(secret))

    def delete(self, target: str) -> None:
        """Remove the credential stored under ``target``."""
        win32cred = _load_win32cred()
        try:
            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
        except Exception as exc:  # pywintypes.error - keep the PwshPyError invariant
            raise NativeCallError(f"CredDelete({target!r}) failed: {exc}") from exc


def _load_win32cred() -> Any:
    """Import ``win32cred`` lazily; absent means a portable (non-Windows) install."""
    try:
        return importlib.import_module("win32cred")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "The credential store requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc


__all__ = ["NativeCredentialStore", "prompt_credential"]
