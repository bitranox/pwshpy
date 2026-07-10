""".NET adapter package — hosted PowerShell 7.6 SDK (in-process via pythonnet).

Behind the optional ``[full]`` extra.  Importing this package never loads .NET;
a call without the extra raises :class:`~pwshpy.domain.errors.FeatureUnavailableError`.

Contents:
    * :func:`run` — execute a PowerShell script in the hosted engine.
    * :func:`invoke` — run a single cmdlet with safely bound parameters.
    * :func:`get_command` — introspect a command's parameter metadata.
    * :func:`is_available` — whether the ``[full]`` extra is importable.
"""

from __future__ import annotations

from .hosted import get_command, invoke, is_available, run

__all__ = ["get_command", "invoke", "is_available", "run"]
