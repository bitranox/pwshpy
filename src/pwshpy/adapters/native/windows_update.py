"""Windows Update search over Microsoft.Update.Session, bounded by a timeout (Windows-only, read-only).

``IUpdateSearcher.Search`` is synchronous and was measured to sit 25+ minutes against an
empty update datastore, and a thread cannot stop a blocked COM call, so the search runs in
a killable ``spawn`` child (:func:`~pwshpy.adapters.native.bounded.run_bounded`).  COM
objects cannot cross a process boundary, so the child reads each ``IUpdate`` into a
:class:`~pwshpy.domain.records.WindowsUpdate` record, which pickles by its importable class
and crosses the pipe as plain data.

The child returns a COM failure as DATA (an error message and its HRESULT beside no updates)
rather than raising it: ``run_bounded`` keeps only the text of a child exception, so a raised
error would arrive as ``"NativeCallError: ..."`` with its HRESULT buried in the text; returned,
the HRESULT crosses the pipe as an int and the parent raises the error typed by it
(:class:`~pwshpy.domain.errors.NativeAccessDeniedError` for E_ACCESSDENIED, else
:class:`~pwshpy.domain.errors.NativeCallError`), ``.hresult`` set.  Anything else the child
raises (a programming error) still arrives as a ``NativeCallError`` via ``run_bounded``.

Read-only: only ``Search`` is called - never ``Download``, ``Install`` or a settings change.

Contents:
    * :func:`search_updates` - the updates matching a WUA search criteria string.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator
from typing import Any, NamedTuple

from ...domain.errors import OperationTimeoutError, PlatformUnsupportedError
from ...domain.records import UpdateCategory, WindowsUpdate
from .bounded import run_bounded
from .marshal import com_error_text, com_hresult, is_com_error, native_error_for_hresult


class _Outcome(NamedTuple):
    """What the child sends back; module-level so it pickles by name across the spawn pipe."""

    error: str  # empty means the search succeeded
    updates: tuple[WindowsUpdate, ...]
    # The failure's unsigned HRESULT, carried as a plain int because the COM error itself
    # cannot cross the pipe; None on success or when the error carried no code.
    hresult: int | None = None


def _strings(collection: Any) -> list[str]:
    """An IStringCollection as a list of str."""
    return [str(collection.Item(i)) for i in range(int(collection.Count))]


def _category(category: Any) -> UpdateCategory:
    """One ICategory as a record; trusted native data, so validation is skipped.

    The GUID is lower-cased so it compares equal however the agent spelled it: the WSUS
    classification table publishes these GUIDs in upper case, the agent was read returning
    lower case, and a caller filtering on either must match.
    """
    return UpdateCategory.model_construct(
        category_id=str(category.CategoryID).lower(), type=str(category.Type), name=str(category.Name)
    )


def _to_update(update: Any) -> WindowsUpdate:
    """One IUpdate as a record of plain values only (no COM object may cross the pipe).

    Trusted native data, so validation is skipped.
    """
    categories = update.Categories
    details = tuple(_category(categories.Item(c)) for c in range(int(categories.Count)))
    return WindowsUpdate.model_construct(
        update_id=str(update.Identity.UpdateID),
        revision_number=int(update.Identity.RevisionNumber),
        title=str(update.Title),
        kb_article_ids=tuple(_strings(update.KBArticleIDs)),
        is_installed=bool(update.IsInstalled),
        is_downloaded=bool(update.IsDownloaded),
        is_hidden=bool(update.IsHidden),
        is_mandatory=bool(update.IsMandatory),
        msrc_severity=str(update.MsrcSeverity or ""),
        # MaxDownloadSize is a COM DECIMAL, which pywin32 hands over as decimal.Decimal.
        max_download_size=int(update.MaxDownloadSize),
        categories=tuple(detail.name for detail in details),
        category_details=details,
    )


def _com_failure_message(exc: BaseException) -> str:
    """Describe a pywin32 error by its effective HRESULT (hex) and text.

    The WU agent fails as DISP_E_EXCEPTION with its own code and description in excepinfo, so
    both are read through the marshaling seam, which unwraps them.
    """
    code = com_hresult(exc)
    text = com_error_text(exc).strip()
    suffix = f" ({text})" if text else ""
    if code is None:
        return f"Windows Update search failed without an HRESULT{suffix}"
    return f"Windows Update search failed: HRESULT 0x{code:08X}{suffix}"


def _windows_update_search(criteria: str, load: Callable[[str], object] | None = None) -> _Outcome:
    """Child-side: run the COM search; return its outcome, never raise a COM error.

    ``load`` imports the pywin32 modules (``None``: :func:`importlib.import_module`). It is the
    seam for the external edge, and it crosses the spawn pipe as a pickled argument, so a
    replacement must be a module-level function.
    """
    loader = load if load is not None else importlib.import_module
    pythoncom: Any = loader("pythoncom")
    pythoncom.CoInitialize()
    try:
        return _search_updates(criteria, loader)
    except Exception as exc:
        if not is_com_error(exc):
            raise
        return _Outcome(error=_com_failure_message(exc), updates=(), hresult=com_hresult(exc))
    finally:
        pythoncom.CoUninitialize()


def _search_updates(criteria: str, load: Callable[[str], object]) -> _Outcome:
    """Search and read each update into a record; every COM object is local to this frame.

    So all of them are released when it returns, BEFORE the caller's ``CoUninitialize``: a COM
    reference still alive at that point is released into a torn-down apartment, and pywin32
    prints "Win32 exception occurred releasing IUnknown" to stderr for each one.
    """
    client: Any = load("win32com.client")
    updates = client.Dispatch("Microsoft.Update.Session").CreateUpdateSearcher().Search(criteria).Updates
    return _Outcome(error="", updates=tuple(_to_update(updates.Item(i)) for i in range(int(updates.Count))))


def _updates_or_raise(outcome: _Outcome) -> tuple[WindowsUpdate, ...]:
    """The child's updates, or its COM failure raised as the error its HRESULT types.

    No ``from`` cause: the COM exception lived in the child and never crossed the pipe.
    """
    if outcome.error:
        raise native_error_for_hresult(outcome.error, outcome.hresult)
    return outcome.updates


def search_updates(criteria: str, *, timeout: float) -> Iterator[WindowsUpdate]:
    """Yield the updates matching ``criteria``, searched in a child killed after ``timeout`` s.

    A generator: nothing runs until the first item is requested.  The child spawns with
    the ``spawn`` start method, so a script calling this must guard its top level with
    ``if __name__ == "__main__":``.

    Args:
        criteria: A WUA search criteria string, e.g. ``"IsInstalled=0 and Type='Software'"``.
        timeout: Seconds to wait for the search before the child is killed.

    Yields:
        One :class:`WindowsUpdate` per update the search returned.

    Raises:
        PlatformUnsupportedError: Off Windows (raised before any child is started).
        OperationTimeoutError: The search outlived ``timeout``; the child was killed.
        NativeAccessDeniedError: The Windows Update agent denied the search
            (E_ACCESSDENIED); a ``NativeCallError`` and a ``PermissionError``.
        NativeCallError: The Windows Update agent reported another COM failure (its HRESULT
            is in the message and on ``.hresult``), or the child died or could not be started.

    Example:
        >>> callable(search_updates)
        True
    """
    if sys.platform != "win32":
        raise PlatformUnsupportedError("Windows Update search is Windows-only.")
    try:
        outcome: _Outcome = run_bounded(_windows_update_search, (criteria,), timeout=timeout)
    except OperationTimeoutError as exc:
        # run_bounded names the child function; say what timed out in the caller's terms.
        raise OperationTimeoutError(
            f"Windows Update search timed out after {exc.timeout:g}s", timeout=exc.timeout
        ) from exc
    yield from _updates_or_raise(outcome)


__all__ = ["search_updates"]
