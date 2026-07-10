"""Native credential store on Linux via the freedesktop Secret Service (jeepney).

The portable counterpart to the Windows Credential Manager adapter: stores secrets in the desktop
keyring (gnome-keyring / kwallet) through ``org.freedesktop.secrets`` on the session bus - the same
vault ``secret-tool`` and browsers use. No subprocess, no plaintext file.

Secrets travel with the Secret Service "plain" algorithm - no encryption handshake, so no
``cryptography`` dependency. That is sound here: the session bus is a per-user unix socket inside the
same trust boundary as the process, so the bytes never leave the machine or that user.

It needs a running Secret Service with an UNLOCKED default collection. On a bare server or in CI with
no keyring daemon, or a locked keyring that would need an interactive unlock prompt, the operations
raise a clear :class:`NativeCallError` rather than silently dropping the secret. jeepney is loaded
lazily, so importing this module is safe on Windows/macOS.

Contents:
    * :func:`scoped_attributes` - the attribute dict that scopes an item to pwshpy + a target (pure).
    * :class:`SecretServiceCredentialStore` - save/load/delete in the desktop keyring (**mutating**).
"""

from __future__ import annotations

import importlib
from typing import Any, cast

from pydantic import SecretStr

from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import Credential

_BUS = "org.freedesktop.secrets"
_SERVICE_PATH = "/org/freedesktop/secrets"
_SERVICE_IFACE = "org.freedesktop.Secret.Service"
_COLLECTION_IFACE = "org.freedesktop.Secret.Collection"
_ITEM_IFACE = "org.freedesktop.Secret.Item"
_PROPS_IFACE = "org.freedesktop.DBus.Properties"
_LABEL_PROP = "org.freedesktop.Secret.Item.Label"
_ATTR_PROP = "org.freedesktop.Secret.Item.Attributes"
_APP = "pwshpy"  # the "application" attribute that scopes pwshpy's items in a shared keyring
_CONTENT_TYPE = "text/plain; charset=utf8"
_NO_PROMPT = "/"  # Unlock returns this object path when nothing needs an interactive prompt


def scoped_attributes(target: str) -> dict[str, str]:
    """The search attributes that identify pwshpy's item for ``target`` in the keyring.

    Example:
        >>> scoped_attributes("db")
        {'application': 'pwshpy', 'target': 'db'}
    """
    return {"application": _APP, "target": target}


def _load_jeepney() -> tuple[Any, Any]:
    """Import jeepney (core + blocking io); raise a clear error off Linux (no jeepney installed)."""
    try:
        core = importlib.import_module("jeepney")
        blocking = importlib.import_module("jeepney.io.blocking")
    except ImportError as exc:  # pragma: no cover - jeepney is a base Linux dep
        raise PlatformUnsupportedError(
            "the credential store needs Windows (win32cred), or Linux with a desktop keyring (reinstall pwshpy)."
        ) from exc
    return core, blocking


class _Session:
    """A session-bus connection plus an open Secret Service session (plain algorithm)."""

    def __init__(self, core: Any, conn: Any, session: str) -> None:
        self.core = core
        self.conn = conn
        self.session = session

    def call(self, path: str, iface: str, method: str, sig: str = "", body: tuple[Any, ...] = ()) -> Any:
        """One Secret Service method call, mapping a D-Bus error to NativeCallError."""
        addr = self.core.DBusAddress(path, bus_name=_BUS, interface=iface)
        reply = self.conn.send_and_get_reply(self.core.new_method_call(addr, method, sig, body))
        if str(reply.header.message_type.name).lower() == "error":
            raise NativeCallError(f"Secret Service {method} failed: {reply.body}")
        return reply.body


def _open() -> _Session:
    """Connect to the session bus and open a plain Secret Service session (the caller closes it)."""
    core, blocking = _load_jeepney()
    try:
        conn = blocking.open_dbus_connection(bus="SESSION")
    except Exception as exc:  # no session bus at all (bare server, no login session)
        raise NativeCallError("no session D-Bus is available; the credential store needs a desktop session.") from exc
    session = _Session(core, conn, "")
    try:
        _out, path = session.call(_SERVICE_PATH, _SERVICE_IFACE, "OpenSession", "sv", ("plain", ("s", "")))
    except NativeCallError as exc:
        conn.close()
        raise NativeCallError("no Secret Service (desktop keyring) is available on the session bus.") from exc
    session.session = str(path)
    return session


