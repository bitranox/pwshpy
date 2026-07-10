"""Native local-account mutation on POSIX via shadow-utils (useradd / groupadd / usermod / gpasswd).

The portable counterpart to the win32net local-account controller. Unlike the read side
(``pwd`` / ``grp``) and every other native adapter, account *mutation* has no stable native C API -
so, by deliberate and documented exception, it drives the shadow-utils commands through a
**shell-free argv list** (the same mechanism as ``ps.exec``, no quoting to get wrong, ``stderr``
captured as data). Needs root, like the Windows verbs need admin.

The returned :class:`LocalUser` / :class:`LocalGroup` is read back from ``pwd`` / ``grp`` for its
identity (uid/gid), with the enable state set to the value the verb just applied (the real lock
state lives in root-only ``/etc/shadow``, but the verb knows what it set).

Contents:
    * :class:`PosixLocalAccountController` - new/remove user+group, membership, enable/disable.
"""

from __future__ import annotations

import importlib
import re
from typing import Any

from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import LocalGroup, LocalUser
from .process_exec import run_process as _run_process

#: A portable POSIX login / group name: a lowercase-letter/underscore start, then letters/digits/_/-,
#: an optional trailing '$' (machine accounts), <=32 chars. Excludes ':' newlines and a leading '-',
#: which would otherwise inject a second chpasswd entry or be parsed as a shadow-utils option.
_NAME_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}\$?")

#: Default seconds a shadow-utils command may run before it is killed (guards a wedged /etc/passwd lock);
#: overridable per call via each verb's ``timeout`` parameter.
_DEFAULT_TIMEOUT = 30.0


def _valid_name(value: str) -> str:
    """Return a validated user/group/member name; raise on anything that could inject into a command."""
    if not _NAME_RE.fullmatch(value):
        raise NativeCallError(
            f"invalid account name {value!r}: expected a POSIX name (lowercase letter or '_' start, then "
            "letters/digits/'_'/'-'; no ':', newline, or leading '-')."
        )
    return value


def _reject_delimiters(value: str, field: str, forbidden: str) -> str:
    """Return ``value`` if it contains none of ``forbidden`` (which would corrupt the account database); else raise."""
    if any(ch in value for ch in forbidden):
        raise NativeCallError(
            f"the {field} must not contain any of {forbidden!r} (it would corrupt the account entry)."
        )
    return value


def _load(module_name: str) -> Any:
    """Import a POSIX-only stdlib module (``pwd`` / ``grp``); raise a clear error off POSIX."""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - only if called off POSIX (Windows)
        raise PlatformUnsupportedError(
            "local-account mutation needs Windows (win32net) or POSIX (shadow-utils)."
        ) from exc


def _read_user(name: str, *, enabled: bool) -> LocalUser:
    """Build a :class:`LocalUser` from ``pwd`` after a mutation, with the just-applied enable state."""
    try:
        entry = _load("pwd").getpwnam(name)
    except KeyError as exc:
        raise NativeCallError(f"user {name!r} not found") from exc
    return LocalUser(
        name=str(entry.pw_name),
        sid=str(entry.pw_uid),
        enabled=enabled,
        full_name=str(entry.pw_gecos).split(",", 1)[0],
        description="",
    )


def _read_group(name: str) -> LocalGroup:
    """Build a :class:`LocalGroup` from ``grp`` after a mutation."""
    try:
        entry = _load("grp").getgrnam(name)
    except KeyError as exc:
        raise NativeCallError(f"group {name!r} not found") from exc
    return LocalGroup(name=str(entry.gr_name), sid=str(entry.gr_gid), description="")


class PosixLocalAccountController:
    """Mutating local-account control over shadow-utils (Linux, **mutating**; needs root).

    The counterpart to the win32 NativeLocalAccountController.

    Example:
        >>> callable(PosixLocalAccountController().new_user)
        True
    """

    def new_user(  # noqa: PLR0913 - a create-user verb legitimately takes name + password/full_name/desc/disabled/timeout
        self,
        name: str,
        *,
        password: str = "",
        full_name: str = "",
        description: str = "",
        disabled: bool = False,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> LocalUser:
        """Create a user (``useradd``); optionally set a password, GECOS comment, and lock it."""
        _valid_name(name)
        comment = full_name or description  # POSIX has one GECOS comment, not separate name/description
        _reject_delimiters(comment, "GECOS comment", "\n\r:")
        _reject_delimiters(password, "password", "\n\r")
        argv = ["useradd"]
        if comment:
            argv += ["--comment", comment]
        argv.append(name)
        self._run(argv, timeout=timeout)
        if password:
            self._run(["chpasswd"], input_text=f"{name}:{password}\n", timeout=timeout)
        if disabled:
            self._run(["usermod", "--lock", name], timeout=timeout)
        return _read_user(name, enabled=not disabled)

    def remove_user(self, name: str, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        """Delete a user (``userdel``; the home directory is left in place)."""
        self._run(["userdel", _valid_name(name)], timeout=timeout)

    def set_user_enabled(self, name: str, *, enabled: bool, timeout: float = _DEFAULT_TIMEOUT) -> LocalUser:
        """Unlock (``usermod --unlock``) or lock (``--lock``) a user's password; return its state."""
        self._run(["usermod", "--unlock" if enabled else "--lock", _valid_name(name)], timeout=timeout)
        return _read_user(name, enabled=enabled)

    def new_group(self, name: str, *, description: str = "", timeout: float = _DEFAULT_TIMEOUT) -> LocalGroup:
        """Create a group (``groupadd``). POSIX groups have no description, so ``description`` is ignored."""
        self._run(["groupadd", _valid_name(name)], timeout=timeout)
        return _read_group(name)

    def remove_group(self, name: str, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        """Delete a group (``groupdel``)."""
        self._run(["groupdel", _valid_name(name)], timeout=timeout)

    def add_group_member(self, group: str, member: str, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        """Add a user to a group (``gpasswd --add``)."""
        self._run(["gpasswd", "--add", _valid_name(member), _valid_name(group)], timeout=timeout)

    def remove_group_member(self, group: str, member: str, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        """Remove a user from a group (``gpasswd --delete``)."""
        self._run(["gpasswd", "--delete", _valid_name(member), _valid_name(group)], timeout=timeout)

    def _run(self, argv: list[str], *, input_text: str | None = None, timeout: float) -> None:
        """Run a shadow-utils command shell-free (bounded); raise NativeCallError on nonzero exit or timeout."""
        result = _run_process(argv, input_text=input_text, timeout=timeout)
        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.exit_code}"
            raise NativeCallError(f"{argv[0]} failed: {detail}")


__all__ = ["PosixLocalAccountController"]
