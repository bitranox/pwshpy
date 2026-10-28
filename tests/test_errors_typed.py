"""NativeCallError carries an HRESULT; NativeAccessDeniedError is also a PermissionError."""

from __future__ import annotations

import errno
from collections.abc import Callable

import lib_cli_exit_tools
import pytest
from exception_round_trips import deepcopy_round_trip, pickle_round_trip

from pwshpy import NativeAccessDeniedError, NativeCallError, OperationTimeoutError

pytestmark = pytest.mark.os_agnostic

_E_ACCESSDENIED = 0x80070005

_Clone = Callable[[NativeAccessDeniedError], NativeAccessDeniedError]


def test_hresult_defaults_to_none() -> None:
    assert NativeCallError("x").hresult is None
    assert OperationTimeoutError("slow", timeout=1.0).hresult is None


def test_hresult_is_kept() -> None:
    assert NativeCallError("x", hresult=0x80070002).hresult == 0x80070002


def test_access_denied_is_both_types() -> None:
    exc = NativeAccessDeniedError("denied", hresult=_E_ACCESSDENIED)
    assert isinstance(exc, NativeCallError)
    assert isinstance(exc, PermissionError)
    assert str(exc) == "denied"


def test_access_denied_exits_like_a_permission_error() -> None:
    expected = lib_cli_exit_tools.get_system_exit_code(PermissionError())
    assert lib_cli_exit_tools.get_system_exit_code(NativeAccessDeniedError("denied")) == expected


@pytest.mark.parametrize("clone", [pickle_round_trip, deepcopy_round_trip], ids=["pickle", "deepcopy"])
def test_access_denied_survives_pickle_and_deepcopy(clone: _Clone) -> None:
    original = NativeAccessDeniedError("denied", hresult=_E_ACCESSDENIED)
    original.errno = errno.EACCES
    rebuilt = clone(original)
    assert type(rebuilt) is NativeAccessDeniedError
    assert (str(rebuilt), rebuilt.args) == ("denied", ("denied",))
    assert (rebuilt.hresult, rebuilt.errno) == (_E_ACCESSDENIED, errno.EACCES)


def test_plain_error_survives_pickle_with_hresult() -> None:
    rebuilt = pickle_round_trip(NativeCallError("x", hresult=0x80041328))
    assert (str(rebuilt), rebuilt.hresult) == ("x", 0x80041328)
