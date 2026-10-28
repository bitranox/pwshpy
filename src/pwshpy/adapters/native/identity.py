"""native current identity - who this process runs as, read from its token (like ``whoami /user``).

The identity comes from the process token, never from ``USERNAME``/``USERDOMAIN``: under a
SYSTEM scheduled task those name the computer account (``DOMAIN\\HOST$``) while the token
is ``S-1-5-18``, so a script that trusts them misjudges whether it runs as SYSTEM.

- Windows: ``OpenProcessToken`` + ``GetTokenInformation`` (``TokenUser`` for the SID,
  ``TokenIntegrityLevel`` for the mandatory label, ``TokenElevation`` for the elevation
  flag), then ``LookupAccountSid`` for the display name.  ``pywin32`` is loaded lazily.
- POSIX: the effective uid (what the kernel checks on every access, the counterpart of the
  token user) and its ``pwd`` entry for the display name.

Contents:
    * :func:`windows_current_identity` - the Windows token's :class:`CurrentIdentity`.
    * :func:`posix_current_identity` - the POSIX effective uid's :class:`CurrentIdentity`.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import Any

from ...domain.enums import WellKnownSid
from ...domain.errors import PlatformUnsupportedError
from ...domain.records import CurrentIdentity
from .marshal import native_error, to_integrity_level


def _load(module: str) -> Any:
    """Import a platform-only module lazily, so importing this one stays safe on every OS."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - only when called on the other OS family
        raise PlatformUnsupportedError(
            f"the current identity needs pywin32 (Windows) or pwd (POSIX); {module} is unavailable."
        ) from exc


def windows_current_identity(*, load: Callable[[str], Any] | None = None) -> CurrentIdentity:
    """Return the identity of this process's Windows token (like ``whoami /user``).

    Args:
        load: Module loader for ``win32api`` / ``win32con`` / ``win32security``; ``None``
            imports the real pywin32 modules (a test passes doubles).

    Returns:
        The token user's SID, its display names, elevation and integrity level.

    Raises:
        NativeCallError: The token could not be opened or queried.
        PlatformUnsupportedError: pywin32 is not installed (not Windows).

    Example:
        >>> import sys
        >>> sys.platform != "win32" or windows_current_identity().sid.startswith("S-1-")
        True
    """
    # Resolved per call, not as a default: a default is bound when the def runs.
    loader = load if load is not None else _load
    win32api = loader("win32api")
    win32con = loader("win32con")
    security = loader("win32security")
    try:
        token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    except Exception as exc:  # pywintypes.error - keep the PwshPyError invariant
        raise native_error(f"OpenProcessToken failed: {exc}", exc) from exc
    try:
        user_sid, _attributes = _query(security, token, security.TokenUser, "TokenUser")
        label_sid, _attributes = _query(security, token, security.TokenIntegrityLevel, "TokenIntegrityLevel")
        elevated = bool(_query(security, token, security.TokenElevation, "TokenElevation"))
    finally:
        win32api.CloseHandle(token)
    sid = _sid_text(security, user_sid)
    name, domain = _display_names(security, user_sid)
    return CurrentIdentity.model_construct(
        sid=sid,
        account=f"{domain}\\{name}" if domain and name else name,
        name=name,
        domain=domain,
        is_system=sid == WellKnownSid.LOCAL_SYSTEM.value,
        elevated=elevated,
        integrity_level=to_integrity_level(_last_sub_authority(label_sid)),
    )


def _query(security: Any, token: Any, info_class: int, label: str) -> Any:
    """Read one ``GetTokenInformation`` class, wrapping a pywin32 failure into NativeCallError."""
    try:
        return security.GetTokenInformation(token, info_class)
    except Exception as exc:
        raise native_error(f"GetTokenInformation({label}) failed: {exc}", exc) from exc


def _sid_text(security: Any, sid: Any) -> str:
    """Return ``sid`` as its ``S-1-...`` string."""
    try:
        return str(security.ConvertSidToStringSid(sid))
    except Exception as exc:
        raise native_error(f"ConvertSidToStringSid failed: {exc}", exc) from exc


def _display_names(security: Any, sid: Any) -> tuple[str, str]:
    """Return ``(name, domain)`` for ``sid``, or ``("", "")`` when it does not resolve.

    The names are display only: the SID already identifies the caller, so a SID with no
    mapping (an orphaned or a logon SID, a domain controller out of reach) must not turn
    "who am I" into an error.
    """
    try:
        name, domain, _kind = security.LookupAccountSid(None, sid)
    except Exception:
        return "", ""
    return str(name), str(domain)


def _last_sub_authority(sid: Any) -> int:
    """Return the last sub-authority of ``sid`` - the RID of a ``S-1-16-<rid>`` mandatory label."""
    try:
        return int(sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1))
    except Exception as exc:
        raise native_error(f"cannot read the integrity label RID: {exc}", exc) from exc


def posix_current_identity(
    *, geteuid: Callable[[], int] | None = None, load: Callable[[str], Any] | None = None
) -> CurrentIdentity:
    """Return the identity of this process's effective uid on POSIX (like ``id -un`` plus ``id -u``).

    The effective uid, not the real one, because it is the uid the kernel checks on each
    access - the POSIX counterpart of the Windows token user.  A uid with no ``pwd`` entry
    keeps its number and gets empty display names, as ``whoami`` cannot name it either.

    Args:
        geteuid: Returns the effective uid; ``None`` uses ``os.geteuid`` (a test passes a stub).
        load: Module loader for ``pwd``; ``None`` imports the real module.

    Returns:
        The euid as ``sid``, its passwd name, and ``is_system``/``elevated`` for euid 0;
        ``integrity_level`` is ``None`` (POSIX has no mandatory label).

    Raises:
        NativeCallError: The user database could not be read.
        PlatformUnsupportedError: Called on Windows (no ``os.geteuid`` / ``pwd``).

    Example:
        >>> import sys
        >>> sys.platform == "win32" or posix_current_identity().sid.isdigit()
        True
    """
    read_euid = geteuid if geteuid is not None else getattr(os, "geteuid", None)
    if read_euid is None:  # pragma: no cover - only on Windows, where the facade never wires this
        raise PlatformUnsupportedError("posix_current_identity needs os.geteuid (POSIX only).")
    euid = int(read_euid())
    pwd = (load if load is not None else _load)("pwd")
    try:
        name = str(pwd.getpwuid(euid).pw_name)
    except KeyError:
        name = ""
    except OSError as exc:
        raise native_error(f"cannot read the user database: {exc}", exc) from exc
    return CurrentIdentity.model_construct(
        sid=str(euid),
        account=name,
        name=name,
        domain="",
        is_system=euid == 0,
        elevated=euid == 0,
        integrity_level=None,
    )


__all__ = ["posix_current_identity", "windows_current_identity"]
