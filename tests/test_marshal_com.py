"""native marshaling seam: recognising a pywin32 COM error and reading its unsigned HRESULT.

pywin32 raises ``pywintypes.com_error`` (and ``pywintypes.error``), importable only on Windows,
so the seam recognises them by class name; the doubles below carry those names and run on any
OS. pywin32 reports an HRESULT signed (0x80070002 arrives as -2147024894).
"""

from __future__ import annotations

import pytest

from pwshpy.adapters.native.marshal import com_hresult, is_com_error


class com_error(Exception):  # noqa: N801, N818 - pywin32's class name is what the seam recognises
    """A stand-in for ``pywintypes.com_error``: the HRESULT on ``.hresult``, signed."""

    def __init__(self, hresult: object) -> None:
        super().__init__(hresult, "localized text nobody may rely on", None, None)
        self.hresult = hresult


class error(Exception):  # noqa: N801, N818 - pywin32's class name is what the seam recognises
    """A stand-in for ``pywintypes.error``; ``hresult`` set only when a test gives one."""

    def __init__(self, hresult: int | None = None) -> None:
        super().__init__("a Win32 failure")
        if hresult is not None:
            self.hresult = hresult


class _NotComError(RuntimeError):
    """Some other exception that happens to carry an ``hresult`` attribute."""

    hresult = -2147024894


def _signed(code: int) -> int:
    return code - 0x1_0000_0000 if code & 0x8000_0000 else code


@pytest.mark.os_agnostic
@pytest.mark.parametrize("code", [0x80070002, 0x80004005, 0x800401F0, 0xFFFFFFFF, 0x00000001, 0x0])
def test_com_hresult_reads_the_signed_hresult_back_unsigned(code: int) -> None:
    assert com_hresult(com_error(_signed(code))) == code


@pytest.mark.os_agnostic
def test_com_hresult_accepts_an_already_unsigned_hresult() -> None:
    assert com_hresult(com_error(0x80070003)) == 0x80070003


@pytest.mark.os_agnostic
def test_com_hresult_reads_a_pywintypes_error_that_carries_an_hresult() -> None:
    assert com_hresult(error(_signed(0x8007007A))) == 0x8007007A


@pytest.mark.os_agnostic
@pytest.mark.parametrize("hresult", [None, "0x80070002", 3.0, True])
def test_com_hresult_is_none_when_the_com_error_carries_no_int_hresult(hresult: object) -> None:
    assert com_hresult(com_error(hresult)) is None


@pytest.mark.os_agnostic
def test_com_hresult_is_none_when_the_attribute_is_missing() -> None:
    assert com_hresult(error()) is None


@pytest.mark.os_agnostic
def test_com_hresult_is_none_for_an_exception_that_is_not_a_com_error() -> None:
    """An ``.hresult`` on some other exception is not a COM status: the name decides."""
    assert com_hresult(_NotComError("not COM")) is None


@pytest.mark.os_agnostic
def test_is_com_error_recognises_both_pywin32_class_names_only() -> None:
    assert is_com_error(com_error(1))
    assert is_com_error(error())
    assert not is_com_error(OSError("x"))
    assert not is_com_error(RuntimeError("com_error"))
