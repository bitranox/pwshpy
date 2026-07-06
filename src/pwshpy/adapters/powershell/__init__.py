"""Tier-B adapter package — hosted PowerShell 7.6 SDK (in-process via pythonnet).

Behind the optional ``[full]`` extra.  Importing this package never loads .NET;
a call without the extra raises :class:`~pwshpy.domain.errors.FeatureUnavailableError`.

Contents:
    * :func:`run` — execute a PowerShell script in the hosted engine.
    * :func:`is_available` — whether the ``[full]`` extra is importable.
"""

from __future__ import annotations

from .hosted import is_available, run

__all__ = ["is_available", "run"]
