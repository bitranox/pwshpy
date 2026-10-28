"""marshal.native_error types a caught native error by its code, not its text.

The pywin32 doubles come from ``com_fakes`` (class name AND module ``pywintypes``, as the seam
recognises them); the psutil double is a class named ``AccessDenied`` from a ``psutil`` module.
A Windows ``OSError`` carries ``winerror``, which CPython sets only on Windows, so
:class:`_WindowsOSError` pins it here on any OS.
"""

from __future__ import annotations

import errno
import pickle

import pytest
from com_fakes import com_error_for, dispatch_error, error, signed

from pwshpy import ElevationDeclinedError, NativeAccessDeniedError, NativeCallError
from pwshpy.adapters.native.marshal import E_ACCESSDENIED, native_error, native_error_for_hresult

pytestmark = pytest.mark.os_agnostic

_E_FILE_NOT_FOUND = 0x80070002
_E_CANCELLED = 0x800704C7  # HRESULT_FROM_WIN32(ERROR_CANCELLED = 1223)


class _WindowsOSError(OSError):
    """An OSError with winerror, which only Windows sets; pinned here on any OS."""

    winerror: int


def _winerror_oserror(code: int) -> OSError:
    """A Windows-shaped OSError for Win32 ``code``, with the errno CPython derives from it."""
    exc = _WindowsOSError(errno.EACCES if code == 5 else errno.ENOENT, "text")
    exc.winerror = code
    return exc


def _psutil_access_denied(module: str = "psutil._exceptions") -> BaseException:
    """An exception shaped like ``psutil.AccessDenied`` (not an OSError) from ``module``."""
    cls = type("AccessDenied", (Exception,), {})
    cls.__module__ = module
    return cls()


def test_com_access_denied() -> None:
    err = native_error("open failed", com_error_for(E_ACCESSDENIED))
    assert type(err) is NativeAccessDeniedError
    assert (err.hresult, err.errno, str(err)) == (E_ACCESSDENIED, None, "open failed")


def test_com_other_code() -> None:
    err = native_error("x", com_error_for(_E_FILE_NOT_FOUND))
    assert (type(err), err.hresult) == (NativeCallError, _E_FILE_NOT_FOUND)


def test_dispatch_exception_is_judged_by_the_callee_code() -> None:
    err = native_error("x", dispatch_error(signed(E_ACCESSDENIED)))
    assert (type(err), err.hresult) == (NativeAccessDeniedError, E_ACCESSDENIED)


def test_win32_error_mapped() -> None:
    err = native_error("x", error(5))
    assert (type(err), err.hresult) == (NativeAccessDeniedError, E_ACCESSDENIED)


def test_windows_oserror_maps_winerror() -> None:  # [155]
    err = native_error("x", _winerror_oserror(2))
    assert (type(err), err.hresult) == (NativeCallError, _E_FILE_NOT_FOUND)


def test_windows_oserror_access_denied_keeps_errno() -> None:
    err = native_error("x", _winerror_oserror(5))
    assert type(err) is NativeAccessDeniedError
    assert (err.hresult, err.errno) == (E_ACCESSDENIED, errno.EACCES)


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM])
def test_posix_permission(code: int) -> None:
    err = native_error("x", OSError(code, "denied"))
    assert type(err) is NativeAccessDeniedError
    assert (err.hresult, err.errno) == (None, code)


def test_posix_other_oserror() -> None:
    err = native_error("x", FileNotFoundError(errno.ENOENT, "gone"))
    assert (type(err), err.hresult) == (NativeCallError, None)


def test_psutil_access_denied() -> None:
    err = native_error("x", _psutil_access_denied())
    assert type(err) is NativeAccessDeniedError
    assert (err.hresult, err.errno) == (None, None)


def test_unrelated_class_named_access_denied_is_not_upgraded() -> None:
    assert type(native_error("x", _psutil_access_denied("someone.else"))) is NativeCallError


def test_non_native_cause() -> None:
    err = native_error("x", KeyError("k"))
    assert (type(err), err.hresult) == (NativeCallError, None)


def test_explicit_kind_is_kept_and_carries_the_code() -> None:
    err = native_error("refused", error(1223), kind=ElevationDeclinedError)
    assert (type(err), err.hresult) == (ElevationDeclinedError, _E_CANCELLED)


def test_explicit_kind_is_never_upgraded_to_access_denied() -> None:
    err = native_error("refused", PermissionError(errno.EACCES, "x"), kind=ElevationDeclinedError)
    assert type(err) is ElevationDeclinedError


def test_factory_built_denial_survives_pickle() -> None:
    denied = native_error("x", OSError(errno.EACCES, "denied"))
    rebuilt: BaseException = pickle.loads(pickle.dumps(denied))  # noqa: S301 - our own bytes
    assert isinstance(rebuilt, NativeAccessDeniedError)
    assert (str(rebuilt), rebuilt.errno) == ("x", errno.EACCES)


@pytest.mark.parametrize(
    ("hresult", "kind"),
    [(E_ACCESSDENIED, NativeAccessDeniedError), (0x80041328, NativeCallError), (None, NativeCallError)],
)
def test_for_hresult(hresult: int | None, kind: type[NativeCallError]) -> None:
    err = native_error_for_hresult("x", hresult)
    assert (type(err), err.hresult) == (kind, hresult)