def _unlocked_default(session: _Session) -> str:
    """The default collection path, unlocked; raise a clear error if it needs an interactive prompt."""
    (collection,) = session.call(_SERVICE_PATH, _SERVICE_IFACE, "ReadAlias", "s", ("default",))
    if collection == _NO_PROMPT:
        raise NativeCallError("no default keyring collection is configured; create one in a desktop session.")
    _unlocked, prompt = session.call(_SERVICE_PATH, _SERVICE_IFACE, "Unlock", "ao", ([collection],))
    if prompt != _NO_PROMPT:
        raise NativeCallError("the default keyring is locked; unlock it in a desktop session (no headless prompt).")
    return str(collection)


def _search(session: _Session, target: str) -> list[str]:
    """The item paths matching ``target``, unlocking any locked matches (or raising if a prompt is needed)."""
    unlocked, locked = session.call(_SERVICE_PATH, _SERVICE_IFACE, "SearchItems", "a{ss}", (scoped_attributes(target),))
    items = [str(path) for path in unlocked]
    if locked:
        opened, prompt = session.call(_SERVICE_PATH, _SERVICE_IFACE, "Unlock", "ao", (list(locked),))
        if prompt != _NO_PROMPT:
            raise NativeCallError("a matching keyring item is locked; unlock it in a desktop session.")
        items += [str(path) for path in opened]
    return items


def _item_username(session: _Session, item: str) -> str:
    """Read the ``username`` attribute pwshpy stored on an item (empty if absent)."""
    (variant,) = session.call(item, _PROPS_IFACE, "Get", "ss", (_ITEM_IFACE, "Attributes"))
    attrs: Any = variant
    if isinstance(attrs, tuple):  # a D-Bus variant arrives as (signature, value); take the value
        attrs = cast("Any", attrs[-1])
    return str(attrs.get("username", ""))


class SecretServiceCredentialStore:
    """Persist/read/remove credentials in the Linux desktop keyring (Secret Service, **mutating**).

    The counterpart to the win32 NativeCredentialStore; needs a running, unlocked keyring.

    Example:
        >>> store = SecretServiceCredentialStore()
        >>> callable(store.save)
        True
    """

    def save(self, target: str, username: str, secret: str) -> Credential:
        """Store (or overwrite) a credential under ``target`` in the default collection."""
        session = _open()
        try:
            collection = _unlocked_default(session)
            attrs = {**scoped_attributes(target), "username": username}
            props = {_LABEL_PROP: ("s", f"pwshpy: {target}"), _ATTR_PROP: ("a{ss}", attrs)}
            value = (session.session, b"", secret.encode("utf-8"), _CONTENT_TYPE)
            session.call(collection, _COLLECTION_IFACE, "CreateItem", "a{sv}(oayays)b", (props, value, True))
            return Credential(target=target, username=username, secret=SecretStr(secret))
        finally:
            session.conn.close()

    def load(self, target: str) -> Credential | None:
        """Read the credential stored under ``target``; ``None`` if there is none."""
        session = _open()
        try:
            items = _search(session, target)
            if not items:
                return None
            (struct,) = session.call(items[0], _ITEM_IFACE, "GetSecret", "o", (session.session,))
            secret = bytes(struct[2]).decode("utf-8", errors="replace")  # struct = (session, params, value, type)
            return Credential(target=target, username=_item_username(session, items[0]), secret=SecretStr(secret))
        finally:
            session.conn.close()

    def delete(self, target: str) -> None:
        """Remove every keyring item pwshpy stored under ``target``."""
        session = _open()
        try:
            for item in _search(session, target):
                session.call(item, _ITEM_IFACE, "Delete", "", ())
        finally:
            session.conn.close()


__all__ = ["SecretServiceCredentialStore", "scoped_attributes"]
