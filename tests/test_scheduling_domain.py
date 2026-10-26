"""Principal, trigger and settings value objects for register_scheduled_task (pure domain, os_agnostic)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

import pwshpy
from pwshpy.domain.enums import TaskLogonType, TaskMultipleInstances, TaskRunLevel, TaskState, WellKnownSid
from pwshpy.domain.records import ScheduledTaskInfo
from pwshpy.domain.scheduling import (
    BootTrigger,
    DailyTrigger,
    LogonTrigger,
    OnceTrigger,
    TaskPrincipal,
    TaskSettings,
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
    with pytest.raises(ValidationError, match="never an account name"):
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
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        adapter.validate_python(payload)


@pytest.mark.os_agnostic
def test_a_principal_refuses_an_unknown_key() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskPrincipal.model_validate({"user_id": "S-1-5-18", "runlevel": "Highest"})


@pytest.mark.os_agnostic
def test_the_interactive_caller_is_neither_sid_nor_uid() -> None:
    p = TaskPrincipal()
    assert not p.is_sid and not p.is_uid


@pytest.mark.os_agnostic
@pytest.mark.parametrize("bad", ["Administrator", "S-1-", "S-1-5-18\n", _ARABIC_INDIC_TEN, "-5", "", "S-1-5-x", 1000])
def test_a_name_or_malformed_identity_is_refused(bad: object) -> None:
    with pytest.raises(ValidationError, match="never an account name"):
        TaskPrincipal(user_id=bad)  # pyright: ignore[reportArgumentType] - wrong types ARE the input under test


@pytest.mark.os_agnostic
def test_a_logon_trigger_refuses_an_account_name() -> None:
    with pytest.raises(ValidationError, match="never an account name"):
        LogonTrigger(user_id="Administrator")


@pytest.mark.os_agnostic
def test_a_principal_is_frozen() -> None:
    p = TaskPrincipal()
    with pytest.raises(ValidationError, match="Instance is frozen"):
        p.run_level = TaskRunLevel.HIGHEST  # pyright: ignore[reportAttributeAccessIssue] - assignment IS the test


@pytest.mark.os_agnostic
def test_a_naive_trigger_time_is_refused() -> None:
    with pytest.raises(ValidationError, match="should have timezone info"):
        OnceTrigger(at=datetime(2026, 10, 21, 23, 59))


@pytest.mark.os_agnostic
def test_every_days_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        DailyTrigger(at=_AT, every_days=0)


@pytest.mark.os_agnostic
def test_a_negative_boot_delay_is_refused() -> None:
    with pytest.raises(ValidationError, match="delay must not be negative"):
        BootTrigger(delay=timedelta(seconds=-1))


@pytest.mark.os_agnostic
def test_the_union_round_trips_by_kind() -> None:
    adapter: TypeAdapter[TaskTrigger] = TypeAdapter(TaskTrigger)
    for trigger in (OnceTrigger(at=_AT), DailyTrigger(at=_AT, every_days=2), BootTrigger(), LogonTrigger()):
        assert type(adapter.validate_python(trigger.model_dump())) is type(trigger)


@pytest.mark.os_agnostic
def test_the_union_refuses_an_unknown_kind() -> None:
    adapter: TypeAdapter[TaskTrigger] = TypeAdapter(TaskTrigger)
    with pytest.raises(ValidationError, match="does not match any of the expected tags"):
        adapter.validate_python({"kind": "weekly", "at": _AT})


@pytest.mark.os_agnostic
def test_empty_task_settings_change_nothing() -> None:
    # Unset means untouched: every field None, so no adapter writes a setting.
    s = TaskSettings()
    assert s.model_dump() == {
        "allow_start_if_on_batteries": None,
        "stop_if_going_on_batteries": None,
        "execution_time_limit": None,
        "multiple_instances": None,
        "allow_hard_terminate": None,
        "allow_demand_start": None,
    }


@pytest.mark.os_agnostic
@pytest.mark.parametrize("allowed", [True, False])
def test_allow_demand_start_takes_a_bool(allowed: bool) -> None:
    assert TaskSettings(allow_demand_start=allowed).allow_demand_start is allowed


@pytest.mark.os_agnostic
def test_task_settings_refuse_an_unknown_key() -> None:
    # A misspelt key silently dropped would leave the OS default the caller meant to change.
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskSettings.model_validate({"allow_hard_terminate": True, "multiple_instance": "IgnoreNew"})


@pytest.mark.os_agnostic
def test_a_zero_execution_time_limit_means_no_limit_and_is_accepted() -> None:
    assert TaskSettings(execution_time_limit=timedelta(0)).execution_time_limit == timedelta(0)


@pytest.mark.os_agnostic
def test_a_whole_second_execution_time_limit_is_accepted() -> None:
    assert TaskSettings(execution_time_limit=timedelta(hours=72)).execution_time_limit == timedelta(hours=72)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("limit", [timedelta(seconds=-1), timedelta(microseconds=-1), timedelta(days=-3)])
def test_a_negative_execution_time_limit_is_refused(limit: timedelta) -> None:
    with pytest.raises(ValidationError, match="execution_time_limit must not be negative"):
        TaskSettings(execution_time_limit=limit)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("limit", [timedelta(seconds=1, microseconds=1), timedelta(milliseconds=500), 1.5])
def test_a_sub_second_execution_time_limit_is_refused(limit: object) -> None:
    # Task Scheduler and systemd are written in whole seconds; a fraction would be dropped silently.
    with pytest.raises(ValidationError, match="execution_time_limit must be whole seconds"):
        TaskSettings(execution_time_limit=limit)  # pyright: ignore[reportArgumentType] - a float IS the input under test


@pytest.mark.os_agnostic
def test_the_largest_execution_time_limit_is_accepted() -> None:
    limit = timedelta(seconds=2147483647)
    assert TaskSettings(execution_time_limit=limit).execution_time_limit == limit


@pytest.mark.os_agnostic
@pytest.mark.parametrize("seconds", [2147483648, 4294967295, 4294967296])
def test_an_execution_time_limit_past_int32_is_refused(seconds: int) -> None:
    # Measured on Windows: 2**31..2**32-1 is refused (0x80041318) and 2**32 wraps to PT0S = no limit.
    with pytest.raises(ValidationError, match="execution_time_limit must not exceed 2147483647 seconds"):
        TaskSettings(execution_time_limit=timedelta(seconds=seconds))


@pytest.mark.os_agnostic
def test_task_settings_are_frozen() -> None:
    s = TaskSettings()
    with pytest.raises(ValidationError, match="Instance is frozen"):
        s.allow_hard_terminate = True  # pyright: ignore[reportAttributeAccessIssue] - assignment IS the test


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("member", "value"),
    [
        (TaskMultipleInstances.PARALLEL, "Parallel"),
        (TaskMultipleInstances.QUEUE, "Queue"),
        (TaskMultipleInstances.IGNORE_NEW, "IgnoreNew"),
        (TaskMultipleInstances.STOP_EXISTING, "StopExisting"),
    ],
)
def test_multiple_instances_values_mirror_powershell(member: TaskMultipleInstances, value: str) -> None:
    assert member.value == value
    assert TaskSettings.model_validate({"multiple_instances": value}).multiple_instances is member


@pytest.mark.os_agnostic
def test_multiple_instances_has_exactly_the_four_policies() -> None:
    assert [m.value for m in TaskMultipleInstances] == ["Parallel", "Queue", "IgnoreNew", "StopExisting"]


@pytest.mark.os_agnostic
def test_an_unknown_multiple_instances_policy_is_refused() -> None:
    with pytest.raises(ValidationError, match="Input should be 'Parallel'"):
        TaskSettings.model_validate({"multiple_instances": "Ignore"})


@pytest.mark.os_agnostic
def test_a_scheduled_task_record_has_no_settings_by_default() -> None:
    t = ScheduledTaskInfo(task_name="Backup", task_path="\\", state=TaskState.READY)
    assert t.settings is None


@pytest.mark.os_agnostic
def test_a_scheduled_task_record_carries_settings() -> None:
    s = TaskSettings(multiple_instances=TaskMultipleInstances.IGNORE_NEW, allow_hard_terminate=True)
    t = ScheduledTaskInfo(task_name="Backup", task_path="\\", state=TaskState.READY, settings=s)
    assert t.settings == s
    assert t.model_dump(mode="json")["settings"]["multiple_instances"] == "IgnoreNew"


@pytest.mark.os_agnostic
def test_the_package_exports_the_task_settings_types() -> None:
    assert pwshpy.TaskSettings is TaskSettings
    assert pwshpy.TaskMultipleInstances is TaskMultipleInstances
    assert {"TaskSettings", "TaskMultipleInstances"} <= set(pwshpy.__all__)
