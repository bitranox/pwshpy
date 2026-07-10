"""native event-log CONTROL over ``win32evtlog`` (Windows-only, **mutating**).

``Clear-EventLog`` - clear a Windows event log via the modern ``EvtClearLog`` API,
optionally exporting it to a backup file first.  The mutating counterpart of the
read-only :mod:`~pwshpy.adapters.native.event_log` source.  ``pywin32`` is imported
lazily so a portable install stays clean.

**MUTATING** - see CLAUDE.md "Development Safety": clearing a log is irreversible.

Contents:
    * :class:`NativeEventLogController` - clear one event log.
"""

from __future__ import annotations

import importlib
from typing import Any

from ...domain.errors import NativeCallError, PlatformUnsupportedError


def _load_win32evtlog() -> Any:
    """Import ``win32evtlog`` lazily; absent means a portable (non-Windows) install."""
    try:
        return importlib.import_module("win32evtlog")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "Event-log control requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc


class NativeEventLogController:
    """Mutating event-log control over ``win32evtlog`` (Clear-EventLog)."""

    def clear(self, log_name: str, *, backup_path: str | None = None) -> None:
        """Clear the event log ``log_name`` (optionally backing it up to ``backup_path`` first)."""
        win32evtlog = _load_win32evtlog()
        try:
            # pywin32 signature: EvtClearLog(ChannelPath, TargetFilePath=None, ...); omit the
            # backup arg entirely when there is none (passing None is rejected).
            if backup_path is None:
                win32evtlog.EvtClearLog(log_name)
            else:
                win32evtlog.EvtClearLog(log_name, backup_path)
        except Exception as exc:
            raise NativeCallError(str(exc) or f"cannot clear event log {log_name!r}") from exc


__all__ = ["NativeEventLogController"]
