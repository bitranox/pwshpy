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

Read-only methods: :func:`invoke_cim_method` mirrors ``Invoke-CimMethod`` for the methods on
:data:`~pwshpy.domain.cim_methods.READ_ONLY_CIM_METHODS` only, refusing any other before WMI is
contacted (see that module for why a fixed list rather than a name rule). Its in-parameters are
checked against the method's own schema and typed from it; an argument's VALUE never appears in
an error message, because some of them identify secrets (a BitLocker key protector ID).

Unsigned integers: the WBEM scripting API hands a ``uint8``/``uint16``/``uint32`` over as a
SIGNED int of the same width (a ``uint32`` of ``0xFFFFFFFF`` arrives as ``-1``, measured on
``GetConversionStatus``'s ``WipingStatus``), so both functions read each property's ``CIMType``
and report it unsigned, as ``Get-CimInstance`` does. ``uint64``/``sint64`` arrive as strings and
stay strings.

Contents:
    * :func:`iter_cim` - stream the instances of a WMI class as CimInstance records.
    * :func:`invoke_cim_method` - call an allowlisted read-only method, one CimMethodResult per instance.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator, Mapping
from typing import Any, cast

from ...domain.cim_methods import CimArgumentValue, is_read_only_cim_method
from ...domain.errors import CimMethodNotAllowedError, NativeCallError, PlatformUnsupportedError
from ...domain.records import CimInstance, CimMethodResult
from .marshal import com_hresult, com_outer_hresult, is_com_error, native_error

# Matched with fullmatch: `$` alone would also accept a name followed by one trailing newline.
_CLASS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEFAULT_NAMESPACE = "root/cimv2"
# wbemFlagReturnImmediately (0x10) | wbemFlagForwardOnly (0x20): a lazy, forward-only enumerator.
_LAZY_FLAGS = 0x10 | 0x20

# WbemCimtypeEnum values the method path reads.
_CIM_SINT16, _CIM_SINT32, _CIM_STRING, _CIM_BOOLEAN = 2, 3, 8, 11
_CIM_SINT8, _CIM_UINT8, _CIM_UINT16, _CIM_UINT32, _CIM_SINT64, _CIM_UINT64 = 16, 17, 18, 19, 20, 21
# The unsigned types WBEM scripting hands over as a signed int of the same width.
_UNSIGNED_BITS = {_CIM_UINT8: 8, _CIM_UINT16: 16, _CIM_UINT32: 32}
_INTEGER_RANGES = {
    _CIM_SINT8: (-(2**7), 2**7 - 1),
    _CIM_UINT8: (0, 2**8 - 1),
    _CIM_SINT16: (-(2**15), 2**15 - 1),
    _CIM_UINT16: (0, 2**16 - 1),
    _CIM_SINT32: (-(2**31), 2**31 - 1),
    _CIM_UINT32: (0, 2**32 - 1),
    _CIM_SINT64: (-(2**63), 2**63 - 1),
    _CIM_UINT64: (0, 2**64 - 1),
}
# WBEM scripting takes a 64-bit integer as its decimal string (VARIANT has no portable 64-bit form).
_STRING_INTEGERS = frozenset({_CIM_SINT64, _CIM_UINT64})
_DECIMAL_RE = re.compile(r"-?[0-9]+")
_BOOLEAN_WORDS = {"true": True, "false": False}
_NO_TEXT = "WMI gave no error text"


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
        raise native_error(str(exc) or f"cannot connect to WMI namespace {namespace!r}", exc) from exc


def _marshal_value(value: Any) -> Any:
    """Normalize a WMI property value to a JSON-friendly type (arrays -> lists)."""
    if isinstance(value, tuple):
        return [_marshal_value(item) for item in cast("tuple[Any, ...]", value)]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)  # embedded objects / decimals / exotic COM types -> string


def _unsigned(value: Any, bits: int) -> Any:
    """``value`` read as an unsigned integer of ``bits`` (a list element-wise); other values as is."""
    if isinstance(value, list):
        return [_unsigned(item, bits) for item in cast("list[Any]", value)]
    if isinstance(value, int) and not isinstance(value, bool) and value < 0:
        return value & ((1 << bits) - 1)
    return value


def _marshal_property(prop: Any) -> Any:
    """A WBEM property's value, JSON-friendly, with an unsigned type reported unsigned."""
    value = _marshal_value(prop.Value)
    bits = _UNSIGNED_BITS.get(prop.CIMType)
    return value if bits is None else _unsigned(value, bits)


def _properties(obj: Any) -> dict[str, Any]:
    return {prop.Name: _marshal_property(prop) for prop in obj.Properties_}


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
    if not _CLASS_RE.fullmatch(class_name):
        raise NativeCallError(f"invalid WMI class name: {class_name!r}")
    service = _load_wbem_service(namespace)
    # Read-only WQL; class_name is validated above and `where` is a documented trust boundary.
    wql = f"SELECT * FROM {class_name}" + (f" WHERE {where}" if where else "")  # noqa: S608 # nosec B608
    try:
        for obj in service.ExecQuery(wql, "WQL", _LAZY_FLAGS):
            yield CimInstance.model_construct(class_name=class_name, properties=_properties(obj))
    except Exception as exc:
        if not is_com_error(exc):
            raise
        raise native_error(_query_failure(exc, wql), exc) from exc


