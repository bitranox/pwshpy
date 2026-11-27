"""CIM/WMI native adapter over win32com WBEM.

A fake WBEM service (injected at the adapter's load seam) exercises property
marshaling, WQL construction, class-name validation, and the streaming/laziness
contract on every OS; a real structural test queries Win32_OperatingSystem on
Windows.  Exact live behaviour is pinned against Get-CimInstance in
``test_cim_pwsh_oracle``.
"""

from __future__ import annotations

import struct
import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from com_fakes import codeless_com_error, com_error_for, dispatch_error, error, signed

from pwshpy.adapters.native import cim as cim_mod
from pwshpy.adapters.native.cim import iter_cim
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import CimInstance

_CIM_STRING = 8  # every real WBEM property carries a CIMType; a plain fake value is a string
# The by-name reader is checked against the list read on real WMI, so the test names both.
_PropertyReader = cim_mod._PropertyReader  # pyright: ignore[reportPrivateUsage]
_properties = cim_mod._properties  # pyright: ignore[reportPrivateUsage]
_load_wbem_service = cim_mod._load_wbem_service  # pyright: ignore[reportPrivateUsage]


class _FakeProp:
    def __init__(self, name: str, value: Any, cim_type: int = _CIM_STRING) -> None:
        self.Name = name
        self.Value = value
        self.CIMType = cim_type


class _FakeObj:
    def __init__(self, props: dict[str, Any], cim_types: dict[str, int] | None = None) -> None:
        types = cim_types or {}
        self.Properties_ = [_FakeProp(name, value, types.get(name, _CIM_STRING)) for name, value in props.items()]


class _FakeService:
    def __init__(self, objs: list[_FakeObj]) -> None:
        self._objs = objs
        self.consumed = 0
        self.last_wql: str | None = None

    def ExecQuery(self, wql: str, lang: str, flags: int) -> Iterator[_FakeObj]:  # noqa: N802 - WBEM API name
        self.last_wql = wql

        def _lazy() -> Iterator[_FakeObj]:
            for obj in self._objs:
                self.consumed += 1
                yield obj

        return _lazy()


def _loader(service: Any) -> Callable[[str], Any]:
    """A typed stand-in for cim._load_wbem_service that ignores the namespace."""

    def _load(_namespace: str) -> Any:
        return service

    return _load


@pytest.mark.os_agnostic
def test_iter_cim_marshals_properties_and_builds_wql(monkeypatch: pytest.MonkeyPatch) -> None:
    """Property values are normalized (arrays -> lists) and the WHERE clause is spliced in."""
    service = _FakeService([_FakeObj({"Name": "A", "Count": 3, "Langs": ("de", "en"), "Flag": True, "Empty": None})])
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(service))

    items = list(iter_cim("Win32_Thing", where="Count = 3"))
    assert len(items) == 1
    instance = items[0]
    assert instance.class_name == "Win32_Thing"
    assert instance.properties == {"Name": "A", "Count": 3, "Langs": ["de", "en"], "Flag": True, "Empty": None}
    assert service.last_wql == "SELECT * FROM Win32_Thing WHERE Count = 3"
    # the open bag serializes as a plain nested dict
    assert instance.to_dict()["properties"]["Langs"] == ["de", "en"]


@pytest.mark.os_agnostic
def test_iter_cim_reports_unsigned_integers_unsigned(monkeypatch: pytest.MonkeyPatch) -> None:
    """WBEM scripting hands a uint8/16/32 over signed; the record carries the unsigned value.

    Measured on Windows: GetConversionStatus's uint32 WipingStatus of 0xFFFFFFFF arrived as -1.
    Signed types keep their sign, and a uint64 (a string in WBEM scripting) is left as is.
    """
    props = {
        "U32": -1,
        "U16": -2,
        "U8": -128,
        "U32s": (-1, 7),
        "S32": -5,
        "U64": "18446744073709551615",
        "Ok": 42,
    }
    types = {"U32": 19, "U16": 18, "U8": 17, "U32s": 19, "S32": 3, "U64": 21, "Ok": 19}
    service = _FakeService([_FakeObj(props, types)])
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(service))
    instance = next(iter_cim("Win32_Thing"))
    assert instance.properties == {
        "U32": 0xFFFFFFFF,
        "U16": 0xFFFE,
        "U8": 0x80,
        "U32s": [0xFFFFFFFF, 7],
        "S32": -5,
        "U64": "18446744073709551615",
        "Ok": 42,
    }


