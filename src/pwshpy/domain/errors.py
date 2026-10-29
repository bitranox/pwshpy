"""Domain exception hierarchy rooted at :class:`PwshPyError`.

Every error raised by pwshpy inherits from :class:`PwshPyError`, so callers can
catch the whole surface with one ``except``.  The marshaling seam in the adapters
layer is the single place that maps foreign errors (``pywintypes.error`` /
HRESULT, .NET ``ErrorRecord``) into this tree.

Contents:
    * :class:`PwshPyError` - root of the hierarchy.
    * :class:`NativeCallError` - a native binding call failed.
    * :class:`NativeAccessDeniedError` - a native call was refused for lack of rights.
    * :class:`ElevationDeclinedError` - the person refused the Administrator prompt.
    * :class:`OperationTimeoutError` - a native call outlived its timeout and its worker was killed.
    * :class:`PowerShellError` - a .NET hosted-PowerShell call failed.
    * :class:`FeatureUnavailableError` - .NET invoked without the ``[full]`` extra.
    * :class:`PlatformUnsupportedError` - a Windows-only adapter used off Windows, or an input
      the running OS has no analog for.
    * :class:`ElevationRequiredError` - an operation needs administrative rights it lacks.
    * :class:`ConfigurationError` - missing, invalid, or incomplete configuration.
    * :class:`PackError` - a script could not be packed into a self-extracting runner.
"""

from __future__ import annotations

from typing import Any


class PwshPyError(Exception):
    """Root of the pwshpy exception hierarchy.

    Example:
        >>> issubclass(NativeCallError, PwshPyError)
        True
    """

    def __reduce__(self) -> tuple[Any, ...]:
        """Rebuild without ``__init__`` so pickle and ``copy.deepcopy`` keep the error intact.

        The inherited reduce calls ``type(self)(*self.args)``, which works only while a
        subclass's ``__init__`` takes exactly its ``args``.  A keyword-only parameter
        (``OperationTimeoutError(message, timeout=...)``) makes the load raise ``TypeError``,
        e.g. when the error crosses a ``multiprocessing`` pipe; an ``__init__`` that joins its
        input into the message (``InvalidLoggingConfigError(problems)``) is handed that message
        back and joins it again, one character at a time.  Restoring ``args`` plus ``__dict__``
        keeps every attribute a subclass adds, whatever its ``__init__`` signature.  It sits on
        the root rather than on each such subclass because forgetting it fails silently.

        Returns:
            The reduce triple: rebuild callable, its arguments, and the instance state.

        Example:
            >>> import pickle
            >>> rebuilt = pickle.loads(pickle.dumps(OperationTimeoutError("slow", timeout=5.0)))
            >>> (str(rebuilt), rebuilt.timeout)
            ('slow', 5.0)
        """
        return (_rebuild_without_init, (type(self), self.args), self.__dict__)


def _rebuild_without_init(cls: type[BaseException], args: tuple[object, ...]) -> BaseException:
    """Create ``cls`` with ``args`` but without running ``__init__`` (the pickle state follows).

    ``args`` is assigned explicitly: an ``OSError`` subclass with its own ``__init__`` (such as
    :class:`NativeAccessDeniedError`) leaves ``args`` empty under ``__new__``, so the rebuilt
    error would have no message.

    Example:
        >>> _rebuild_without_init(OperationTimeoutError, ("slow",)).args
        ('slow',)
    """
    rebuilt = cls.__new__(cls, *args)
    rebuilt.args = args
    return rebuilt


