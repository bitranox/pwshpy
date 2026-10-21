"""Principal and trigger value objects for register_scheduled_task (pure domain, os_agnostic)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from pwshpy.domain.enums import TaskLogonType, TaskRunLevel, WellKnownSid
from pwshpy.domain.scheduling import (
    BootTrigger,
    DailyTrigger,
    LogonTrigger,
    OnceTrigger,
    TaskPrincipal,
    TaskTrigger,
)

_AT = datetime(2026, 10, 21, 23, 59, tzinfo=timezone.utc)

# Built with chr() so the source stays ASCII: Arabic-Indic "10" passes str.isdigit() and r"\d".
_ARABIC_INDIC_TEN = chr(0x0661) + chr(0x0660)


@pytest.mark.os_agnostic
def test_default_principal_is_the_interactive_caller() -> None:
    p = TaskPrincipal()
    assert p.user_id is None
    assert p.effective_logon_type() is TaskLogonType.INTERACTIVE_TOKEN
    assert p.run_level is TaskRunLevel.LIMITED


@pytest.mark.os_agnostic
def test_system_principal_is_a_highest_service_account() -> None:
    p = TaskPrincipal.system()
    assert p.user_id == "S-1-5-18"
    assert p.effective_logon_type() is TaskLogonType.SERVICE_ACCOUNT
    assert p.run_level is TaskRunLevel.HIGHEST


@pytest.mark.os_agnostic
def test_a_well_known_sid_member_is_accepted_as_its_value() -> None:
    p = TaskPrincipal(user_id=WellKnownSid.NETWORK_SERVICE)
    assert p.user_id == "S-1-5-20"
    # The stored value is the plain SID string, not the enum member.
    assert type(p.user_id) is str
    assert p.effective_logon_type() is TaskLogonType.SERVICE_ACCOUNT


@pytest.mark.os_agnostic
def test_an_ordinary_user_sid_defaults_to_s4u() -> None:
    p = TaskPrincipal(user_id="S-1-5-21-1-2-3-1001")
    assert p.effective_logon_type() is TaskLogonType.S4U
    assert p.is_sid and not p.is_uid


@pytest.mark.os_agnostic
def test_an_explicit_logon_type_wins_over_the_implied_one() -> None:
    p = TaskPrincipal(user_id="S-1-5-18", logon_type=TaskLogonType.S4U)
    assert p.effective_logon_type() is TaskLogonType.S4U


@pytest.mark.os_agnostic
def test_a_decimal_uid_is_accepted() -> None:
    p = TaskPrincipal(user_id="1000")
    assert p.is_uid and not p.is_sid


@pytest.mark.os_agnostic
@pytest.mark.parametrize("uid", ["0", "1000", "4294967295"])
def test_a_uid_within_uid_t_is_accepted(uid: str) -> None:
    assert TaskPrincipal(user_id=uid).is_uid


@pytest.mark.os_agnostic
@pytest.mark.parametrize("uid", ["01", "007", "4294967296", "9999999999"])
def test_a_zero_padded_or_out_of_range_uid_is_refused(uid: str) -> None:
    # uid_t is 32-bit; "01" would name the same account as "1" under a second spelling.
    with pytest.raises(ValidationError):
        TaskPrincipal(user_id=uid)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "logon", "user": "S-1-5-21-1-2-3-1001"},
        {"kind": "daily", "at": _AT, "every_day": 7},
        {"kind": "once", "at": _AT, "repeat": True},
        {"kind": "boot", "dealy": timedelta(seconds=30)},
    ],
)
def test_the_union_refuses_an_unknown_key(payload: dict[str, object]) -> None:
    # A misspelt key silently dropped would widen the task: "this user logs on" -> "any user".
    adapter: TypeAdapter[TaskTrigger] = TypeAdapter(TaskTrigger)
    with pytest.raises(ValidationError):
        adapter.validate_python(payload)


@pytest.mark.os_agnostic
def test_a_principal_refuses_an_unknown_key() -> None:
    with pytest.raises(ValidationError):
        TaskPrincipal.model_validate({"user_id": "S-1-5-18", "runlevel": "Highest"})


@pytest.mark.os_agnostic
def test_the_interactive_caller_is_neither_sid_nor_uid() -> None:
    p = TaskPrincipal()
    assert not p.is_sid and not p.is_uid


@pytest.mark.os_agnostic
@pytest.mark.parametrize("bad", ["Administrator", "S-1-", "S-1-5-18\n", _ARABIC_INDIC_TEN, "-5", "", "S-1-5-x", 1000])
def test_a_name_or_malformed_identity_is_refused(bad: object) -> None:
    with pytest.raises(ValidationError):
        TaskPrincipal(user_id=bad)  # pyright: ignore[reportArgumentType] - wrong types ARE the input under test


@pytest.mark.os_agnostic
def test_a_logon_trigger_refuses_an_account_name() -> None:
    with pytest.raises(ValidationError):
        LogonTrigger(user_id="Administrator")


@pytest.mark.os_agnostic
def test_a_principal_is_frozen() -> None:
    p = TaskPrincipal()
    with pytest.raises(ValidationError):
        p.run_level = TaskRunLevel.HIGHEST  # pyright: ignore[reportAttributeAccessIssue] - assignment IS the test


@pytest.mark.os_agnostic
def test_a_naive_trigger_time_is_refused() -> None:
    with pytest.raises(ValidationError):
        OnceTrigger(at=datetime(2026, 10, 21, 23, 59))


@pytest.mark.os_agnostic
def test_every_days_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        DailyTrigger(at=_AT, every_days=0)


@pytest.mark.os_agnostic
def test_a_negative_boot_delay_is_refused() -> None:
    with pytest.raises(ValidationError):
        BootTrigger(delay=timedelta(seconds=-1))


@pytest.mark.os_agnostic
def test_the_union_round_trips_by_kind() -> None:
    adapter: TypeAdapter[TaskTrigger] = TypeAdapter(TaskTrigger)
    for trigger in (OnceTrigger(at=_AT), DailyTrigger(at=_AT, every_days=2), BootTrigger(), LogonTrigger()):
        assert type(adapter.validate_python(trigger.model_dump())) is type(trigger)


@pytest.mark.os_agnostic
def test_the_union_refuses_an_unknown_kind() -> None:
    adapter: TypeAdapter[TaskTrigger] = TypeAdapter(TaskTrigger)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "weekly", "at": _AT})
