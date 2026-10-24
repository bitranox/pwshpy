"""Windows Update search adapter: marshaling, the COM-error message, and the off-Windows guard.

``os_agnostic`` where the code under test is pure (IUpdate -> record, com_error -> message,
the outcome crossing a real spawn pipe); the COM objects are faked at that true external edge
because pywin32 exists only on Windows.  The live search against the real Windows Update agent is the pwsh oracle in
``test_windows_update_pwsh_oracle.py``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
from bounded_targets import echo

from pwshpy.adapters.native import windows_update
from pwshpy.adapters.native.bounded import run_bounded
from pwshpy.domain.errors import NativeCallError, PlatformUnsupportedError
from pwshpy.domain.records import UpdateCategory, WindowsUpdate

# White-box tests of the adapter's pure helpers (no public path reaches them off Windows).
_Outcome = windows_update._Outcome  # pyright: ignore[reportPrivateUsage]
_to_update = windows_update._to_update  # pyright: ignore[reportPrivateUsage]
_com_failure_message = windows_update._com_failure_message  # pyright: ignore[reportPrivateUsage]
_updates_or_raise = windows_update._updates_or_raise  # pyright: ignore[reportPrivateUsage]

# Real WUA CategoryIDs (read live from the Windows Update agent): the Security Updates
# classification and the Windows 11 product.
_SECURITY_UPDATES = "0fa1201d-4330-4fa8-8ae9-b877473b6441"
_WINDOWS_11 = "72e7624a-5b00-45d2-b92f-e561c0a6a160"


# --- fake IUpdate: the COM edge, shaped like the WUA automation objects ------------------------


@dataclass
class _Collection:
    """An IStringCollection / ICategoryCollection: Count plus Item(i)."""

    items: list[object] = field(default_factory=lambda: [])

    @property
    def Count(self) -> int:  # noqa: N802 - mirrors the COM property name
        return len(self.items)

    def Item(self, index: int) -> object:  # noqa: N802 - mirrors the COM method name
        return self.items[index]


@dataclass
class _Identity:
    UpdateID: str
    RevisionNumber: int


@dataclass
class _Category:
    Name: str
    CategoryID: str
    Type: str


@dataclass
class _FakeUpdate:
    Identity: _Identity
    Title: str
    KBArticleIDs: _Collection
    Categories: _Collection
    MsrcSeverity: str | None
    MaxDownloadSize: Decimal
    IsInstalled: bool = False
    IsDownloaded: bool = False
    IsHidden: bool = True
    IsMandatory: bool = True


def _fake_update() -> _FakeUpdate:
    return _FakeUpdate(
        Identity=_Identity(UpdateID="1a2b", RevisionNumber=201),
        Title="2026-10 Cumulative Update",
        KBArticleIDs=_Collection(["5031455", "5031456"]),
        # WUA hands CategoryID over as the agent stored it; upper case must come out lower.
        Categories=_Collection(
            [
                _Category("Security Updates", _SECURITY_UPDATES.upper(), "UpdateClassification"),
                _Category("Windows 11", _WINDOWS_11, "Product"),
            ]
        ),
        MsrcSeverity=None,
        MaxDownloadSize=Decimal(123456789),
    )


_EXPECTED = WindowsUpdate(
    update_id="1a2b",
    revision_number=201,
    title="2026-10 Cumulative Update",
    kb_article_ids=("5031455", "5031456"),
    is_installed=False,
    is_downloaded=False,
    is_hidden=True,
    is_mandatory=True,
    msrc_severity="",
    max_download_size=123456789,
    categories=("Security Updates", "Windows 11"),
    category_details=(
        UpdateCategory(category_id=_SECURITY_UPDATES, type="UpdateClassification", name="Security Updates"),
        UpdateCategory(category_id=_WINDOWS_11, type="Product", name="Windows 11"),
    ),
)


@pytest.mark.os_agnostic
def test_to_update_reads_every_iupdate_field_into_a_record_of_plain_values() -> None:
    """IUpdate -> record: COM collections become tuples, DECIMAL size an int, a None severity ''."""
    update = _to_update(_fake_update())
    assert type(update) is WindowsUpdate
    assert update == _EXPECTED
    assert type(update.max_download_size) is int
    assert type(update.kb_article_ids) is tuple
    assert type(update.categories) is tuple
    assert type(update.category_details) is tuple


@pytest.mark.os_agnostic
def test_to_update_carries_each_category_as_a_typed_sub_record() -> None:
    """category_details holds one UpdateCategory per category, in the same order as categories."""
    update = _to_update(_fake_update())
    assert all(type(category) is UpdateCategory for category in update.category_details)
    assert tuple(category.name for category in update.category_details) == update.categories


@pytest.mark.os_agnostic
def test_category_details_serialise_as_nested_objects() -> None:
    """--json / --jsonl carry each category as an object with its id, type and name."""
    dumped = _to_update(_fake_update()).to_dict()
    assert dumped["category_details"][0] == {
        "category_id": _SECURITY_UPDATES,
        "type": "UpdateClassification",
        "name": "Security Updates",
    }


@pytest.mark.os_agnostic
def test_an_outcome_of_records_crosses_a_real_spawn_pipe_unchanged() -> None:
    """The child's outcome must pickle by name both ways; a real spawn child echoes it back."""
    outcome = _Outcome(error="", updates=(_to_update(_fake_update()),))
    returned = run_bounded(echo, (outcome,), timeout=30)
    assert type(returned) is _Outcome
    assert returned == outcome
    assert type(returned.updates[0]) is WindowsUpdate
    assert type(returned.updates[0].category_details[0]) is UpdateCategory


