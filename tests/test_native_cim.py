"""CIM/WMI native adapter over win32com WBEM.

A fake WBEM service (injected at the adapter's load seam) exercises property
marshaling, WQL construction, class-name validation, and the streaming/laziness
contract on every OS; a real structural test queries Win32_OperatingSystem on
Windows.  Exact live behaviour is pinned against Get-CimInstance in
``test_cim_pwsh_oracle``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from pwshpy.adapters.native import cim as cim_mod
from pwshpy.adapters.native.cim import iter_cim
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import CimInstance


class _FakeProp:
    def __init__(self, name: str, value: Any) -> None:
        self.Name = name
        self.Value = value


class _FakeObj:
    def __init__(self, props: dict[str, Any]) -> None:
        self.Properties_ = [_FakeProp(name, value) for name, value in props.items()]


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


class _FakeComError(Exception):
    """Mimics pywintypes.com_error; the adapter wraps errors whose type name is 'com_error'."""


_FakeComError.__name__ = "com_error"


@pytest.mark.os_agnostic
def test_iter_cim_wraps_query_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A COM/query failure surfaces as NativeCallError."""

    class _Boom:
        def ExecQuery(self, *args: Any) -> Iterator[Any]:  # noqa: N802 - WBEM API name
            raise _FakeComError("bad query")

    monkeypatch.setattr(cim_mod, "_load_wbem_service", _loader(_Boom()))
    with pytest.raises(NativeCallError):
        list(iter_cim("Win32_Thing"))


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