@pytest.mark.os_agnostic
def test_iter_cim_without_filter_omits_where(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _FakeService([_FakeObj({"n": 1})])
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(service))
    list(iter_cim("Win32_Thing"))
    assert service.last_wql == "SELECT * FROM Win32_Thing"


@pytest.mark.os_agnostic
def test_iter_cim_is_lazy_and_memory_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Consuming one record pulls exactly one instance - a huge class never materializes."""
    service = _FakeService([_FakeObj({"n": 1}), _FakeObj({"n": 2}), _FakeObj({"n": 3})])
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(service))
    first = next(iter_cim("Win32_Thing"))
    assert first.properties == {"n": 1}
    assert service.consumed == 1  # instances 2 and 3 were NOT pulled


@pytest.mark.os_agnostic
def test_iter_cim_rejects_bad_class_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-identifier class name is refused before any query (injection guard)."""
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_FakeService([])))
    with pytest.raises(NativeCallError):
        list(iter_cim("Win32_Thing; DROP"))


@pytest.mark.os_agnostic
@pytest.mark.parametrize("class_name", ["Win32_Process\n", "Win32_Process\r\n", "\nWin32_Process", ""])
def test_iter_cim_rejects_a_class_name_with_a_line_break(monkeypatch: pytest.MonkeyPatch, class_name: str) -> None:
    """The whole name must be an identifier: a trailing newline is not "letters, digits and underscores"."""
    service = _FakeService([])
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(service))
    with pytest.raises(NativeCallError, match="invalid WMI class name"):
        list(iter_cim(class_name))
    assert service.last_wql is None


class _Boom:
    """A WBEM service whose query raises ``failure``."""

    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    def ExecQuery(self, *args: Any) -> Iterator[Any]:  # noqa: N802 - WBEM API name
        raise self._failure


_WBEM_E_INVALID_CLASS = 0x80041010


@pytest.mark.os_agnostic
def test_iter_cim_wraps_query_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A COM/query failure surfaces as NativeCallError, keeping the error's own text."""
    failure = com_error_for(_WBEM_E_INVALID_CLASS, "bad query")
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_Boom(failure)))
    with pytest.raises(NativeCallError) as info:
        list(iter_cim("Win32_Thing"))
    assert str(failure) in str(info.value)
    assert info.value.__cause__ is failure


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("failure", "suffix"),
    [
        (com_error_for(_WBEM_E_INVALID_CLASS), " (HRESULT 0x80041010)"),
        (error(5, "ExecQuery", "Access is denied."), " (HRESULT 0x80070005)"),
        (dispatch_error(0, "Invalid class "), " (HRESULT 0x80020009)"),  # no callee code: the outer one stands
    ],
    ids=["direct", "win32", "dispatch-without-scode"],
)
def test_iter_cim_names_the_hresult_the_error_carries(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException, suffix: str
) -> None:
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_Boom(failure)))
    with pytest.raises(NativeCallError) as info:
        list(iter_cim("Win32_Thing"))
    assert str(info.value) == f"{failure}{suffix}"


@pytest.mark.os_agnostic
def test_iter_cim_names_an_unwrapped_code_as_the_callees(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DISP_E_EXCEPTION's scode is the provider's own code, not the one str(exc) leads with."""
    failure = dispatch_error(signed(_WBEM_E_INVALID_CLASS), "Invalid class ")
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_Boom(failure)))
    with pytest.raises(NativeCallError) as info:
        list(iter_cim("Win32_Thing"))
    assert str(info.value) == f"{failure} (callee HRESULT 0x80041010)"


@pytest.mark.os_agnostic
def test_iter_cim_names_no_hresult_when_none_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_Boom(codeless_com_error())))
    with pytest.raises(NativeCallError) as info:
        list(iter_cim("Win32_Thing"))
    assert "HRESULT" not in str(info.value)
    assert str(info.value) == "WMI query failed: 'SELECT * FROM Win32_Thing'"


@pytest.mark.os_agnostic
def test_iter_cim_lets_a_foreign_error_class_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """``struct.error`` shares pywin32's class name ``error`` but is not a WMI answer."""
    failure = struct.error("not pywin32's")
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_Boom(failure)))
    with pytest.raises(struct.error) as info:
        list(iter_cim("Win32_Thing"))
    assert info.value is failure


