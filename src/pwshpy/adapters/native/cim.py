"""native CIM/WMI source over win32com WBEM (Windows-only), streaming and memory-bounded.

Queries the WMI repository with WQL through the WBEM scripting API, mirroring
``Get-CimInstance``.  The query uses the forward-only + return-immediately flags,
so the adapter is a GENERATOR that pulls instances lazily one at a time, keeping
the CLIENT side memory-bounded.  Caveat: a few providers scan their whole scope
SERVER-side before the first row - ``CIM_DataFile`` enumerates every file and can
block for minutes, so narrow it with a ``where`` filter; ``.take(n)`` alone cannot
bound a server-side scan.  ``win32com`` (from pywin32) is imported lazily so a
portable install stays clean and raises
:class:`~pwshpy.domain.errors.PlatformUnsupportedError`.

Security: ``where`` is a WQL filter fragment supplied by the caller and spliced
into a read-only ``SELECT``; WQL cannot mutate state, and the class name is
validated as an identifier, but as with any query interface do not build the
filter from untrusted input.

Contents:
    * :func:`iter_cim` - stream the instances of a WMI class as CimInstance records.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator
from typing import Any, cast

from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import CimInstance

_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_NAMESPACE = "root/cimv2"
# wbemFlagReturnImmediately (0x10) | wbemFlagForwardOnly (0x20): a lazy, forward-only enumerator.
_LAZY_FLAGS = 0x10 | 0x20


def _load_wbem_service(namespace: str) -> Any:
    """Connect to a WMI namespace via the WBEM scripting locator (lazy pywin32 import)."""
    try:
        win32com_client: Any = importlib.import_module("win32com.client")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "The CIM/WMI subsystem requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc
    try:
        locator = win32com_client.Dispatch("WbemScripting.SWbemLocator")
        return locator.ConnectServer(".", namespace)
    except Exception as exc:
        raise NativeCallError(str(exc) or f"cannot connect to WMI namespace {namespace!r}") from exc


def _marshal_value(value: Any) -> Any:
    """Normalize a WMI property value to a JSON-friendly type (arrays -> lists)."""
    if isinstance(value, tuple):
        return [_marshal_value(item) for item in cast("tuple[Any, ...]", value)]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)  # embedded objects / decimals / exotic COM types -> string


def _properties(obj: Any) -> dict[str, Any]:
    return {prop.Name: _marshal_value(prop.Value) for prop in obj.Properties_}


def iter_cim(
    class_name: str, *, where: str | None = None, namespace: str = _DEFAULT_NAMESPACE
) -> Iterator[CimInstance]:
    """Stream the instances of a WMI class as :class:`CimInstance` records (like ``Get-CimInstance``).

    Yields one record at a time from a forward-only enumerator, so the client
    stays memory-bounded on a large class (``Win32_Process``, ...).  A provider
    that scans server-side first (``CIM_DataFile``) still needs a ``where`` filter
    to be fast - see the module docstring.

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import CimInstance
        >>> sys.platform != "win32" or isinstance(next(iter_cim("Win32_OperatingSystem")), CimInstance)
        True
    """
    if not _CLASS_RE.match(class_name):
        raise NativeCallError(f"invalid WMI class name: {class_name!r}")
    service = _load_wbem_service(namespace)
    # Read-only WQL; class_name is validated above and `where` is a documented trust boundary.
    wql = f"SELECT * FROM {class_name}" + (f" WHERE {where}" if where else "")  # noqa: S608 # nosec B608
    try:
        for obj in service.ExecQuery(wql, "WQL", _LAZY_FLAGS):
            yield CimInstance.model_construct(class_name=class_name, properties=_properties(obj))
    except Exception as exc:
        if type(exc).__name__ not in {"com_error", "error"}:
            raise
        raise NativeCallError(str(exc) or f"WMI query failed: {wql!r}") from exc


__all__ = ["iter_cim"]
