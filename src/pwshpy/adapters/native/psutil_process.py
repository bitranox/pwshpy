"""native process source over ``psutil`` (the portable, cross-platform binding).

This module is the **typed facade** over the untyped ``psutil`` surface: the
single ``# pyright: ignore`` for its missing stubs is confined here, and the rest
of the codebase consumes the fully-typed :func:`iter_processes`.

Records come from a trusted native API, so they are built with
``model_construct`` (validation skipped) on this hot path — thousands of
processes must not each pay Pydantic validation.

Contents:
    * :func:`iter_processes` — yield one :class:`ProcessInfo` per live process.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

import psutil as _psutil_module  # pyright: ignore[reportMissingTypeStubs]

from ...domain.records import ProcessInfo
from .marshal import epoch_to_datetime, to_process_name, to_process_status

# Funnel the untyped module through an explicit ``Any`` alias so member accesses
# below do not each raise ``reportUnknownMemberType`` under pyright strict.
_psutil: Any = _psutil_module

_PROCESS_ATTRS = ("pid", "ppid", "name", "status", "username", "memory_info", "create_time")


def _memory_rss(memory_info: Any) -> int | None:
    """Extract resident-set-size bytes from a psutil ``memory_info`` value."""
    if memory_info is None:
        return None
    rss = getattr(memory_info, "rss", None)
    return int(rss) if rss is not None else None


def _to_process_info(info: dict[str, Any]) -> ProcessInfo:
    """Marshal one psutil ``proc.info`` mapping into a :class:`ProcessInfo`."""
    return ProcessInfo.model_construct(
        pid=int(info["pid"]),
        ppid=None if info.get("ppid") is None else int(info["ppid"]),
        name=to_process_name(info.get("name"), strip_exe=sys.platform == "win32"),
        status=to_process_status(info.get("status")),
        username=info.get("username"),
        memory_rss=_memory_rss(info.get("memory_info")),
        create_time=epoch_to_datetime(info.get("create_time")),
    )


def iter_processes() -> Iterator[ProcessInfo]:
    """Yield a :class:`ProcessInfo` for every process visible to the caller.

    Processes that vanish or deny access mid-iteration are skipped rather than
    aborting the whole listing.

    Example:
        >>> it = iter_processes()
        >>> isinstance(next(it), ProcessInfo)
        True
    """
    for proc in _psutil.process_iter(_PROCESS_ATTRS):
        try:
            info: dict[str, Any] = proc.info
        except (_psutil.NoSuchProcess, _psutil.AccessDenied):
            continue
        yield _to_process_info(info)


__all__ = ["iter_processes"]
