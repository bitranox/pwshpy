"""Principal and trigger value objects for registering a scheduled task (pure domain).

These are INPUTS to ``register_scheduled_task``, validated at the boundary, so they are
plain frozen Pydantic models rather than :class:`~pwshpy.domain.records.PSRecord` output
records.  A principal is identified by SID (Windows) or decimal uid (POSIX), never by a
localized account name - see ``docs/locale-and-identity.md``.

Contents:
    * :class:`TaskPrincipal` - who the task runs as, and with which token.
    * :class:`OnceTrigger`, :class:`DailyTrigger`, :class:`BootTrigger`, :class:`LogonTrigger`.
    * :data:`TaskTrigger` - the discriminated union of the four.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from .enums import TaskLogonType, TaskRunLevel, WellKnownSid

# fullmatch + [0-9]: '$' would accept a trailing newline and '\d' accepts non-ASCII digits.
_SID = re.compile(r"S-1-[0-9]+(?:-[0-9]+)+")
# No leading zeros: "01" and "1" would be two spellings of one account.
_UID = re.compile(r"0|[1-9][0-9]{0,9}")
# uid_t is an unsigned 32-bit integer on every POSIX target pwshpy supports.
_UID_MAX = 4294967295
_SERVICE_SIDS = frozenset(
    {
        WellKnownSid.LOCAL_SYSTEM.value,
        WellKnownSid.LOCAL_SERVICE.value,
        WellKnownSid.NETWORK_SERVICE.value,
    }
)

# extra="forbid": these are user-supplied inputs, and a silently dropped misspelt key widens
# the task (LogonTrigger "user" instead of "user_id" would mean "any user logs on").
_INPUT_CONFIG = ConfigDict(frozen=True, extra="forbid")


def _is_uid(text: str) -> bool:
    """True when ``text`` is a canonical decimal uid within ``uid_t``.

    The regex bounds the length to 10 ASCII digits first, so ``int()`` never sees a long input.

    Example:
        >>> _is_uid("4294967295"), _is_uid("4294967296"), _is_uid("01")
        (True, False, False)
    """
    return _UID.fullmatch(text) is not None and int(text) <= _UID_MAX


def _identity(value: object) -> str | None:
    """Accept a SID or decimal uid (or a :class:`WellKnownSid` member) and return it as plain text.

    A localized account name is refused rather than resolved: the same account reads
    ``Administrators`` on one machine and ``Administratoren`` on the next.

    Raises:
        ValueError: ``value`` is not None, a :class:`WellKnownSid`, a SID string or a canonical
            decimal uid within ``uid_t`` (account names, zero-padded or out-of-range uids,
            non-string values).  Pydantic reports it as a ``ValidationError``.

    Example:
        >>> _identity(WellKnownSid.LOCAL_SYSTEM)
        'S-1-5-18'
        >>> _identity("1000")
        '1000'
    """
    if value is None:
        return None
    text = value.value if isinstance(value, WellKnownSid) else value
    if isinstance(text, str) and (_SID.fullmatch(text) or _is_uid(text)):
        return text
    raise ValueError("must be a SID (S-1-...) or a decimal uid, never an account name")


class TaskPrincipal(BaseModel):
    """Who a scheduled task runs as.

    ``user_id`` None means the interactive caller.  A service SID
    (SYSTEM, LOCAL SERVICE, NETWORK SERVICE) logs on as a service account; any other SID
    defaults to S4U (runs whether or not the user is logged on, no stored password).

    Example:
        >>> TaskPrincipal.system().user_id
        'S-1-5-18'
    """

    model_config = _INPUT_CONFIG

    user_id: str | None = None
    logon_type: TaskLogonType | None = None
    run_level: TaskRunLevel = TaskRunLevel.LIMITED

    @field_validator("user_id", mode="before")
    @classmethod
    def _check_user_id(cls, value: object) -> str | None:
        return _identity(value)

    @classmethod
    def system(cls) -> TaskPrincipal:
        """SYSTEM at the highest run level - the shape a multi-hour unattended job needs.

        Example:
            >>> TaskPrincipal.system().run_level.value
            'Highest'
        """
        return cls(user_id=WellKnownSid.LOCAL_SYSTEM.value, run_level=TaskRunLevel.HIGHEST)

    @property
    def is_sid(self) -> bool:
        """True when ``user_id`` is a Windows SID.

        Example:
            >>> TaskPrincipal(user_id="S-1-5-18").is_sid
            True
        """
        return self.user_id is not None and _SID.fullmatch(self.user_id) is not None

    @property
    def is_uid(self) -> bool:
        """True when ``user_id`` is a decimal POSIX uid.

        Example:
            >>> TaskPrincipal(user_id="1000").is_uid
            True
        """
        return self.user_id is not None and _is_uid(self.user_id)

    @property
    def is_service_account(self) -> bool:
        """True when ``user_id`` is a service account SID (SYSTEM, LOCAL SERVICE, NETWORK SERVICE).

        Example:
            >>> TaskPrincipal(user_id="S-1-5-20").is_service_account, TaskPrincipal().is_service_account
            (True, False)
        """
        return self.user_id in _SERVICE_SIDS

    def effective_logon_type(self) -> TaskLogonType:
        """The explicit logon type, else the one implied by ``user_id``.

        Example:
            >>> TaskPrincipal().effective_logon_type().value
            'InteractiveToken'
        """
        if self.logon_type is not None:
            return self.logon_type
        if self.user_id is None:
            return TaskLogonType.INTERACTIVE_TOKEN
        if self.is_service_account:
            return TaskLogonType.SERVICE_ACCOUNT
        return TaskLogonType.S4U


class OnceTrigger(BaseModel):
    """Run once at ``at`` (timezone-aware).

    Example:
        >>> from datetime import datetime, timezone
        >>> OnceTrigger(at=datetime(2026, 1, 1, tzinfo=timezone.utc)).kind
        'once'
    """

    model_config = _INPUT_CONFIG
    kind: Literal["once"] = "once"
    at: AwareDatetime


class DailyTrigger(BaseModel):
    """Run every ``every_days`` days at the time of day of ``at``, starting at ``at``.

    Example:
        >>> from datetime import datetime, timezone
        >>> DailyTrigger(at=datetime(2026, 1, 1, 3, tzinfo=timezone.utc)).every_days
        1
    """

    model_config = _INPUT_CONFIG
    kind: Literal["daily"] = "daily"
    at: AwareDatetime
    every_days: int = Field(default=1, ge=1, le=365)


class BootTrigger(BaseModel):
    """Run when the machine boots, after ``delay``.

    Example:
        >>> BootTrigger().delay.total_seconds()
        0.0
    """

    model_config = _INPUT_CONFIG
    kind: Literal["boot"] = "boot"
    delay: timedelta = timedelta(0)

    @field_validator("delay")
    @classmethod
    def _non_negative(cls, value: timedelta) -> timedelta:
        if value < timedelta(0):
            raise ValueError("delay must not be negative")
        return value


class LogonTrigger(BaseModel):
    """Run when ``user_id`` (a SID) logs on; None means any user (Windows-only).

    Example:
        >>> LogonTrigger().user_id is None
        True
    """

    model_config = _INPUT_CONFIG
    kind: Literal["logon"] = "logon"
    user_id: str | None = None

    @field_validator("user_id", mode="before")
    @classmethod
    def _check_user_id(cls, value: object) -> str | None:
        return _identity(value)

    @property
    def is_uid(self) -> bool:
        """True when ``user_id`` is a decimal POSIX uid.

        Example:
            >>> LogonTrigger(user_id="1000").is_uid, LogonTrigger(user_id="S-1-5-18").is_uid
            (True, False)
        """
        return self.user_id is not None and _is_uid(self.user_id)


# PEP 604 '|' between classes is a runtime types.UnionType from 3.10 on, which is this
# package's floor, and Pydantic accepts it as a discriminated union.
TaskTrigger = Annotated[OnceTrigger | DailyTrigger | BootTrigger | LogonTrigger, Field(discriminator="kind")]

__all__ = ["BootTrigger", "DailyTrigger", "LogonTrigger", "OnceTrigger", "TaskPrincipal", "TaskTrigger"]
