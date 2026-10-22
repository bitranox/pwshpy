"""Current identity (``ps.get_current_identity``): the process token, never USERNAME/USERDOMAIN.

The Windows path runs everywhere against pywin32 doubles injected through the adapter's
``load`` seam (the same seam the elevation adapter uses), so the token handling, the
error wrapping and the handle release are pinned on Linux CI too.  The POSIX path is
pinned against the real ``os.geteuid`` / ``pwd`` of the machine running the suite.
The real-token Windows cross-checks live in ``test_identity_pwsh_oracle.py``.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli
from pwshpy.adapters.native.identity import posix_current_identity, windows_current_identity
from pwshpy.adapters.native.marshal import to_integrity_level
from pwshpy.composition import AppServices, build_ps
from pwshpy.domain.enums import IntegrityLevel, WellKnownSid
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import CurrentIdentity

# --- pywin32 doubles ---------------------------------------------------------------------------


class _FakePyWinError(Exception):
    """Stands in for ``pywintypes.error``: the adapter must not let it escape unwrapped."""


class _FakeSid:
    """A PySID double carrying its string form; sub-authorities read from that string."""

    def __init__(self, text: str) -> None:
        self.text = text

    def GetSubAuthorityCount(self) -> int:  # noqa: N802 - pywin32's spelling
        return len(self.text.split("-")) - 3

    def GetSubAuthority(self, index: int) -> int:  # noqa: N802 - pywin32's spelling
        return int(self.text.split("-")[3 + index])


class _FakeWin32:
    """One object standing in for win32api, win32con and win32security, recording handle use."""

    TOKEN_QUERY = 0x0008
    TokenUser = 1
    TokenElevation = 20
    TokenIntegrityLevel = 25

    def __init__(
        self,
        *,
        user_sid: str = "S-1-5-18",
        label_sid: str = "S-1-16-16384",
        elevated: int = 1,
        account: tuple[str, str] | None = ("SYSTEM", "NT AUTHORITY"),
        fail: str = "",
    ) -> None:
        self.user_sid = user_sid
        self.label_sid = label_sid
        self.elevated = elevated
        self.account = account
        self.fail = fail
        self.opened: list[int] = []
        self.closed: list[int] = []

    def load(self, _module: str) -> Any:
        return self

    def GetCurrentProcess(self) -> int:  # noqa: N802 - pywin32's spelling
        return -1

    def OpenProcessToken(self, process: int, access: int) -> int:  # noqa: N802 - pywin32's spelling
        assert (process, access) == (-1, self.TOKEN_QUERY)  # read-only access, never more
        if self.fail == "open":
            raise _FakePyWinError("OpenProcessToken: access denied")
        self.opened.append(77)
        return 77

    def GetTokenInformation(self, token: int, info_class: int) -> Any:  # noqa: N802 - pywin32's spelling
        assert token == 77
        if self.fail == "query":
            raise _FakePyWinError("GetTokenInformation: the parameter is incorrect")
        if info_class == self.TokenUser:
            return (_FakeSid(self.user_sid), 0)
        if info_class == self.TokenIntegrityLevel:
            return (_FakeSid(self.label_sid), 0x20)
        assert info_class == self.TokenElevation
        return self.elevated

    def ConvertSidToStringSid(self, sid: _FakeSid) -> str:  # noqa: N802 - pywin32's spelling
        return sid.text

    def LookupAccountSid(self, system: object, sid: _FakeSid) -> tuple[str, str, int]:  # noqa: N802 - pywin32's spelling
        assert system is None and isinstance(sid, _FakeSid)
        if self.account is None:
            raise _FakePyWinError("LookupAccountSid: no mapping between account names and security IDs")
        return (self.account[0], self.account[1], 5)

    def CloseHandle(self, handle: int) -> None:  # noqa: N802 - pywin32's spelling
        self.closed.append(handle)


# --- integrity level mapping (pure) ------------------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("rid", "level"),
    [
        (0x0000, IntegrityLevel.UNTRUSTED),
        (0x0FFF, IntegrityLevel.UNTRUSTED),
        (0x1000, IntegrityLevel.LOW),
        (0x2000, IntegrityLevel.MEDIUM),
        (0x2100, IntegrityLevel.MEDIUM),  # SECURITY_MANDATORY_MEDIUM_PLUS_RID is still medium
        (0x3000, IntegrityLevel.HIGH),
        (0x4000, IntegrityLevel.SYSTEM),
        (0x5000, IntegrityLevel.PROTECTED),
        (0x7FFF, IntegrityLevel.PROTECTED),
    ],
)
def test_integrity_level_follows_the_mandatory_label_thresholds(rid: int, level: IntegrityLevel) -> None:
    """A RID maps to the band it falls in, so an in-between RID (medium plus) is never 'unknown'."""
    assert to_integrity_level(rid) is level


@pytest.mark.os_agnostic
def test_integrity_level_values_are_english() -> None:
    """The enum carries canonical English values, never a localized label text."""
    assert [level.value for level in IntegrityLevel] == ["Untrusted", "Low", "Medium", "High", "System", "Protected"]


# --- Windows path through injected pywin32 doubles ---------------------------------------------


@pytest.mark.os_agnostic
def test_a_system_token_reads_as_local_system() -> None:
    """A SYSTEM token yields S-1-5-18, is_system, elevated and the System integrity level."""
    fake = _FakeWin32()
    identity = windows_current_identity(load=fake.load)
    assert identity == CurrentIdentity(
        sid=WellKnownSid.LOCAL_SYSTEM.value,
        account="NT AUTHORITY\\SYSTEM",
        name="SYSTEM",
        domain="NT AUTHORITY",
        is_system=True,
        elevated=True,
        integrity_level=IntegrityLevel.SYSTEM,
    )
    assert fake.closed == fake.opened == [77]


@pytest.mark.os_agnostic
def test_a_filtered_user_token_reads_medium_and_not_elevated() -> None:
    """An ordinary user's filtered token: the user's SID, medium integrity, not elevated, not SYSTEM."""
    fake = _FakeWin32(user_sid="S-1-5-21-1-2-3-1001", label_sid="S-1-16-8192", elevated=0, account=("srvadmin", "PC1"))
    identity = windows_current_identity(load=fake.load)
    assert (identity.sid, identity.account, identity.is_system) == ("S-1-5-21-1-2-3-1001", "PC1\\srvadmin", False)
    assert (identity.elevated, identity.integrity_level) == (False, IntegrityLevel.MEDIUM)


@pytest.mark.os_agnostic
def test_an_unresolvable_account_keeps_the_sid_and_blanks_the_display_names() -> None:
    """The name is display only: a SID LookupAccountSid cannot map still identifies the caller."""
    fake = _FakeWin32(user_sid="S-1-5-21-9-9-9-1234", label_sid="S-1-16-12288", account=None)
    identity = windows_current_identity(load=fake.load)
    assert (identity.sid, identity.account, identity.name, identity.domain) == ("S-1-5-21-9-9-9-1234", "", "", "")
    assert identity.integrity_level is IntegrityLevel.HIGH
    assert fake.closed == [77]


@pytest.mark.os_agnostic
def test_a_refused_token_open_raises_native_call_error() -> None:
    """OpenProcessToken failing surfaces as NativeCallError, and nothing is closed that never opened."""
    fake = _FakeWin32(fail="open")
    with pytest.raises(NativeCallError, match="OpenProcessToken"):
        windows_current_identity(load=fake.load)
    assert fake.closed == []


@pytest.mark.os_agnostic
def test_a_failed_token_query_raises_and_still_closes_the_handle() -> None:
    """GetTokenInformation failing raises NativeCallError and the token handle is released."""
    fake = _FakeWin32(fail="query")
    with pytest.raises(NativeCallError, match="GetTokenInformation"):
        windows_current_identity(load=fake.load)
    assert fake.closed == [77]


# --- POSIX path ---------------------------------------------------------------------------------


def _fake_pwd(names: dict[int, str]) -> Callable[[str], Any]:
    def getpwuid(uid: int) -> SimpleNamespace:
        if uid not in names:
            raise KeyError(f"getpwuid(): uid not found: {uid}")
        return SimpleNamespace(pw_name=names[uid])

    return lambda _module: SimpleNamespace(getpwuid=getpwuid)


@pytest.mark.os_agnostic
def test_posix_root_is_system_and_elevated() -> None:
    """euid 0 is the POSIX SYSTEM: is_system and elevated, no integrity level (Windows-only concept)."""
    identity = posix_current_identity(geteuid=lambda: 0, load=_fake_pwd({0: "root"}))
    assert identity == CurrentIdentity(
        sid="0", account="root", name="root", domain="", is_system=True, elevated=True, integrity_level=None
    )


@pytest.mark.os_agnostic
def test_posix_uid_without_a_passwd_entry_keeps_the_uid() -> None:
    """A uid with no passwd entry (a container's arbitrary uid) still answers, with no name."""
    identity = posix_current_identity(geteuid=lambda: 4242, load=_fake_pwd({}))
    assert (identity.sid, identity.account, identity.name) == ("4242", "", "")
    assert (identity.is_system, identity.elevated) == (False, False)


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="os.geteuid and pwd exist only on POSIX")
def test_posix_live_identity_matches_geteuid_and_pwd() -> None:
    """On this machine the record is the effective uid and its passwd name, read for real."""
    # The skipif does not narrow for pyright, which checks one platform per run; this assert
    # does, so a Windows-platform type check sees no POSIX-only name below it.
    assert sys.platform != "win32"
    # Imported here, not at module top: pwd is POSIX-only and would break collection on Windows.
    import pwd

    euid = os.geteuid()
    identity = build_ps().get_current_identity()
    assert identity.sid == str(euid)
    assert identity.name == pwd.getpwuid(euid).pw_name
    assert identity.account == identity.name
    assert identity.is_system is identity.elevated is (euid == 0)
    assert identity.integrity_level is None


# --- CLI ------------------------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_get_current_identity_cli_jsonl(cli_runner: CliRunner, production_factory: Callable[[], AppServices]) -> None:
    """``get_current_identity --jsonl`` emits one record carrying the facade's SID."""
    result = cli_runner.invoke(cli, ["get_current_identity", "--jsonl"], obj=production_factory)
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["sid"] == build_ps().get_current_identity().sid
    assert set(rows[0]) == {"sid", "account", "name", "domain", "is_system", "elevated", "integrity_level"}


@pytest.mark.os_agnostic
def test_get_current_identity_cli_table(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default human table renders the record whole and exits 0."""
    # No COLUMNS: a piped table is sized to its content, so a 46-character domain SID (which an
    # 80-column fallback cut with an ellipsis, measured on Windows) must appear whole without one.
    monkeypatch.delenv("COLUMNS", raising=False)
    result = cli_runner.invoke(cli, ["get_current_identity"], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert build_ps().get_current_identity().sid in result.stdout
