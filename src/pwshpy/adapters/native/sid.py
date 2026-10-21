"""SID string resolution for native adapters (Windows; pywin32 imported lazily).

Task Scheduler reports a principal as whatever string it was registered with - a SID or
an account name, localized for built-ins - so readers normalise it to a SID here.

Contents:
    * :func:`sid_string` - a SID or account name -> its SID string.
"""

from __future__ import annotations

import importlib
from typing import Any

from ...domain.errors import NativeCallError, PlatformUnsupportedError


def _load_security() -> Any:
    """Import ``win32security`` lazily, so importing this module stays safe off Windows."""
    try:
        return importlib.import_module("win32security")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError("Resolving an account to a SID requires pywin32 (Windows only).") from exc


def sid_string(name_or_sid: str) -> str:
    """Return ``name_or_sid`` as a SID string (``""`` stays ``""``).

    A value that is already a SID passes through without a lookup; an account name is
    resolved with ``LookupAccountName`` on the local machine, which also accepts the
    localized names of built-in accounts.

    Args:
        name_or_sid: A SID string (``"S-1-..."``), an account name, or ``""``.

    Returns:
        The SID string, or ``""`` for an empty input.

    Raises:
        NativeCallError: The account name does not resolve to a SID.
        PlatformUnsupportedError: An account name needs resolving but pywin32 is absent.

    Example:
        >>> sid_string("S-1-5-18"), sid_string("")
        ('S-1-5-18', '')
    """
    if not name_or_sid or name_or_sid.startswith("S-1-"):
        return name_or_sid
    security = _load_security()
    try:
        sid, _domain, _kind = security.LookupAccountName(None, name_or_sid)
        return str(security.ConvertSidToStringSid(sid))
    except Exception as exc:
        raise NativeCallError(str(exc) or f"cannot resolve account {name_or_sid!r} to a SID") from exc


__all__ = ["sid_string"]