class NativeCallError(PwshPyError):
    """A native binding call (win32 / wmi / psutil / winreg / systemd D-Bus / ...) failed.

    Wraps the foreign error (``pywintypes.error`` / HRESULT, a D-Bus error, an
    ``OSError``) surfaced by a native binding, re-raised as a typed pwshpy error at
    the boundary. Also raised by a ``strict=True`` listing for an item it cannot read,
    and by :meth:`~pwshpy.domain.records.ProcessResult.check` for a nonzero exit.

    ``hresult`` is the failure's status as an unsigned 32-bit HRESULT when the native error
    carried one (a pywin32 COM/Win32 error, or a Windows ``OSError`` whose ``winerror`` is
    mapped as HRESULT_FROM_WIN32), else ``None``. Branch on it, never on the (localized)
    message.

    Args:
        message: The human-readable description.
        hresult: The unsigned HRESULT, or ``None`` when the failure carried none.

    Attributes:
        hresult: The unsigned HRESULT, or ``None``.

    Example:
        >>> raise NativeCallError("OpenSCManager failed")
        Traceback (most recent call last):
        ...
        pwshpy.domain.errors.NativeCallError: OpenSCManager failed
        >>> f"0x{NativeCallError('x', hresult=0x80070002).hresult:08X}"
        '0x80070002'
    """

    hresult: int | None = None

    def __init__(self, message: str, *, hresult: int | None = None) -> None:
        super().__init__(message)
        self.hresult = hresult


class NativeAccessDeniedError(NativeCallError, PermissionError):
    """A native call was refused for lack of rights (E_ACCESSDENIED, EACCES, EPERM).

    Also a systemd refusal over D-Bus (``org.freedesktop.DBus.Error.AccessDenied`` or
    ``InteractiveAuthorizationRequired``). A Secret Service or shadow-utils refusal stays a plain
    :class:`NativeCallError`.

    A :class:`NativeCallError`, so existing handlers keep catching it, and a
    :class:`PermissionError`, so ``except PermissionError`` catches it and the ``pwshpy`` CLI
    exits as for one (13 on POSIX, 5 on Windows, via lib_cli_exit_tools). ``errno`` is the
    POSIX code when the failure carried one, else ``None``.

    Args:
        message: The human-readable description.
        hresult: The unsigned HRESULT (``0x80070005`` on Windows), or ``None``.

    Example:
        >>> isinstance(NativeAccessDeniedError("denied"), PermissionError)
        True
    """

    def __reduce__(self) -> tuple[Any, ...]:
        """Keep ``errno`` across pickle: it is an OSError slot, not part of ``__dict__``.

        Returns:
            The reduce triple of :meth:`PwshPyError.__reduce__` with ``errno`` in its state.
        """
        rebuild, args, state = super().__reduce__()
        return (rebuild, args, {**state, "errno": self.errno})


class ElevationDeclinedError(NativeCallError):
    """The elevated relaunch was refused: the UAC prompt, or sudo's password.

    A subclass of :class:`NativeCallError`, so code that already catches that
    keeps working, and its own type, because a refusal is a decision rather than
    a fault: a caller typically carries on unelevated and says so, where a
    failure is reported as one. Windows reports it as ``ERROR_CANCELLED`` (1223);
    on POSIX it is a non-zero ``sudo -v``, which a wrong password, an interrupted
    prompt and a user sudo does not allow all give alike. The ``pwshpy`` CLI
    (``--elevate``) and the packed runners exit 77 (``EX_NOPERM``) for it.

    Example:
        >>> issubclass(ElevationDeclinedError, NativeCallError)
        True
    """


class OperationTimeoutError(NativeCallError):
    """A native call did not finish within its timeout and was abandoned (its worker killed).

    A subclass of :class:`NativeCallError`, so existing ``except NativeCallError``
    callers keep working, and its own type, so a caller can tell a call that ran
    out of time from one that could not start at all: catch it before
    ``NativeCallError``. Raised by ``ps.exec(timeout=...)`` (the child, or with
    ``wait_tree`` its whole tree, is killed), ``ps.get_windows_update`` (the search
    child is killed), a service start/stop/restart that has not settled (the
    control WAS issued), ``ps.wait_process(timeout=...)`` (the process keeps
    running), ``ps.resolve_dns_name(timeout=...)`` (the lookup is abandoned), the web
    verbs ``ps.invoke_web_request`` / ``ps.invoke_rest_method`` / ``ps.download_file``
    (a connect or a read that stalls) and, on Linux, a local-account verb whose
    shadow-utils commands together outlast its ``timeout`` (the running one is killed).
    The ``pwshpy`` CLI exits 110 (``ETIMEDOUT``) for it.

    Args:
        message: The human-readable description.
        timeout: The limit, in seconds, that ran out.

    Attributes:
        timeout: The limit, in seconds, that ran out.

    Example:
        >>> OperationTimeoutError("slow", timeout=5.0).timeout
        5.0
    """

    def __init__(self, message: str, *, timeout: float) -> None:
        super().__init__(message)
        self.timeout = timeout


