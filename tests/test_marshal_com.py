"""native marshaling seam: recognising a pywin32 COM error, reading its HRESULT and its text.

pywin32 raises ``pywintypes.com_error`` and ``pywintypes.error``, importable only on Windows, so
the seam recognises them by class name and module; the doubles in ``com_fakes`` carry both and
run on any OS. pywin32 reports an HRESULT signed (0x80070002 arrives as -2147024894), a
``pywintypes.error`` carries a Win32 code on ``.winerror`` (no ``.hresult``), and a late-bound
IDispatch failure arrives as DISP_E_EXCEPTION with the callee's own status in ``excepinfo[5]``.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable

import pytest
from com_fakes import DISP_E_EXCEPTION, codeless_com_error, com_error, dispatch_error, error, signed

from pwshpy.adapters.native.marshal import com_error_text, com_hresult, com_outer_hresult, is_com_error

_DISP = 0x80020009


def _raised(fail: Callable[[], object]) -> BaseException:
    """The exception ``fail`` really raises."""
    try:
        fail()
    except Exception as exc:
        return exc
    raise AssertionError("expected the call to raise")


# Real stdlib exceptions whose class is also named ``error``: the bare name must not admit them.
_FOREIGN_ERRORS = [_raised(lambda: struct.unpack("i", b"")), _raised(lambda: zlib.decompress(b"x"))]


class _NotComError(RuntimeError):
    """Some other exception that happens to carry an ``hresult`` attribute."""

    hresult = -2147024894


class _ElsewhereComError(Exception):
    """Named like pywin32's class, but defined outside ``pywintypes``."""

    hresult = -2147024894


_ElsewhereComError.__name__ = "com_error"


# --- is_com_error -----------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_is_com_error_recognises_both_pywintypes_classes() -> None:
    assert is_com_error(com_error(1))
    assert is_com_error(error(5))


@pytest.mark.os_agnostic
@pytest.mark.parametrize("exc", _FOREIGN_ERRORS, ids=lambda exc: type(exc).__module__)
def test_is_com_error_refuses_a_stdlib_class_that_is_also_named_error(exc: BaseException) -> None:
    assert type(exc).__name__ == "error"
    assert not is_com_error(exc)
    assert com_hresult(exc) is None
    assert com_error_text(exc) == ""


@pytest.mark.os_agnostic
def test_is_com_error_refuses_the_name_from_another_module() -> None:
    assert not is_com_error(_ElsewhereComError())
    assert com_hresult(_ElsewhereComError()) is None


@pytest.mark.os_agnostic
def test_is_com_error_refuses_other_exceptions() -> None:
    assert not is_com_error(OSError("x"))
    assert not is_com_error(RuntimeError("com_error"))


# --- com_hresult: com_error -------------------------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize("code", [0x80070002, 0x80004005, 0x800401F0, 0xFFFFFFFF, 0x00000001, 0x0])
def test_com_hresult_gives_the_same_unsigned_code_for_signed_and_unsigned_input(code: int) -> None:
    assert com_hresult(com_error(signed(code))) == code
    assert com_hresult(com_error(code)) == code


@pytest.mark.os_agnostic
@pytest.mark.parametrize("hresult", [None, "0x80070002", 3.0, True, False])
def test_com_hresult_is_none_when_the_com_error_carries_no_int_hresult(hresult: object) -> None:
    assert com_hresult(com_error(hresult)) is None


@pytest.mark.os_agnostic
def test_com_hresult_is_none_when_no_code_is_readable() -> None:
    assert com_hresult(codeless_com_error()) is None


@pytest.mark.os_agnostic
def test_com_hresult_is_none_for_an_exception_that_is_not_a_com_error() -> None:
    """An ``.hresult`` on some other exception is not a COM status: the class decides."""
    assert com_hresult(_NotComError("not COM")) is None


@pytest.mark.os_agnostic
def test_com_hresult_falls_back_to_args_when_the_attribute_is_missing() -> None:
    failure = com_error(signed(0x80070005))
    del failure.hresult
    assert com_hresult(failure) == 0x80070005


@pytest.mark.os_agnostic
def test_com_hresult_reads_args_of_a_com_error_raw() -> None:
    """A com_error's ``args[0]`` is already an HRESULT: a small value is not a Win32 code."""
    failure = com_error(5)
    del failure.hresult
    assert com_hresult(failure) == 0x00000005


# --- com_hresult: DISP_E_EXCEPTION unwrap -----------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize("scode", [signed(0x80070002), 0x80070002])
def test_com_hresult_unwraps_a_dispatch_exception_to_its_scode(scode: int) -> None:
    assert com_hresult(dispatch_error(scode)) == 0x80070002


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "excepinfo",
    [None, (0, "s", "d", None, 0, 0), (0, "s", "d", None, 0, None), (0, "s", "d", None, 0, True), (0, "s", "d")],
    ids=["missing", "zero-scode", "none-scode", "bool-scode", "short-tuple"],
)
def test_com_hresult_keeps_the_outer_code_when_there_is_no_scode_to_read(excepinfo: object) -> None:
    assert com_hresult(com_error(DISP_E_EXCEPTION, "Exception occurred.", excepinfo)) == _DISP


