"""Tier-A system-information source over ``psutil`` (portable, cross-platform).

Typed facade over the untyped ``psutil`` surface (the ``# pyright: ignore`` for
its missing stubs is confined here).  Mirrors ``Get-Uptime``.

Contents:
    * :func:`get_uptime` — boot time + elapsed uptime as a :class:`SystemUptime`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import psutil as _psutil_module  # pyright: ignore[reportMissingTypeStubs]

from ...domain.records import SystemUptime

# Funnel the untyped module through an explicit ``Any`` alias (see psutil_process).
_psutil: Any = _psutil_module


def get_uptime() -> SystemUptime:
    """Return the system boot time and seconds elapsed since boot.

    Example:
        >>> up = get_uptime()
        >>> up.uptime_seconds >= 0.0
        True
        >>> up.boot_time.tzinfo is not None
        True
    """
    boot_epoch = float(_psutil.boot_time())
    return SystemUptime.model_construct(
        boot_time=datetime.fromtimestamp(boot_epoch, tz=UTC),
        uptime_seconds=max(time.time() - boot_epoch, 0.0),
    )


__all__ = ["get_uptime"]
