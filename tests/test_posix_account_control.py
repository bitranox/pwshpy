"""POSIX local-account controller: os_agnostic argv checks with a monkeypatched runner.

The verbs shell out to shadow-utils through ``run_process``; here we patch that runner to capture
the argv (no root, no real accounts) and assert each verb issues the right shell-free command, and
that a nonzero exit becomes a NativeCallError. The live round-trip is in
``test_posix_account_controller.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pwshpy.adapters.native import posix_account_control as pac
from pwshpy.adapters.native.posix_account_control import PosixLocalAccountController
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import ProcessResult


@pytest.fixture
def captured() -> Iterator[list[list[str]]]:
    """Patch the process runner to record argv and always succeed; yields the recorded calls."""
    calls: list[list[str]] = []

    def fake(argv: list[str], *, input_text: str | None = None, timeout: float | None = None) -> ProcessResult:
        calls.append(list(argv))
        return ProcessResult(argv=list(argv), exit_code=0, stdout="", stderr="", duration_s=0.0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pac, "_run_process", fake)
        yield calls


@pytest.mark.os_agnostic
def test_remove_user_argv(captured: list[list[str]]) -> None:
    PosixLocalAccountController().remove_user("bob")
    assert captured == [["userdel", "bob"]]


@pytest.mark.os_agnostic
def test_remove_group_argv(captured: list[list[str]]) -> None:
    PosixLocalAccountController().remove_group("devs")
    assert captured == [["groupdel", "devs"]]


@pytest.mark.os_agnostic
def test_add_group_member_argv(captured: list[list[str]]) -> None:
    PosixLocalAccountController().add_group_member("devs", "bob")
    assert captured == [["gpasswd", "--add", "bob", "devs"]]


@pytest.mark.os_agnostic
def test_remove_group_member_argv(captured: list[list[str]]) -> None:
    PosixLocalAccountController().remove_group_member("devs", "bob")
    assert captured == [["gpasswd", "--delete", "bob", "devs"]]


@pytest.mark.os_agnostic
def test_nonzero_exit_raises() -> None:
    """A shadow-utils command exiting nonzero surfaces as a NativeCallError with its stderr."""

    def fake(argv: list[str], *, input_text: str | None = None, timeout: float | None = None) -> ProcessResult:
        return ProcessResult(
            argv=list(argv), exit_code=6, stdout="", stderr="groupdel: group 'nope' does not exist", duration_s=0.0
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pac, "_run_process", fake)
        with pytest.raises(NativeCallError, match="groupdel failed: groupdel: group 'nope'"):
            PosixLocalAccountController().remove_group("nope")


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("kwargs", "expected"), [({}, 30.0), ({"timeout": 5.0}, 5.0)])
def test_timeout_is_forwarded(kwargs: dict[str, float], expected: float) -> None:
    """The per-verb ``timeout`` (default 30s) reaches run_process, so a wedged command is bounded."""
    seen: list[float | None] = []

    def fake(argv: list[str], *, input_text: str | None = None, timeout: float | None = None) -> ProcessResult:
        seen.append(timeout)
        return ProcessResult(argv=list(argv), exit_code=0, stdout="", stderr="", duration_s=0.0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pac, "_run_process", fake)
        PosixLocalAccountController().remove_group("devs", **kwargs)
    assert seen == [expected]


@pytest.mark.os_agnostic
def test_newline_password_is_rejected_before_running(captured: list[list[str]]) -> None:
    """A newline in the password (a chpasswd second-entry injection) is rejected before any command runs."""
    with pytest.raises(NativeCallError, match="password must not contain"):
        PosixLocalAccountController().new_user("alice", password="foo\nroot:pwned")
    assert captured == []  # nothing was spawned - useradd never ran


@pytest.mark.os_agnostic
def test_colon_in_gecos_comment_is_rejected(captured: list[list[str]]) -> None:
    """A colon/newline in the GECOS comment (which would corrupt /etc/passwd) is rejected."""
    with pytest.raises(NativeCallError, match="GECOS comment must not contain"):
        PosixLocalAccountController().new_user("alice", full_name="Alice:root:0:0")
    assert captured == []


@pytest.mark.os_agnostic
@pytest.mark.parametrize("bad", ["-G", "root:x", "a b", "foo\nbar", "1abc", ""])
def test_unsafe_names_are_rejected(bad: str, captured: list[list[str]]) -> None:
    """A name that could inject a shadow-utils option or a database delimiter is rejected before running."""
    with pytest.raises(NativeCallError, match="invalid account name"):
        PosixLocalAccountController().remove_user(bad)
    assert captured == []