@pytest.mark.os_agnostic
def test_com_hresult_unwraps_from_args_when_the_attributes_are_missing() -> None:
    failure = dispatch_error(signed(0x80070005))
    del failure.hresult
    del failure.excepinfo
    assert com_hresult(failure) == 0x80070005


@pytest.mark.os_agnostic
def test_com_hresult_reads_excepinfo_from_args_when_the_attribute_is_missing() -> None:
    failure = dispatch_error(signed(0x80070005))
    del failure.excepinfo
    assert com_hresult(failure) == 0x80070005


# --- com_hresult: pywintypes.error ------------------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("winerror", "expected"),
    [
        (5, 0x80070005),
        (2, 0x80070002),
        (1, 0x80070001),
        (0xFFFF, 0x8007FFFF),
        (0x10000, 0x00010000),
        (0, 0),
        (0x12345, 0x12345),
        (signed(0x80004005), 0x80004005),
    ],
    ids=[
        "access-denied",
        "not-found",
        "min-win32",
        "max-win32",
        "first-beyond-win32",
        "success",
        "beyond-win32",
        "already-an-hresult",
    ],
)
def test_com_hresult_maps_a_win32_code_to_its_hresult(winerror: int, expected: int) -> None:
    assert com_hresult(error(winerror)) == expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize("missing", ["deleted", "none"])
def test_com_hresult_maps_args_of_a_win32_error_as_a_win32_code(missing: str) -> None:
    """A pywintypes.error's ``args[0]`` is its Win32 code, so it is mapped like ``.winerror``."""
    failure = error(5, "OpenSCManager", "Access is denied.")
    if missing == "deleted":
        del failure.winerror
    else:
        failure.winerror = None
    assert com_hresult(failure) == 0x80070005


@pytest.mark.os_agnostic
@pytest.mark.parametrize("winerror", [None, True, "5"])
def test_com_hresult_is_none_for_a_win32_error_without_an_int_code(winerror: object) -> None:
    assert com_hresult(error(winerror)) is None


# --- com_outer_hresult -----------------------------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (dispatch_error(signed(0x80041010)), _DISP),
        (com_error(signed(0x80041010)), 0x80041010),
        (error(5), 0x80070005),
        (codeless_com_error(), None),
        (ValueError("not COM"), None),
    ],
    ids=["dispatch", "plain", "win32", "codeless", "not-com"],
)
def test_com_outer_hresult_reads_the_errors_own_code_without_unwrapping(exc: BaseException, expected: object) -> None:
    assert com_outer_hresult(exc) == expected


# --- com_error_text ---------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_com_error_text_reads_a_plain_com_error() -> None:
    failure = com_error(signed(0x80070002), "The system cannot find the file.")
    assert com_error_text(failure) == "The system cannot find the file."


@pytest.mark.os_agnostic
def test_com_error_text_prefers_the_callee_description_inside_a_dispatch_exception() -> None:
    assert com_error_text(dispatch_error(signed(0x8024402C), "No network")) == "No network"


@pytest.mark.os_agnostic
def test_com_error_text_reads_the_callee_description_even_without_a_scode() -> None:
    """The description is the callee's own account; a zero scode does not make it less so."""
    assert com_error_text(dispatch_error(0, "No network")) == "No network"


@pytest.mark.os_agnostic
@pytest.mark.parametrize("description", [None, "", 7])
def test_com_error_text_keeps_the_outer_text_without_a_callee_description(description: object) -> None:
    assert com_error_text(dispatch_error(signed(0x8024402C), description)) == "Exception occurred."


@pytest.mark.os_agnostic
def test_com_error_text_reads_a_win32_error_strerror() -> None:
    assert com_error_text(error(5, "OpenSCManager", "Access is denied.")) == "Access is denied."


@pytest.mark.os_agnostic
def test_com_error_text_falls_back_to_args_when_the_attribute_is_missing() -> None:
    """Each class keeps its text at its own position: ``args[1]`` for com_error, ``args[2]`` for error."""
    plain = com_error(1, "from args")
    del plain.strerror
    win32 = error(5, "OpenSCManager", "from args too")
    del win32.strerror
    assert com_error_text(plain) == "from args"
    assert com_error_text(win32) == "from args too"


@pytest.mark.os_agnostic
def test_com_error_text_is_empty_when_there_is_no_text() -> None:
    assert com_error_text(com_error(1, None)) == ""
    assert com_error_text(ValueError("not COM")) == ""


@pytest.mark.os_agnostic
def test_com_error_text_needs_no_code() -> None:
    assert com_error_text(codeless_com_error()) == "localized text nobody may rely on"
