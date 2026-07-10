"""Domain exception hierarchy rooted at :class:`PwshPyError`.

Every error raised by pwshpy inherits from :class:`PwshPyError`, so callers can
catch the whole surface with one ``except``.  The marshaling seam in the adapters
layer is the single place that maps foreign errors (``pywintypes.error`` /
HRESULT, .NET ``ErrorRecord``) into this tree.

Contents:
    * :class:`PwshPyError` — root of the hierarchy.
    * :class:`NativeCallError` — a native native binding call failed.
    * :class:`PowerShellError` — a .NET hosted-PowerShell call failed.
    * :class:`FeatureUnavailableError` — .NET invoked without the ``[full]`` extra.
    * :class:`PlatformUnsupportedError` — a Windows-only adapter used off Windows.
    * :class:`ElevationRequiredError` — an operation needs administrative rights it lacks.
    * :class:`ConfigurationError` — missing, invalid, or incomplete configuration.
"""

from __future__ import annotations


class PwshPyError(Exception):
    """Root of the pwshpy exception hierarchy.

    Example:
        >>> issubclass(NativeCallError, PwshPyError)
        True
    """


class NativeCallError(PwshPyError):
    """A native native binding call (win32 / wmi / psutil / winreg) failed.

    Wraps the foreign error (``pywintypes.error`` / HRESULT) surfaced by a
    native Windows binding, re-raised as a typed pwshpy error at the boundary.

    Example:
        >>> raise NativeCallError("OpenSCManager failed")
        Traceback (most recent call last):
        ...
        pwshpy.domain.errors.NativeCallError: OpenSCManager failed
    """


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
    """A Windows-only adapter was used on a non-Windows platform.

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
    logically inconsistent. Typically caught at CLI boundaries to provide
    user-friendly error messages.

    Example:
        >>> str(ConfigurationError("No output format configured"))
        'No output format configured'
    """


__all__ = [
    "ConfigurationError",
    "ElevationRequiredError",
    "FeatureUnavailableError",
    "NativeCallError",
    "PlatformUnsupportedError",
    "PowerShellError",
    "PwshPyError",
]