@pytest.mark.os_agnostic
def test_windows_update_record_defaults() -> None:
    """Only the identity and title are required; the rest default to empty / False / 0."""
    update = WindowsUpdate(update_id="u", title="t")
    assert update.kb_article_ids == ()
    assert update.categories == ()
    assert update.category_details == ()
    assert update.revision_number == 0
    assert update.max_download_size == 0
    assert update.is_installed is False


# --- COM failure: the child returns the message as data, the parent raises it ------------------


class com_error(Exception):  # noqa: N801, N818 - named like pywintypes.com_error, which the adapter matches by name
    """Shaped like pywintypes.com_error: (hresult, strerror, excepinfo, argerror)."""


@pytest.mark.os_agnostic
def test_com_failure_message_prefers_the_scode_inside_a_dispatch_exception() -> None:
    """DISP_E_EXCEPTION wraps the real WU code in excepinfo; the message names that code."""
    exc = com_error(-2147352567, "Exception occurred.", (0, None, "No network", None, 0, -2145107924), None)
    message = _com_failure_message(exc)
    assert "0x8024402C" in message
    assert "No network" in message
    assert message.startswith("Windows Update search failed")


@pytest.mark.os_agnostic
def test_com_failure_message_uses_the_hresult_when_there_is_no_excepinfo() -> None:
    """A plain com_error (no excepinfo) reports its own HRESULT and text."""
    exc = com_error(-2147024894, "The system cannot find the file specified.", None, None)
    message = _com_failure_message(exc)
    assert "0x80070002" in message
    assert "cannot find the file" in message


@pytest.mark.os_agnostic
def test_updates_or_raise_raises_the_childs_error_as_nativecallerror() -> None:
    """A child outcome carrying an error message becomes a NativeCallError with that exact text."""
    with pytest.raises(NativeCallError) as info:
        _updates_or_raise(_Outcome(error="Windows Update search failed: HRESULT 0x80240032", updates=()))
    assert str(info.value) == "Windows Update search failed: HRESULT 0x80240032"


@pytest.mark.os_agnostic
def test_updates_or_raise_passes_updates_through_on_success() -> None:
    """An outcome with an empty error message hands its updates back unchanged."""
    updates = (_to_update(_fake_update()),)
    assert _updates_or_raise(_Outcome(error="", updates=updates)) is updates


# --- off Windows ------------------------------------------------------------------------------


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the off-Windows guard; Windows runs the real search")
def test_search_updates_off_windows_raises_platform_unsupported_before_spawning() -> None:
    """Off Windows the guard fires first: a spawned child would fail importing pythoncom instead.

    That child failure would surface as a plain NativeCallError, so getting the
    PlatformUnsupportedError type proves no child was started.
    """
    with pytest.raises(PlatformUnsupportedError):
        next(windows_update.search_updates("x", timeout=1))
