"""Windows Update search over Microsoft.Update.Session, bounded by a timeout (Windows-only, read-only).

``IUpdateSearcher.Search`` is synchronous and was measured to sit 25+ minutes against an
empty update datastore, and a thread cannot stop a blocked COM call, so the search runs in
a killable ``spawn`` child (:func:`~pwshpy.adapters.native.bounded.run_bounded`).  COM
objects cannot cross a process boundary, so the child flattens each ``IUpdate`` to a plain
dict and the parent builds the records.

The child returns a COM failure as DATA (an error message beside empty rows) rather than
raising it: ``run_bounded`` keeps only the text of a child exception, so a raised error would
arrive as ``"NativeCallError: ..."`` with its HRESULT buried; returned, the parent raises one
clean :class:`~pwshpy.domain.errors.NativeCallError` naming the HRESULT.  Anything else the
child raises (a programming error) still arrives as a ``NativeCallError`` via ``run_bounded``.

Read-only: only ``Search`` is called - never ``Download``, ``Install`` or a settings change.

Contents:
    * :func:`search_updates` - the updates matching a WUA search criteria string.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from typing import Any, cast

from ...domain.errors import NativeCallError, OperationTimeoutError, PlatformUnsupportedError
from ...domain.records import WindowsUpdate
from .bounded import run_bounded

# DISP_E_EXCEPTION: IDispatch::Invoke reports that the callee raised, with the callee's own
# HRESULT in excepinfo[5] (scode).  The WU agent fails this way, so the outer code alone would
# read "Exception occurred" for every failure.
_DISP_E_EXCEPTION = -2147352567

# (error message, rows): an empty message means the search succeeded.
_Outcome = tuple[str, list[dict[str, Any]]]


def _strings(collection: Any) -> list[str]:
    """An IStringCollection as a list of str."""
    return [str(collection.Item(i)) for i in range(int(collection.Count))]


def _flatten(update: Any) -> dict[str, Any]:
    """One IUpdate as plain data (only picklable values cross the pipe)."""
    categories = update.Categories
    return {
        "update_id": str(update.Identity.UpdateID),
        "revision_number": int(update.Identity.RevisionNumber),
        "title": str(update.Title),
        "kb_article_ids": _strings(update.KBArticleIDs),
        "is_installed": bool(update.IsInstalled),
        "is_downloaded": bool(update.IsDownloaded),
        "is_hidden": bool(update.IsHidden),
        "is_mandatory": bool(update.IsMandatory),
        "msrc_severity": str(update.MsrcSeverity or ""),
        # MaxDownloadSize is a COM DECIMAL, which pywin32 hands over as decimal.Decimal.
        "max_download_size": int(update.MaxDownloadSize),
        "categories": [str(categories.Item(c).Name) for c in range(int(categories.Count))],
    }


def _com_failure_message(exc: BaseException) -> str:
    """Describe a ``pywintypes.com_error`` by its effective HRESULT (hex) and text.

    ``com_error.args`` is ``(hresult, strerror, excepinfo, argerror)``.
    """
    args: tuple[object, ...] = (*exc.args, None, None, None)
    code = args[0] if isinstance(args[0], int) else 0
    text = str(args[1] or "")
    if code == _DISP_E_EXCEPTION:
        code, text = _callee_failure(args[2], code=code, text=text)
    suffix = f" ({text.strip()})" if text.strip() else ""
    return f"Windows Update search failed: HRESULT 0x{code & 0xFFFFFFFF:08X}{suffix}"


def _callee_failure(excepinfo: object, *, code: int, text: str) -> tuple[int, str]:
    """The callee's (scode, description) from ``excepinfo``, else the outer pair unchanged.

    ``excepinfo`` is ``(wcode, source, description, helpfile, helpcontext, scode)``.
    """
    if not isinstance(excepinfo, tuple):
        return code, text
    fields = cast("tuple[object, ...]", excepinfo)
    if len(fields) < 6:  # noqa: PLR2004 - excepinfo's documented arity
        return code, text
    scode, description = fields[5], fields[2]
    return (scode if isinstance(scode, int) and scode else code), str(description or text)


def _windows_update_search(criteria: str) -> _Outcome:
    """Child-side: run the COM search; return (error message, rows), never raise a COM error."""
    pythoncom: Any = importlib.import_module("pythoncom")
    pythoncom.CoInitialize()
    try:
        return _search_rows(criteria)
    except Exception as exc:
        # Matched by name: pywintypes is importable only on Windows, and cim.py does the same.
        if type(exc).__name__ != "com_error":
            raise
        return _com_failure_message(exc), []
    finally:
        pythoncom.CoUninitialize()


def _search_rows(criteria: str) -> _Outcome:
    """Search and flatten; every COM object is local to this frame.

    So all of them are released when it returns, BEFORE the caller's ``CoUninitialize``: a COM
    reference still alive at that point is released into a torn-down apartment, and pywin32
    prints "Win32 exception occurred releasing IUnknown" to stderr for each one.
    """
    client: Any = importlib.import_module("win32com.client")
    updates = client.Dispatch("Microsoft.Update.Session").CreateUpdateSearcher().Search(criteria).Updates
    return "", [_flatten(updates.Item(i)) for i in range(int(updates.Count))]


def _rows_or_raise(outcome: _Outcome) -> list[dict[str, Any]]:
    """The child's rows, or its COM failure raised as NativeCallError."""
    error, rows = outcome
    if error:
        raise NativeCallError(error)
    return rows


def _to_update(row: dict[str, Any]) -> WindowsUpdate:
    """A child row as a record; trusted native data, so validation is skipped."""
    return WindowsUpdate.model_construct(
        update_id=row["update_id"],
        revision_number=row["revision_number"],
        title=row["title"],
        kb_article_ids=tuple(row["kb_article_ids"]),
        is_installed=row["is_installed"],
        is_downloaded=row["is_downloaded"],
        is_hidden=row["is_hidden"],
        is_mandatory=row["is_mandatory"],
        msrc_severity=row["msrc_severity"],
        max_download_size=row["max_download_size"],
        categories=tuple(row["categories"]),
    )


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
        NativeCallError: The Windows Update agent reported a COM failure (its HRESULT is
            in the message), or the child died or could not be started.

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
    for row in _rows_or_raise(outcome):
        yield _to_update(row)


__all__ = ["search_updates"]
