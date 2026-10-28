"""marshal.native_error types a caught native error by its code, not its text.

The pywin32 doubles come from ``com_fakes`` (class name AND module ``pywintypes``, as the seam
recognises them); the psutil double is a class named ``AccessDenied`` from a ``psutil`` module.
A Windows ``OSError`` carries ``winerror``, which CPython sets only on Windows, so the
``oserror_fakes`` doubles pin it here on any OS.
"""

from __future__ import annotations

import errno
import pickle

import pytest
from com_fakes import com_error_for, dispatch_error, error, signed
from oserror_fakes import WindowsOSError, WindowsPermissionError

from pwshpy import ElevationDeclinedError, NativeAccessDeniedError, NativeCallError
from pwshpy.adapters.native.marshal import E_ACCESSDENIED, native_error, native_error_for_hresult

pytestmark = pytest.mark.os_agnostic

_E_FILE_NOT_FOUND = 0x80070002
_E_CANCELLED = 0x800704C7  # HRESULT_FROM_WIN32(ERROR_CANCELLED = 1223)


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
    err = native_error("x", WindowsOSError(2, errno.ENOENT))
    assert (type(err), err.hresult) == (NativeCallError, _E_FILE_NOT_FOUND)


def test_windows_oserror_access_denied_keeps_errno() -> None:
    err = native_error("x", WindowsPermissionError())
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


# --- The effective native cause behind a wrapper library's own error --------------------
#
# lib_registry raises its own RegistryError family (not an OSError), some `from None` (the
# winreg OSError survives only as the suppressed __context__), some `from exc`. Measured on
# Windows: a missing key reached pwshpy with hresult None until the chain was walked.


class _RegistryError(Exception):
    """Shaped like lib_registry's RegistryError: a plain Exception, no code of its own."""


def _raised_over(inner: BaseException, *, suppress: bool) -> _RegistryError:
    """Raise ``_RegistryError`` while handling ``inner``, ``from None`` or ``from inner``."""
    try:
        try:
            raise inner
        except OSError as exc:
            if suppress:
                raise _RegistryError("registry key not found") from None
            raise _RegistryError("registry key not found") from exc
    except _RegistryError as wrapper:
        return wrapper
    raise AssertionError("unreachable")  # pragma: no cover


def _chain(depth: int, carrier: BaseException) -> BaseException:
    """``depth`` non-carriers linked by a ``from None`` context, with ``carrier`` at the end."""
    head: BaseException = carrier
    for index in range(depth):
        outer = ValueError(f"link {index}")
        outer.__context__ = head
        # What `raise ... from None` sets; an unsuppressed context is never walked.
        outer.__suppress_context__ = True
        head = outer
    return head


@pytest.mark.parametrize("suppress", [True, False], ids=["from-None", "from-exc"])
def test_wrapper_is_judged_by_the_winreg_error_it_hides(suppress: bool) -> None:
    err = native_error("x", _raised_over(WindowsOSError(2, errno.ENOENT), suppress=suppress))
    assert (type(err), err.hresult) == (NativeCallError, _E_FILE_NOT_FOUND)


def test_wrapper_over_a_denied_winreg_error_is_access_denied() -> None:
    err = native_error("x", _raised_over(WindowsPermissionError(), suppress=True))
    assert type(err) is NativeAccessDeniedError
    assert (err.hresult, err.errno) == (E_ACCESSDENIED, errno.EACCES)


def test_wrapper_over_a_pywin32_error_carries_its_code() -> None:
    wrapper = _RegistryError("wrapped")
    wrapper.__cause__ = com_error_for(E_ACCESSDENIED)
    err = native_error("x", wrapper)
    assert (type(err), err.hresult) == (NativeAccessDeniedError, E_ACCESSDENIED)


def test_explicit_cause_wins_over_the_context() -> None:
    wrapper = _RegistryError("wrapped")
    wrapper.__cause__ = WindowsOSError(2, errno.ENOENT)
    wrapper.__context__ = WindowsPermissionError()
    assert native_error("x", wrapper).hresult == _E_FILE_NOT_FOUND


def test_a_carrier_cause_is_not_walked_past() -> None:
    # The given OSError is the native error itself, even with no code: its own context is
    # older history, not a better answer.
    outer = FileNotFoundError(errno.ENOENT, "gone")
    outer.__context__ = WindowsPermissionError()
    err = native_error("x", outer)
    assert (type(err), err.hresult) == (NativeCallError, None)


def test_chain_without_a_carrier() -> None:
    err = native_error("x", _chain(3, KeyError("k")))
    assert (type(err), err.hresult) == (NativeCallError, None)


def test_carrier_four_links_down_is_found() -> None:
    assert native_error("x", _chain(4, WindowsOSError(2, errno.ENOENT))).hresult == _E_FILE_NOT_FOUND


def test_carrier_five_links_down_is_not_reached() -> None:
    assert native_error("x", _chain(5, WindowsOSError(2, errno.ENOENT))).hresult is None


def test_self_referencing_context_terminates() -> None:
    looped = ValueError("loop")
    looped.__context__ = looped
    assert native_error("x", looped).hresult is None


def test_psutil_access_denied_over_a_chain_without_a_carrier_stays_denied() -> None:
    denied = _psutil_access_denied()
    denied.__context__ = KeyError("k")
    assert type(native_error("x", denied)) is NativeAccessDeniedError


# --- An unsuppressed implicit context is the CALLER's history, never the native cause ----
#
# Python sets __context__ on any raise to whatever the thread is handling, including an
# exception a caller frames up is handling. A pwshpy site raising a non-carrier (a KeyError
# from a lookup, a parse error) inside a caller's `except PermissionError:` must not take
# that unrelated errno or winerror.


def _raised_while_handling(unrelated: BaseException) -> BaseException:
    """A KeyError raised (and caught) while ``unrelated`` is being handled: implicit context."""
    try:
        raise unrelated
    except OSError:
        try:
            raise KeyError("lookup")
        except KeyError as exc:
            assert exc.__context__ is unrelated  # proves the context is set
            assert exc.__suppress_context__ is False
            return exc
    raise AssertionError("unreachable")  # pragma: no cover


def test_unsuppressed_posix_context_is_not_taken() -> None:
    err = native_error("x", _raised_while_handling(PermissionError(errno.EACCES, "caller")))
    assert type(err) is NativeCallError
    assert err.hresult is None
    assert not hasattr(err, "errno")


def test_unsuppressed_windows_context_is_not_taken() -> None:
    err = native_error("x", _raised_while_handling(WindowsPermissionError()))
    assert type(err) is NativeCallError
    assert err.hresult is None
