"""Doubles shaped like pywin32's ``pywintypes.com_error`` and ``pywintypes.error``.

pywintypes is importable only on Windows, so the native adapters recognise its errors by class
name AND module (``pywintypes``); these doubles carry both, so the seam treats them exactly as
the real classes and every os_agnostic test runs on any OS. Each one carries only the attributes
the real class has (measured on Windows with pywin32): ``com_error`` its HRESULT signed on
``.hresult`` with ``.excepinfo``, never ``.winerror``; ``error`` its Win32 code on ``.winerror``,
never ``.hresult``.

Imported by bare module name (``from com_fakes import ...``): pytest puts ``tests`` on the path.
"""

from __future__ import annotations

#: IDispatch's "the callee raised" HRESULT, signed as pywin32 reports it; the callee's own
#: status rides in ``excepinfo[5]``.
DISP_E_EXCEPTION = -2147352567


def signed(code: int) -> int:
    """Return ``code`` as pywin32 reports an HRESULT: a signed 32-bit int."""
    return code - 0x1_0000_0000 if code & 0x8000_0000 else code


class com_error(Exception):  # noqa: N801, N818 - pywin32's class name is what the seam recognises
    """Shaped like ``pywintypes.com_error``: ``args == (hresult, strerror, excepinfo, argerror)``."""

    def __init__(
        self,
        hresult: object,
        strerror: object = "localized text nobody may rely on",
        excepinfo: object = None,
        argerror: object = None,
    ) -> None:
        super().__init__(hresult, strerror, excepinfo, argerror)
        self.hresult = hresult
        self.strerror = strerror
        self.excepinfo = excepinfo
        self.argerror = argerror


com_error.__module__ = "pywintypes"


class error(Exception):  # noqa: N801, N818 - pywin32's class name is what the seam recognises
    """Shaped like ``pywintypes.error``: ``args == (winerror, funcname, strerror)``."""

    def __init__(self, winerror: object, funcname: object = "Win32Function", strerror: object = "localized") -> None:
        super().__init__(winerror, funcname, strerror)
        self.winerror = winerror
        self.funcname = funcname
        self.strerror = strerror


error.__module__ = "pywintypes"


def com_error_for(code: int, text: str = "localized text nobody may rely on") -> com_error:
    """A plain ``com_error`` for an unsigned HRESULT ``code``, carried signed as pywin32 does."""
    return com_error(signed(code), text)


def dispatch_error(scode: object, description: object = None, text: str = "Exception occurred.") -> com_error:
    """A DISP_E_EXCEPTION ``com_error`` wrapping ``scode`` (and ``description``) in its excepinfo."""
    return com_error(DISP_E_EXCEPTION, text, (0, "source", description, None, 0, scode))


def codeless_com_error() -> com_error:
    """A ``com_error`` from which no status can be read: no ``.hresult`` and no ``args``."""
    failure = com_error(0)
    del failure.hresult
    failure.args = ()
    return failure


__all__ = [
    "DISP_E_EXCEPTION",
    "codeless_com_error",
    "com_error",
    "com_error_for",
    "dispatch_error",
    "error",
    "signed",
]