@pytest.mark.os_agnostic
def test_iter_cim_lets_marshaling_bug_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-COM error (e.g. a marshaling bug) propagates with its own type, not mislabeled."""

    class _BadObj:
        @property
        def Properties_(self) -> Any:  # noqa: N802 - WBEM API name
            raise RuntimeError("marshaling defect")

    class _Service:
        def ExecQuery(self, *args: Any) -> Iterator[Any]:  # noqa: N802 - WBEM API name
            return iter([_BadObj()])

    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_Service()))
    with pytest.raises(RuntimeError):
        list(iter_cim("Win32_Thing"))


@pytest.mark.os_agnostic
def test_iter_cim_stringifies_exotic_property(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-JSON-primitive property value falls back to str() (e.g. an embedded COM object)."""

    class _Weird:
        def __str__(self) -> str:
            return "weird-value"

    service = _FakeService([_FakeObj({"X": _Weird()})])
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(service))
    instance = next(iter_cim("Win32_Thing"))
    assert instance.properties["X"] == "weird-value"


@pytest.mark.os_agnostic
def test_iter_cim_forwards_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The namespace argument is passed through to the WBEM connection."""
    captured: list[str] = []

    def loader(namespace: str) -> _FakeService:
        captured.append(namespace)
        return _FakeService([_FakeObj({"n": 1})])

    monkeypatch.setattr(cim_mod, "_load_wbem_service", loader)
    list(iter_cim("Win32_Thing", namespace="root/custom"))
    assert captured == ["root/custom"]


@pytest.mark.os_agnostic
def test_iter_cim_wraps_connect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ConnectServer failure (bad namespace / WMI down) surfaces as NativeCallError."""

    class _Locator:
        def ConnectServer(self, server: Any, namespace: Any) -> Any:  # noqa: N802 - WBEM API name
            raise OSError("invalid namespace")

    class _Client:
        @staticmethod
        def Dispatch(progid: str) -> _Locator:  # noqa: N802 - win32com API name
            return _Locator()

    real_import = cim_mod.importlib.import_module

    def fake_import(name: str) -> Any:
        return _Client if name == "win32com.client" else real_import(name)

    monkeypatch.setattr(cim_mod.importlib, "import_module", fake_import)
    with pytest.raises(NativeCallError):
        list(iter_cim("Win32_Thing", namespace="root/nope"))


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="WMI is Windows-only")
def test_iter_cim_reads_real_os_class() -> None:
    """Against the real WMI repository, the adapter yields a typed CimInstance."""
    instance = next(iter_cim("Win32_OperatingSystem"))
    assert isinstance(instance, CimInstance)
    assert instance.class_name == "Win32_OperatingSystem"
    assert "Version" in instance.properties


# --- reading values by name: each class's property list is read once per query -------------

_DISP_E_UNKNOWNNAME = 0x80020006


class _FakeRawDispatch:
    """A raw IDispatch (what ``_oleobj_`` holds): dispids by name, a property get by dispid."""

    def __init__(self, members: dict[str, Any], *, refuse: frozenset[str] = frozenset()) -> None:
        self._names = list(members)
        self._members = members
        self._refuse = refuse

    def GetIDsOfNames(self, name: str) -> int:  # noqa: N802 - IDispatch API name
        if name in self._refuse or name not in self._members:
            raise com_error_for(_DISP_E_UNKNOWNNAME)
        return self._names.index(name)

    def Invoke(self, dispid: int, lcid: int, flags: int, want_result: bool, *args: Any) -> Any:  # noqa: N802
        return self._members[self._names[dispid]]


class _CountingObj:
    """A WBEM object of class ``class_name`` that counts each walk of its ``Properties_`` list."""

    def __init__(
        self,
        class_name: str,
        props: dict[str, Any],
        cim_types: dict[str, int] | None = None,
        *,
        refuse: frozenset[str] = frozenset(),
        dispatch_type: type[_FakeRawDispatch] = _FakeRawDispatch,
    ) -> None:
        self._listed = _FakeObj(props, cim_types).Properties_
        self.walks = 0
        path = _FakeRawDispatch({"Class": class_name})
        self._oleobj_ = dispatch_type({**props, "Path_": path}, refuse=refuse)

    @property
    def Properties_(self) -> list[_FakeProp]:  # noqa: N802 - WBEM API name
        self.walks += 1
        return self._listed


