"""Tier-A environment-variable source over ``os.environ`` (portable, stdlib).

Mirrors PowerShell's ``env:`` drive.  ``os.environ`` is fully typed, so no
pyright suppression is needed here.

Contents:
    * :func:`iter_env` — yield one :class:`EnvVar` per environment variable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from ...domain.records import EnvVar


def iter_env() -> Iterator[EnvVar]:
    """Yield an :class:`EnvVar` for every variable in the process environment.

    Example:
        >>> from pwshpy.domain.records import EnvVar
        >>> all(isinstance(v, EnvVar) for v in iter_env())
        True
    """
    for name, value in os.environ.items():
        yield EnvVar.model_construct(name=name, value=value)


__all__ = ["iter_env"]
