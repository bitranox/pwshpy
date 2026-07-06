"""Tier-B adapter — hosts the PowerShell 7.6 SDK in-process via pythonnet.

The engine is reached through the optional ``[full]`` extra (pythonnet + the
``Microsoft.PowerShell.SDK`` 7.6.x assemblies on the .NET 10 runtime).  A base
install carries none of this, so :func:`run` raises an actionable
:class:`FeatureUnavailableError` — importing this module never touches .NET.

This is the import-guard seam.  The in-process hosting itself (RunspacePool,
``AddCommand``/``AddParameter`` binding, PSObject marshaling, timeout/cancel) is
built on a Windows/.NET host in a later step; the guard contract is live now.

Contents:
    * :func:`is_available` — whether the ``[full]`` extra is importable.
    * :func:`run` — execute a script in the hosted engine (Tier B).
"""

from __future__ import annotations

import importlib.util
from typing import Any

from ...domain.errors import FeatureUnavailableError

_INSTALL_HINT = (
    "Tier B (hosted PowerShell 7.6 SDK) is unavailable: the optional dependencies "
    "are not installed. Install them with: pip install pwshpy[full] "
    "(also requires the .NET 10 runtime present)."
)


def is_available() -> bool:
    """Return whether the ``[full]`` extra (pythonnet) is importable.

    Example:
        >>> isinstance(is_available(), bool)
        True
    """
    return importlib.util.find_spec("pythonnet") is not None


def run(script: str, *, timeout: float | None = None) -> list[Any]:
    """Execute ``script`` in the hosted PowerShell engine and return marshaled objects.

    Args:
        script: PowerShell source to run in-process. An explicit trust boundary —
            never feed it unvalidated external input.
        timeout: Optional seconds before the host stops the pipeline.

    Raises:
        FeatureUnavailableError: If the ``[full]`` extra is not installed.

    Example:
        >>> run("Get-Process")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ...
        FeatureUnavailableError: ...
    """
    if not is_available():
        raise FeatureUnavailableError(_INSTALL_HINT)
    # The hosted-engine bridge is wired on a Windows/.NET host in a later step.
    raise FeatureUnavailableError(_INSTALL_HINT)  # pragma: no cover


__all__ = ["is_available", "run"]