def _query_failure(exc: BaseException, wql: str) -> str:
    """The error's own text, plus its HRESULT when one is readable (the text may bury it)."""
    return _failure_text(exc, f"WMI query failed: {wql!r}")


def _failure_text(exc: BaseException, fallback: str) -> str:
    """The error's own text (``fallback`` when it has none), plus its HRESULT when readable.

    ``str(exc)`` leads with the error's outer code, so a code unwrapped from a
    DISP_E_EXCEPTION is labelled as the callee's: two unlabelled codes would read as a
    contradiction.
    """
    message = str(exc) or fallback
    code = com_hresult(exc)
    if code is None:
        return message
    label = "HRESULT" if code == com_outer_hresult(exc) else "callee HRESULT"
    return f"{message} ({label} 0x{code:08X})"


def invoke_cim_method(
    class_name: str,
    method_name: str,
    *,
    where: str | None = None,
    namespace: str = _DEFAULT_NAMESPACE,
    arguments: Mapping[str, CimArgumentValue] | None = None,
) -> Iterator[CimMethodResult]:
    """Call a read-only WMI method on each matching instance (like ``Invoke-CimMethod``).

    The names and the allowlist are checked HERE, when called, so a refused method never
    contacts WMI; the method runs only while the returned iterator is consumed, one instance at a
    time. A static method (``StdRegProv.GetStringValue``) runs once, on the class.

    Args:
        class_name: The WMI class; letters, digits and underscores only.
        method_name: The method; letters, digits and underscores only.
        where: A WQL condition selecting the instances (None: every instance). Not escaped, so
            never build it from untrusted input; a static method takes none.
        namespace: The WMI namespace (default ``"root/cimv2"``).
        arguments: In-parameters by name (any case). Each value is checked against the
            parameter's CIM type: a ``str`` for a string, an ``int`` or a decimal ``str`` for an
            integer (range-checked), a ``bool`` or ``"true"``/``"false"`` for a boolean.

    Returns:
        A lazy iterator of :class:`CimMethodResult` records.

    Raises:
        CimMethodNotAllowedError: the method is not on the read-only allowlist (raised at once).
        NativeCallError: a name is not an identifier or an argument value is not a str, int or
            bool (at once); later, the namespace, class or method cannot be read, an argument
            does not fit the method's schema (the message names the argument, never its value),
            or WMI could not make the call. A ``ReturnValue`` other than 0 is NOT an error: it is
            the record's ``return_value``.
        PlatformUnsupportedError: pywin32 is not installed (any OS but Windows).

    Example:
        >>> try:
        ...     invoke_cim_method("Win32_Process", "Terminate")
        ... except CimMethodNotAllowedError as exc:
        ...     print(type(exc).__name__)
        CimMethodNotAllowedError
    """
    _require_identifier("class", class_name)
    _require_identifier("method", method_name)
    if not is_read_only_cim_method(namespace, class_name, method_name):
        raise CimMethodNotAllowedError(
            f"{class_name}.{method_name} in namespace {namespace!r} is not on pwshpy's read-only CIM "
            "method allowlist; invoke_cim_method calls only methods known to change nothing "
            "(pwshpy.domain.cim_methods.READ_ONLY_CIM_METHODS)"
        )
    checked = _checked_arguments(f"{class_name}.{method_name}", arguments or {})
    return _run_method(class_name, method_name, where=where, namespace=namespace, arguments=checked)


def _require_identifier(kind: str, name: str) -> None:
    if not _CLASS_RE.fullmatch(name):
        raise NativeCallError(f"invalid WMI {kind} name: {name!r}")


def _checked_arguments(label: str, arguments: Mapping[str, object]) -> dict[str, CimArgumentValue]:
    """A private copy of ``arguments``, refusing a value that is not a str, int or bool.

    Typed ``object`` on purpose: this is where a caller's untyped value is checked at run time.
    """
    checked: dict[str, CimArgumentValue] = {}
    for name, value in arguments.items():
        if not isinstance(value, (str, int)):  # bool is an int
            raise NativeCallError(f"{label}: argument {name!r} must be a str, int or bool")
        checked[name] = value
    return checked


