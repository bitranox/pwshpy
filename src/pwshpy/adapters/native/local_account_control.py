"""native local-accounts CONTROL over ``win32net`` / ``win32security`` (Windows-only, **mutating**).

New / Remove a local user or group, add / remove group members, and enable /
disable a user - the mutating counterpart of the read-only
:mod:`~pwshpy.adapters.native.local_accounts` source.  Principals are addressed by
NAME; each returned record carries the resolved SID (canonical, locale-independent;
see ``docs/locale-and-identity.md``).  ``pywin32`` is imported lazily so a portable
install stays clean.  Every native call is wrapped so only
:class:`~pwshpy.domain.errors.PwshPyError` escapes.

**MUTATING** - see CLAUDE.md "Development Safety": real tests create + remove their
own scratch principals (``pwshpy_test_*``) on the disposable throwaway VM.

Contents:
    * :class:`NativeLocalAccountController` - new/remove user+group, membership, enable/disable.
"""

from __future__ import annotations

import importlib
from typing import Any

from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import LocalGroup, LocalUser

_UF_SCRIPT = 0x0001
_UF_ACCOUNTDISABLE = 0x0002
_UF_NORMAL_ACCOUNT = 0x0200
_USER_PRIV_USER = 1


def _load() -> tuple[Any, Any]:
    """Import ``win32net`` + ``win32security`` lazily (absent on a portable install)."""
    try:
        net: Any = importlib.import_module("win32net")
        security: Any = importlib.import_module("win32security")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "Local-account control requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc
    return net, security


def _sid_for(security: Any, name: str) -> str:
    """Resolve a principal name to its string SID, or ``""`` if it cannot be looked up."""
    try:
        sid, _domain, _type = security.LookupAccountName(None, name)
        return str(security.ConvertSidToStringSid(sid))
    except Exception:
        return ""


def _get_user(net: Any, security: Any, name: str) -> LocalUser:
    """Snapshot a user as a :class:`LocalUser` after a mutation."""
    try:
        info: Any = net.NetUserGetInfo(None, name, 2)
    except Exception as exc:
        raise NativeCallError(str(exc) or f"cannot read user {name!r}") from exc
    return LocalUser.model_construct(
        name=name,
        sid=_sid_for(security, name),
        enabled=not (int(info["flags"]) & _UF_ACCOUNTDISABLE),
        full_name=str(info.get("full_name") or ""),
        description=str(info.get("comment") or ""),
    )


class NativeLocalAccountController:
    """Mutating local-accounts control over ``win32net`` (users, groups, membership).

    ``timeout`` is accepted on every verb for parity with the POSIX (shadow-utils) controller, where
    it bounds the external command; the win32net calls are synchronous in-process, so it is ignored.
    """

    def new_user(  # noqa: PLR0913 - a create-user verb legitimately takes name + password/full_name/desc/disabled/timeout
        self,
        name: str,
        *,
        password: str = "",
        full_name: str = "",
        description: str = "",
        disabled: bool = False,
        timeout: float = 30.0,  # accepted for protocol parity; win32net is synchronous so it is unused
    ) -> LocalUser:
        """Create a local user (``NetUserAdd``); return the created :class:`LocalUser`."""
        net, security = _load()
        flags = _UF_SCRIPT | _UF_NORMAL_ACCOUNT | (_UF_ACCOUNTDISABLE if disabled else 0)
        info = {
            "name": name,
            "password": password,
            "priv": _USER_PRIV_USER,
            "home_dir": None,
            "comment": description,
            "flags": flags,
            "script_path": None,
        }
        try:
            net.NetUserAdd(None, 1, info)
            if full_name:
                net.NetUserSetInfo(None, name, 1011, {"full_name": full_name})
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot create user {name!r}") from exc
        return _get_user(net, security, name)

    def remove_user(self, name: str, *, timeout: float = 30.0) -> None:
        """Delete a local user (``NetUserDel``; ``timeout`` unused - synchronous, parity only)."""
        net, _ = _load()
        try:
            net.NetUserDel(None, name)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot remove user {name!r}") from exc

    def set_user_enabled(self, name: str, *, enabled: bool, timeout: float = 30.0) -> LocalUser:
        """Enable or disable a local user (toggles ``UF_ACCOUNTDISABLE``); return the new state."""
        net, security = _load()
        try:
            current: Any = net.NetUserGetInfo(None, name, 1)
            flags = int(current["flags"])
            flags = flags & ~_UF_ACCOUNTDISABLE if enabled else flags | _UF_ACCOUNTDISABLE
            net.NetUserSetInfo(None, name, 1008, {"flags": flags})
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot update user {name!r}") from exc
        return _get_user(net, security, name)

    def new_group(self, name: str, *, description: str = "", timeout: float = 30.0) -> LocalGroup:
        """Create a local group (``NetLocalGroupAdd``); return the created :class:`LocalGroup`."""
        net, security = _load()
        try:
            net.NetLocalGroupAdd(None, 1, {"name": name, "comment": description})
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot create group {name!r}") from exc
        return LocalGroup.model_construct(name=name, sid=_sid_for(security, name), description=description)

    def remove_group(self, name: str, *, timeout: float = 30.0) -> None:
        """Delete a local group (``NetLocalGroupDel``; ``timeout`` unused - synchronous, parity only)."""
        net, _ = _load()
        try:
            net.NetLocalGroupDel(None, name)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot remove group {name!r}") from exc

    def add_group_member(self, group: str, member: str, *, timeout: float = 30.0) -> None:
        """Add ``member`` (a user/group name) to ``group`` (``NetLocalGroupAddMembers``)."""
        net, _ = _load()
        try:
            net.NetLocalGroupAddMembers(None, group, 3, [{"domainandname": member}])
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot add {member!r} to group {group!r}") from exc

    def remove_group_member(self, group: str, member: str, *, timeout: float = 30.0) -> None:
        """Remove ``member`` from ``group`` (``NetLocalGroupDelMembers``)."""
        net, _ = _load()
        try:
            net.NetLocalGroupDelMembers(None, group, [member])
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot remove {member!r} from group {group!r}") from exc


__all__ = ["NativeLocalAccountController"]
