"""Run in-process COM work on a dedicated fresh thread in its own multithreaded apartment.

pwshpy never initialises or uninitialises COM on a caller's thread. Measured on Windows with
pywin32: ``import pythoncom`` initialises the importing thread itself and records it as its
"main" thread; there pywin32 skips a later ``CoInitializeEx`` but performs the matching
``CoUninitialize`` for real, which leaves COM unusable on that thread for the rest of the process
(``0x800401F0``). A balanced init/uninit pair on the caller's thread is therefore not balanced.

So the work runs on a new thread that does ``CoInitializeEx(COINIT_MULTITHREADED)``, runs it, and
``CoUninitialize()`` in ``finally``; the result or the exception is handed back to the caller's
thread. ``pythoncom`` is loaded on the caller's thread first, as every other pywin32 adapter in
the process expects (the import initialises that thread the way pywin32 always does), so the
worker is never pywin32's "main" thread and its init and uninit are both real.

Every COM object the work creates must be a local of the work (or of what it calls) and must not
be returned: locals are released when the work's frames end, before the uninit. When the work
raises, the traceback still holds those frames, so their locals are cleared before the uninit
too; a COM object released after it is released into a torn-down apartment.

Contents:
    * :func:`run_in_com_thread` - run one callable on a fresh MTA thread and return its result.
"""

from __future__ import annotations

import importlib
import threading
import traceback
from collections.abc import Callable
from typing import Any, Generic, TypeVar

from ...domain.errors import PlatformUnsupportedError

__all__ = ["run_in_com_thread"]

_T = TypeVar("_T")


class _Outcome(Generic[_T]):
    """What the worker thread hands back: one result, or the exception the work raised."""

    def __init__(self) -> None:
        self.results: list[_T] = []
        self.error: BaseException | None = None


def run_in_com_thread(work: Callable[[], _T], *, load: Callable[[str], Any] | None = None) -> _T:
    """Run ``work`` on a fresh thread inside ``CoInitializeEx(MTA)`` / ``CoUninitialize``.

    The caller blocks until the worker ends. Nothing COM-related runs on the caller's thread
    beyond loading ``pythoncom``.

    The wait has no timeout, by design: a timeout could only abandon the worker, not stop it, and
    an abandoned worker would still hold the COM objects and the apartment the work is using. The
    work passed here is bounded (one shortcut save or read, no network or user prompt), so a hang
    is a hung COM call, which a timeout would hide rather than end. The worker is a daemon
    thread, so it never keeps the process alive at exit.

    Args:
        work: A callable taking no arguments. Every COM object it makes must stay local to it;
            its return value must not be a COM object (it outlives the apartment).
        load: Module loader for ``pythoncom``; ``None`` imports the real pywin32 module (a test
            passes a double).

    Returns:
        What ``work`` returned.

    Raises:
        PlatformUnsupportedError: pywin32 is not installed (not Windows); raised before any
            thread starts.
        BaseException: Whatever ``work`` or ``CoInitializeEx`` raised, unchanged (a pywin32
            ``com_error`` stays a ``com_error``; the caller maps it).

    Example:
        >>> class _Pythoncom:
        ...     COINIT_MULTITHREADED = 0
        ...     def CoInitializeEx(self, flags): pass
        ...     def CoUninitialize(self): pass
        >>> run_in_com_thread(lambda: threading.current_thread().name, load=lambda _: _Pythoncom())
        'pwshpy-com'
    """
    # Resolved per call, not as a default: a default is bound when the def runs.
    loader = load if load is not None else _load
    pythoncom = loader("pythoncom")
    outcome: _Outcome[_T] = _Outcome()
    # Daemon: a caller interrupted while it waits must not keep the process alive on the worker.
    worker = threading.Thread(target=_run, args=(work, pythoncom, outcome), name="pwshpy-com", daemon=True)
    worker.start()
    worker.join()
    if outcome.error is not None:
        raise outcome.error
    return outcome.results[0]


def _run(work: Callable[[], _T], pythoncom: Any, outcome: _Outcome[_T]) -> None:
    """Worker side: init the MTA, run ``work``, release its frames, uninit.

    What ``CoInitializeEx`` or ``work`` raises is recorded in ``outcome``, not raised. Only an
    exception from ``CoUninitialize`` in the ``finally`` escapes: it ends this thread through
    :func:`threading.excepthook` (a traceback on stderr), while the caller still receives the
    result or the error already recorded.
    """
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
    except BaseException as exc:  # handed to the caller, which owns the error mapping
        outcome.error = exc
        return  # no apartment was entered, so there is nothing to uninitialise
    try:
        outcome.results.append(work())
    except BaseException as exc:  # handed to the caller, which owns the error mapping
        _release_frames(exc)
        outcome.error = exc
    finally:
        pythoncom.CoUninitialize()


def _release_frames(exc: BaseException) -> None:
    """Clear the locals of every finished frame ``exc`` and its chained exceptions still hold.

    The traceback keeps the work's frames, and so its COM objects, alive past the uninit; the
    line numbers stay, only the locals go.
    """
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback.clear_frames(current.__traceback__)
        pending.extend(chained for chained in (current.__cause__, current.__context__) if chained is not None)


def _load(module: str) -> Any:
    """Import a pywin32 module lazily, so importing this module stays safe on every OS."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            f"in-process COM needs pywin32 (Windows only); {module} is unavailable."
        ) from exc
