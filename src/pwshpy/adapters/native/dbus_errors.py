"""Typed errors for a D-Bus error reply, shared by every jeepney adapter (systemd, Secret Service).

A D-Bus error reply names its error in a header field (``error_name``); the message text in its
body is the service's own and may be translated. So the decision whether a reply is a refusal
reads the NAME only, and every adapter that receives an error reply maps it here, so a refusal is
a :class:`~pwshpy.domain.errors.NativeAccessDeniedError` whichever service sent it.

Contents:
    * :func:`error_name` - the D-Bus error name of an error reply, or "".
    * :func:`reply_error` - the typed error for an error reply (a refusal is access denied).
"""

from __future__ import annotations

from typing import Any

from ...domain.errors import NativeAccessDeniedError, NativeCallError

#: The D-Bus error names a refusal comes as, from systemd and the Secret Service alike (one set,
#: so the two services cannot drift): ``AccessDenied`` (a bus policy refusal, or a host without
#: polkit) and ``InteractiveAuthorizationRequired`` (polkit would have to ask and the call did not
#: allow it; what a non-root systemd StopUnit gets on a polkit host, measured).
_DENIED_ERROR_NAMES = frozenset(
    {
        "org.freedesktop.DBus.Error.AccessDenied",
        "org.freedesktop.DBus.Error.InteractiveAuthorizationRequired",
    }
)


def error_name(reply: Any) -> str:
    """The D-Bus error name of an error ``reply`` (jeepney keeps it in a header field), or "".

    Matched by the field's name, so the adapter needs no jeepney import to read it.

    Example:
        >>> class _Header:
        ...     fields: dict[object, str] = {}
        >>> class _Reply:
        ...     header = _Header()
        >>> error_name(_Reply())
        ''
    """
    fields: dict[Any, Any] = getattr(reply.header, "fields", {}) or {}
    for field, value in fields.items():
        if getattr(field, "name", None) == "error_name":
            return str(value)
    return ""


def reply_error(what: str, reply: Any) -> NativeCallError:
    """The typed error for an error ``reply``: a refusal is a NativeAccessDeniedError.

    The decision reads the error NAME, never the service's (translatable) message text.
    """
    name = error_name(reply)
    text = " ".join(str(part) for part in reply.body) if reply.body else ""
    message = f"{what} failed: {name}: {text}" if name else f"{what} failed: {text}"
    if name in _DENIED_ERROR_NAMES:
        return NativeAccessDeniedError(message)
    return NativeCallError(message)


__all__ = ["error_name", "reply_error"]
