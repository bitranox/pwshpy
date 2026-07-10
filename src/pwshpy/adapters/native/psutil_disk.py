"""native disk-usage source over ``psutil`` (portable, cross-platform).

Typed facade over the untyped ``psutil`` surface (the ``# pyright: ignore`` for
its missing stubs is confined here).  Mirrors ``Get-Volume``: each mounted
partition marshals into a :class:`DiskUsage` via ``model_construct``.

Contents:
    * :func:`iter_disks` — yield one :class:`DiskUsage` per mounted filesystem.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psutil as _psutil_module  # pyright: ignore[reportMissingTypeStubs]

from ...domain.records import DiskUsage

# Funnel the untyped module through an explicit ``Any`` alias (see psutil_process).
_psutil: Any = _psutil_module


def _to_disk_usage(partition: Any, usage: Any) -> DiskUsage:
    """Marshal a psutil partition + usage pair into a :class:`DiskUsage`."""
    return DiskUsage.model_construct(
        device=str(partition.device),
        mountpoint=str(partition.mountpoint),
        fstype=str(partition.fstype),
        total=int(usage.total),
        used=int(usage.used),
        free=int(usage.free),
        percent=float(usage.percent),
    )


def iter_disks() -> Iterator[DiskUsage]:
    """Yield a :class:`DiskUsage` for every mounted filesystem visible to the caller.

    Partitions whose usage cannot be read (empty removable drives, permission
    errors) are skipped rather than aborting the listing.

    Example:
        >>> from pwshpy.domain.records import DiskUsage
        >>> all(isinstance(d, DiskUsage) for d in iter_disks())
        True
    """
    for partition in _psutil.disk_partitions(all=False):
        try:
            usage = _psutil.disk_usage(partition.mountpoint)
        except OSError:
            continue
        yield _to_disk_usage(partition, usage)


__all__ = ["iter_disks"]
