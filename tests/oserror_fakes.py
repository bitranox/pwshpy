"""Doubles for an ``OSError`` carrying ``winerror``, which CPython sets only on Windows.

A Windows ``OSError`` carries its Win32 code on ``.winerror`` beside the errno CPython derives
from it; the seams that read it (``marshal.native_error``, ``is_pipe_closed``) must be tested on
every OS, so these doubles pin the attribute on any platform. Each class defines ``__init__``,
so ``OSError.__new__`` leaves the arguments to it and never picks an errno subclass on its own:
the class is the one the test names, as CPython's would be for that errno on Windows.

Imported by bare module name (``from oserror_fakes import ...``): pytest puts ``tests`` on the path.
"""

from __future__ import annotations

import errno


class WindowsOSError(OSError):
    """An ``OSError`` with ``winerror`` set; ``errno`` only when given (a pipe error may carry none).

    Example:
        >>> exc = WindowsOSError(32, errno.EACCES)
        >>> (exc.winerror, exc.errno, isinstance(exc, PermissionError))
        (32, 13, False)
        >>> WindowsOSError(232).errno is None
        True
    """

    winerror: int

    def __init__(self, winerror: int, errno_code: int | None = None, text: str = "localized text") -> None:
        if errno_code is None:
            super().__init__(text)
        else:
            super().__init__(errno_code, text)
        self.winerror = winerror


class WindowsPermissionError(WindowsOSError, PermissionError):
    """The ``PermissionError`` CPython raises on Windows for ERROR_ACCESS_DENIED (winerror 5).

    Example:
        >>> exc = WindowsPermissionError()
        >>> (exc.winerror, exc.errno, isinstance(exc, PermissionError))
        (5, 13, True)
    """

    def __init__(self, winerror: int = 5, errno_code: int | None = errno.EACCES, text: str = "denied") -> None:
        super().__init__(winerror, errno_code, text)


__all__ = ["WindowsOSError", "WindowsPermissionError"]