class PowerShellError(PwshPyError):
    """A .NET hosted-PowerShell invocation failed.

    Wraps a PowerShell ``ErrorRecord`` / .NET exception, preserving the
    category and PowerShell stack where available.

    Example:
        >>> issubclass(PowerShellError, PwshPyError)
        True
    """


class FeatureUnavailableError(PwshPyError):
    """A .NET call was made without the optional ``[full]`` extra installed.

    The message names the exact ``pip install pwshpy[full]`` fix so the failure
    is actionable.

    Example:
        >>> raise FeatureUnavailableError(".NET requires pip install pwshpy[full]")
        Traceback (most recent call last):
        ...
        pwshpy.domain.errors.FeatureUnavailableError: .NET requires pip install pwshpy[full]
    """


class PlatformUnsupportedError(PwshPyError):
    """The running OS has no honest analog for the call or one of its inputs.

    A Windows-only subsystem (registry, CIM/WMI, Windows Update, hotfixes, shortcuts)
    called off Windows, or an input the running OS cannot honour, such as a decimal uid
    as a scheduled-task principal on Windows, a SID or a logon trigger on Linux, or
    ``KnownFolder.PROGRAMS`` on Linux or macOS, where .NET maps it to no folder.

    Example:
        >>> issubclass(PlatformUnsupportedError, PwshPyError)
        True
    """


class ElevationRequiredError(PwshPyError):
    """An operation requires administrative (elevated) privileges it does not have.

    Raised by :meth:`~pwshpy.composition.facade.Ps.require_elevation` when the
    current process is not elevated - the honest, catchable equivalent of
    PowerShell's ``#Requires -RunAsAdministrator`` (which only aborts).  Use
    :meth:`~pwshpy.composition.facade.Ps.elevate` to relaunch elevated.

    Example:
        >>> issubclass(ElevationRequiredError, PwshPyError)
        True
    """


class ConfigurationError(PwshPyError):
    """Missing, invalid, or incomplete configuration.

    Raised when required configuration values are absent, malformed, or
    logically inconsistent, and for scheduled-task inputs that contradict each
    other (a logon type that does not fit the principal, a group SID as the
    principal, a line break in the program text), and for shortcut values a
    ``.lnk`` cannot store exactly (a relative target, a field over its length
    limit, NUL/CR/LF in a field, a path not ending in ``.lnk``), refused before
    anything is written. Typically caught at CLI boundaries to provide
    user-friendly error messages.

    Example:
        >>> str(ConfigurationError("No output format configured"))
        'No output format configured'
    """


class PackError(PwshPyError):
    """A script could not be packed into a self-extracting runner.

    Raised for an unreadable or non-existent entry script, a syntax error in a
    module being packed, a local import that resolves outside the pack root, or
    a destination that would clobber an input file.

    Example:
        >>> str(PackError("entry not found: tool.py"))
        'entry not found: tool.py'
    """


__all__ = [
    "ConfigurationError",
    "PackError",
    "ElevationDeclinedError",
    "ElevationRequiredError",
    "FeatureUnavailableError",
    "NativeAccessDeniedError",
    "NativeCallError",
    "OperationTimeoutError",
    "PlatformUnsupportedError",
    "PowerShellError",
    "PwshPyError",
]