def _run_method(
    class_name: str, method_name: str, *, where: str | None, namespace: str, arguments: dict[str, CimArgumentValue]
) -> Iterator[CimMethodResult]:
    label = f"{class_name}.{method_name}"
    service = _load_wbem_service(namespace)
    try:
        klass = service.Get(class_name)
        method = klass.Methods_.Item(method_name)
        static = _is_static(method)
        in_params = _in_parameters(method, label, arguments)
    except Exception as exc:
        if not is_com_error(exc):
            raise
        raise native_error(f"cannot read the definition of {label}: {_failure_text(exc, _NO_TEXT)}", exc) from exc
    if static:
        if where is not None:
            raise NativeCallError(f"{label} is a static method: it runs on the class and takes no where filter")
        yield _call(klass, class_name, method_name, in_params, instance_path=class_name)
        return
    wql = f"SELECT * FROM {class_name}" + (f" WHERE {where}" if where else "")  # noqa: S608 # nosec B608
    try:
        for obj in service.ExecQuery(wql, "WQL", _LAZY_FLAGS):
            yield _call(obj, class_name, method_name, in_params, instance_path=str(obj.Path_.RelPath))
    except Exception as exc:
        if not is_com_error(exc):
            raise
        raise native_error(_query_failure(exc, wql), exc) from exc


def _is_static(method: Any) -> bool:
    """Whether the method carries the ``Static`` qualifier (it then runs on the class, not an instance)."""
    return any(qualifier.Name.lower() == "static" and bool(qualifier.Value) for qualifier in method.Qualifiers_)


def _in_parameters(method: Any, label: str, arguments: dict[str, CimArgumentValue]) -> Any:
    """The method's in-parameter object with ``arguments`` set, typed from its schema; None when it takes none.

    Raises:
        NativeCallError: an argument the method does not declare, or a value its type cannot hold.
            The message names the argument, never its value.
    """
    definition = method.InParameters
    if definition is None:
        if arguments:
            raise NativeCallError(f"{label} takes no arguments; got {', '.join(sorted(arguments))}")
        return None
    params = definition.SpawnInstance_()
    declared = {prop.Name.lower(): prop for prop in params.Properties_}
    for name, value in arguments.items():
        prop = declared.get(name.lower())
        if prop is None:
            known = ", ".join(sorted(p.Name for p in declared.values())) or "none"
            raise NativeCallError(f"{label} has no in-parameter {name!r}; it takes: {known}")
        params.Properties_.Item(prop.Name).Value = _wire_value(label, prop.Name, prop.CIMType, value)
    return params


def _wire_value(label: str, name: str, cim_type: int, value: CimArgumentValue) -> Any:
    """``value`` in the form WBEM scripting expects for ``cim_type``; refuses a value that does not fit."""
    if cim_type == _CIM_BOOLEAN:
        return _boolean(label, name, value)
    if cim_type in _INTEGER_RANGES:
        return _integer(label, name, cim_type, value)
    if cim_type == _CIM_STRING and isinstance(value, str):
        return value
    if cim_type == _CIM_STRING:
        raise NativeCallError(f"{label}: argument {name!r} is a string parameter and takes a str")
    raise NativeCallError(f"{label}: argument {name!r} has CIM type {cim_type}, which invoke_cim_method does not set")


def _boolean(label: str, name: str, value: CimArgumentValue) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in _BOOLEAN_WORDS:
        return _BOOLEAN_WORDS[value.lower()]
    raise NativeCallError(f"{label}: argument {name!r} is a boolean parameter and takes a bool or 'true'/'false'")


def _integer(label: str, name: str, cim_type: int, value: CimArgumentValue) -> Any:
    number = _as_int(value)
    low, high = _INTEGER_RANGES[cim_type]
    if number is None or not low <= number <= high:
        raise NativeCallError(
            f"{label}: argument {name!r} is an integer parameter and takes an int (or decimal str) from {low} to {high}"
        )
    if cim_type in _STRING_INTEGERS:
        return str(number)
    bits = _UNSIGNED_BITS.get(cim_type)
    # The signed form of the same width, as WBEM scripting itself hands an unsigned value over.
    return number - (1 << bits) if bits is not None and number >= 1 << (bits - 1) else number


def _as_int(value: CimArgumentValue) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if _DECIMAL_RE.fullmatch(value) else None


def _call(target: Any, class_name: str, method_name: str, in_params: Any, *, instance_path: str) -> CimMethodResult:
    label = f"{class_name}.{method_name}"
    try:
        out = target.ExecMethod_(method_name) if in_params is None else target.ExecMethod_(method_name, in_params)
        values = {} if out is None else _properties(out)
    except Exception as exc:
        if not is_com_error(exc):
            raise
        raise native_error(f"{label} failed on {instance_path}: {_failure_text(exc, _NO_TEXT)}", exc) from exc
    return_value = values.pop("ReturnValue", None)
    if return_value is not None and (isinstance(return_value, bool) or not isinstance(return_value, int)):
        values["ReturnValue"] = return_value  # a method whose ReturnValue is not an integer keeps it as data
        return_value = None
    return CimMethodResult.model_construct(
        class_name=class_name,
        method_name=method_name,
        instance_path=instance_path,
        return_value=return_value,
        out_parameters=values,
    )


__all__ = ["invoke_cim_method", "iter_cim"]