@pytest.mark.os_agnostic
def test_iter_cim_walks_the_property_list_once_per_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """Later instances of a class are read by name: the costly per-property list is walked once.

    Measured on a Windows VM: walking Properties_ (a COM object per property, each wrapped with
    its type information) took 7.7 s for 279 Win32_Service instances; by name, 0.27 s.
    """
    objs = [_CountingObj("Win32_Thing", {"Name": f"n{i}", "Count": -1}, {"Count": 19}) for i in range(4)]
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_FakeService(objs)))  # type: ignore[arg-type]
    items = list(iter_cim("Win32_Thing"))
    assert [i.properties for i in items] == [{"Name": f"n{i}", "Count": 0xFFFFFFFF} for i in range(4)]
    assert [o.walks for o in objs] == [1, 0, 0, 0]


@pytest.mark.os_agnostic
def test_iter_cim_learns_each_class_of_a_mixed_query_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """A query over a base class returns several classes; each keeps its own properties and order."""
    objs = [
        _CountingObj("Win32_DiskDrive", {"Model": "m", "Size": "10"}),
        _CountingObj("Win32_Keyboard", {"Layout": "de", "Model": "k", "Keys": 102}),
        _CountingObj("Win32_DiskDrive", {"Model": "m2", "Size": "20"}),
    ]
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_FakeService(objs)))  # type: ignore[arg-type]
    items = list(iter_cim("CIM_LogicalDevice"))
    assert [list(i.properties.items()) for i in items] == [
        [("Model", "m"), ("Size", "10")],
        [("Layout", "de"), ("Model", "k"), ("Keys", 102)],
        [("Model", "m2"), ("Size", "20")],
    ]
    assert [o.walks for o in objs] == [1, 1, 0]


@pytest.mark.os_agnostic
def test_a_property_named_like_a_wbem_member_is_read_from_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name ending in "_" may be an SWbemObject member (Path_), so such a class is never read by name."""
    objs = [_CountingObj("Odd_Class", {"Path_": "mine", "Name": f"n{i}"}) for i in range(2)]
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_FakeService(objs)))  # type: ignore[arg-type]
    items = list(iter_cim("Odd_Class"))
    assert [i.properties for i in items] == [{"Path_": "mine", "Name": "n0"}, {"Path_": "mine", "Name": "n1"}]
    assert [o.walks for o in objs] == [2, 1]


@pytest.mark.os_agnostic
def test_an_instance_whose_name_lookup_fails_is_read_from_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """A COM refusal on the by-name path costs speed, never the value: that instance takes the list."""
    objs = [
        _CountingObj("Win32_Thing", {"Name": "a"}),
        _CountingObj("Win32_Thing", {"Name": "b"}, refuse=frozenset({"Name"})),
    ]
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_FakeService(objs)))  # type: ignore[arg-type]
    assert [i.properties for i in iter_cim("Win32_Thing")] == [{"Name": "a"}, {"Name": "b"}]
    assert [o.walks for o in objs] == [1, 1]


@pytest.mark.os_agnostic
def test_a_non_com_failure_on_the_by_name_path_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a COM refusal falls back; a defect in the read itself surfaces with its own type."""

    class _Broken(_FakeRawDispatch):
        def Invoke(self, dispid: int, lcid: int, flags: int, want_result: bool, *args: Any) -> Any:  # noqa: N802
            raise RuntimeError("marshaling defect")

    obj = _CountingObj("Win32_Thing", {"Name": "a"}, dispatch_type=_Broken)
    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_FakeService([obj])))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="marshaling defect"):
        list(iter_cim("Win32_Thing"))


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="WMI is Windows-only")
@pytest.mark.parametrize(
    "wql", ["SELECT * FROM Win32_Service", "SELECT * FROM CIM_LogicalDevice", "SELECT * FROM Win32_Processor"]
)
def test_reading_by_name_matches_the_property_list_on_real_wmi(wql: str) -> None:
    """On the same fetched object, the by-name read gives the list's values in the list's order.

    CIM_LogicalDevice returns several classes, so it also checks one schema per class.
    """
    service = _load_wbem_service("root/cimv2")
    reader = _PropertyReader()
    compared = 0
    for obj in service.ExecQuery(wql, "WQL", 0x30):
        expected = _properties(obj)
        assert list(reader.read(obj).items()) == list(expected.items())
        compared += 1
    assert compared > 0
