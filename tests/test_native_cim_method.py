"""Read-only CIM/WMI method calls (``invoke_cim_method``) over win32com WBEM.

A fake WBEM service, injected at the adapter's load seam (the COM edge), exercises the allowlist,
the schema-typed in-parameters, the unsigned marshaling of out-parameters, static methods, the
error paths and the laziness contract on every OS. The real calls (a process's owner, a static
``StdRegProv`` read with a high-bit ``uint32`` in-parameter) run on Windows.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from com_fakes import com_error_for, error

from pwshpy import ps
from pwshpy.adapters.native import cim as cim_mod
from pwshpy.adapters.native.cim import invoke_cim_method
from pwshpy.domain.cim_methods import READ_ONLY_CIM_METHODS, is_read_only_cim_method, normalize_namespace
from pwshpy.domain.errors import CimMethodNotAllowedError, NativeAccessDeniedError, NativeCallError
from pwshpy.domain.records import CimMethodResult

# WbemCimtypeEnum
_SINT32, _STRING, _BOOLEAN, _UINT8, _UINT16, _UINT32, _SINT64, _UINT64, _DATETIME = 3, 8, 11, 17, 18, 19, 20, 21, 101

_BITLOCKER = "root/cimv2/Security/MicrosoftVolumeEncryption"
_TPM = "root/cimv2/Security/MicrosoftTpm"
#: Stands in for a key protector ID: no message may ever repeat it.
_SECRET = "{SECRET-PROTECTOR-ID-0000}"
_WBEM_E_NOT_FOUND = 0x80041002


class _Prop:
    def __init__(self, name: str, value: Any = None, cim_type: int = _STRING) -> None:
        self.Name = name
        self.Value = value
        self.CIMType = cim_type


class _PropSet:
    """Iterable like SWbemPropertySet, with ``Item(name)`` (case-insensitive, as WMI is)."""

    def __init__(self, props: list[_Prop]) -> None:
        self._props = props

    def __iter__(self) -> Iterator[_Prop]:
        return iter(self._props)

    def Item(self, name: str) -> _Prop:  # noqa: N802 - WBEM API name
        return next(p for p in self._props if p.Name.lower() == name.lower())


class _Params:
    """An in- or out-parameter object."""

    def __init__(self, props: list[_Prop]) -> None:
        self.Properties_ = _PropSet(props)

    def values(self) -> dict[str, Any]:
        return {p.Name: p.Value for p in self.Properties_}


class _InDefinition:
    """A method's ``InParameters`` class object: each ``SpawnInstance_`` is a fresh, empty instance."""

    def __init__(self, declared: dict[str, int]) -> None:
        self._declared = declared
        self.spawned: list[_Params] = []

    def SpawnInstance_(self) -> _Params:  # noqa: N802 - WBEM API name
        params = _Params([_Prop(name, None, cim_type) for name, cim_type in self._declared.items()])
        self.spawned.append(params)
        return params


class _Qualifier:
    def __init__(self, name: str, value: Any) -> None:
        self.Name = name
        self.Value = value


class _Method:
    def __init__(self, in_params: dict[str, int] | None, *, static: bool = False) -> None:
        self.InParameters = None if in_params is None else _InDefinition(in_params)
        self.Qualifiers_ = [_Qualifier("Implemented", True)] + ([_Qualifier("Static", True)] if static else [])


class _MethodSet:
    def __init__(self, methods: dict[str, _Method]) -> None:
        self._methods = methods

    def Item(self, name: str) -> _Method:  # noqa: N802 - WBEM API name
        for key, method in self._methods.items():
            if key.lower() == name.lower():
                return method
        raise com_error_for(_WBEM_E_NOT_FOUND, "Not found ")


Answer = Callable[[str, "_Params | None"], "_Params | None"]


def _out(values: dict[str, Any], types: dict[str, int]) -> _Params:
    return _Params([_Prop(name, value, types.get(name, _UINT32)) for name, value in values.items()])


def _answer(values: dict[str, Any], types: dict[str, int] | None = None) -> Answer:
    def answer(_method: str, _params: _Params | None) -> _Params:
        return _out(values, types or {})

    return answer


class _Target:
    """An instance (or, for a static method, the class) that records each ExecMethod_ call."""

    def __init__(self, rel_path: str, answer: Answer) -> None:
        self.Path_ = type("_Path", (), {"RelPath": rel_path})()
        self._answer = answer
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def ExecMethod_(self, method: str, params: _Params | None = None) -> _Params | None:  # noqa: N802 - WBEM API name
        self.calls.append((method, None if params is None else params.values()))
        return self._answer(method, params)


class _Class(_Target):
    def __init__(self, name: str, methods: dict[str, _Method], answer: Answer) -> None:
        super().__init__(name, answer)
        self.Methods_ = _MethodSet(methods)


class _Service:
    def __init__(self, klass: _Class, instances: list[_Target]) -> None:
        self.klass = klass
        self.instances = instances
        self.pulled = 0
        self.last_wql: str | None = None

    def Get(self, name: str) -> _Class:  # noqa: N802 - WBEM API name
        return self.klass

    def ExecQuery(self, wql: str, lang: str, flags: int) -> Iterator[_Target]:  # noqa: N802 - WBEM API name
        self.last_wql = wql

        def lazy() -> Iterator[_Target]:
            for instance in self.instances:
                self.pulled += 1
                yield instance

        return lazy()


def _install(monkeypatch: pytest.MonkeyPatch, service: Any) -> list[str]:
    """Inject ``service`` at the WBEM seam; returns the namespaces it was asked to connect to."""
    namespaces: list[str] = []

    def load(namespace: str) -> Any:
        namespaces.append(namespace)
        return service

    monkeypatch.setattr(cim_mod, "_load_wbem_service", load)
    return namespaces


def _volumes(*answers: Answer, in_params: dict[str, int] | None = None, method: str = "GetLockStatus") -> _Service:
    instances = [
        _Target(f'Win32_EncryptableVolume.DeviceID="vol{index}"', answer) for index, answer in enumerate(answers)
    ]
    klass = _Class("Win32_EncryptableVolume", {method: _Method(in_params)}, _answer({}))
    return _Service(klass, instances)


# --- the allowlist (pure) ---------------------------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("namespace", "class_name", "method_name"),
    [
        (_BITLOCKER, "Win32_EncryptableVolume", "GetConversionStatus"),
        (_BITLOCKER, "Win32_EncryptableVolume", "GetLockStatus"),
        (_BITLOCKER, "Win32_EncryptableVolume", "GetKeyProtectors"),
        (_BITLOCKER, "Win32_EncryptableVolume", "GetKeyProtectorType"),
        (_TPM, "Win32_Tpm", "IsReady"),
        (_TPM, "Win32_Tpm", "IsOwned"),
        (_TPM, "Win32_Tpm", "IsActivated"),
        (_TPM, "Win32_Tpm", "IsEnabled"),
        (_TPM, "Win32_Tpm", "GetDictionaryAttackParameters"),
        ("ROOT\\CIMV2\\SECURITY\\MICROSOFTTPM\\", "WIN32_TPM", "isready"),
        ("root/cimv2", "Win32_Process", "GetOwner"),
    ],
)
def test_the_allowlist_holds_the_read_only_methods_in_any_spelling(
    namespace: str, class_name: str, method_name: str
) -> None:
    assert is_read_only_cim_method(namespace, class_name, method_name)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("namespace", "class_name", "method_name"),
    [
        (_BITLOCKER, "Win32_EncryptableVolume", "Encrypt"),
        (_BITLOCKER, "Win32_EncryptableVolume", "Lock"),
        (_BITLOCKER, "Win32_EncryptableVolume", "DeleteKeyProtectors"),
        (_BITLOCKER, "Win32_EncryptableVolume", "GetKeyProtectorNumericalPassword"),  # a secret
        (_TPM, "Win32_Tpm", "ClearOwnership"),
        (_TPM, "Win32_Tpm", "GetOwnerAuth"),  # a secret
        ("root/cimv2", "Win32_Process", "Terminate"),
        ("root/cimv2", "Win32_Tpm", "IsReady"),  # the right method in the wrong namespace
        (_TPM, "Win32_Process", "GetOwner"),  # the right method on another namespace's class
        ("root/default", "StdRegProv", "SetStringValue"),
    ],
)
def test_the_allowlist_refuses_writers_secrets_and_misplaced_names(
    namespace: str, class_name: str, method_name: str
) -> None:
    assert not is_read_only_cim_method(namespace, class_name, method_name)


@pytest.mark.os_agnostic
def test_every_allowlist_key_is_in_its_normalized_form() -> None:
    """A key in any other spelling could never match, so the method it names would be unreachable."""
    for namespace, classes in READ_ONLY_CIM_METHODS.items():
        assert namespace == normalize_namespace(namespace)
        for class_name, methods in classes.items():
            assert class_name == class_name.lower()
            assert all(method == method.lower() for method in methods)


@pytest.mark.os_agnostic
def test_the_allowlist_cannot_be_mutated() -> None:
    with pytest.raises(TypeError):
        classes: Any = READ_ONLY_CIM_METHODS["root/cimv2"]
        classes["win32_process"] = frozenset({"terminate"})


# --- refusals at call time --------------------------------------------------------------------


def _no_wmi(monkeypatch: pytest.MonkeyPatch) -> None:
    def load(_namespace: str) -> Any:
        raise AssertionError("WMI was contacted")

    monkeypatch.setattr(cim_mod, "_load_wbem_service", load)


@pytest.mark.os_agnostic
def test_a_method_off_the_allowlist_is_refused_when_called_before_wmi(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_wmi(monkeypatch)
    with pytest.raises(CimMethodNotAllowedError) as info:
        invoke_cim_method("Win32_EncryptableVolume", "Encrypt", namespace=_BITLOCKER)
    assert "Win32_EncryptableVolume.Encrypt" in str(info.value)
    assert "read-only" in str(info.value)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("class_name", "method_name", "kind"),
    [("Win32_Tpm; x", "IsReady", "class"), ("Win32_Tpm", "IsReady\n", "method"), ("", "IsReady", "class")],
)
def test_a_name_that_is_not_an_identifier_is_refused_when_called(
    monkeypatch: pytest.MonkeyPatch, class_name: str, method_name: str, kind: str
) -> None:
    _no_wmi(monkeypatch)
    with pytest.raises(NativeCallError, match=f"invalid WMI {kind} name"):
        invoke_cim_method(class_name, method_name, namespace=_TPM)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("value", [3.5, None, b"raw", [_SECRET]])
def test_an_argument_value_of_another_type_is_refused_when_called(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    _no_wmi(monkeypatch)
    arguments: Any = {"VolumeKeyProtectorID": value}  # the wrong type is the point
    with pytest.raises(NativeCallError) as info:
        invoke_cim_method("Win32_EncryptableVolume", "GetKeyProtectorType", namespace=_BITLOCKER, arguments=arguments)
    assert "'VolumeKeyProtectorID' must be a str, int or bool" in str(info.value)
    assert _SECRET not in str(info.value)


# --- the call ---------------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_each_instance_yields_its_answer_with_the_return_value_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _volumes(_answer({"LockStatus": 0, "ReturnValue": 0}), _answer({"LockStatus": 1, "ReturnValue": 0}))
    namespaces = _install(monkeypatch, service)

    results = list(invoke_cim_method("Win32_EncryptableVolume", "GetLockStatus", namespace=_BITLOCKER))

    assert namespaces == [_BITLOCKER]
    assert service.last_wql == "SELECT * FROM Win32_EncryptableVolume"
    assert [r.out_parameters for r in results] == [{"LockStatus": 0}, {"LockStatus": 1}]
    assert [r.return_value for r in results] == [0, 0]
    assert all(isinstance(r, CimMethodResult) for r in results)
    assert results[1].instance_path == 'Win32_EncryptableVolume.DeviceID="vol1"'
    assert (results[0].class_name, results[0].method_name) == ("Win32_EncryptableVolume", "GetLockStatus")


@pytest.mark.os_agnostic
def test_the_where_filter_selects_the_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _volumes(_answer({"ReturnValue": 0}))
    _install(monkeypatch, service)
    list(invoke_cim_method("Win32_EncryptableVolume", "GetLockStatus", namespace=_BITLOCKER, where="DriveLetter='C:'"))
    assert service.last_wql == "SELECT * FROM Win32_EncryptableVolume WHERE DriveLetter='C:'"


@pytest.mark.os_agnostic
def test_the_call_is_lazy_one_instance_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = [_answer({"ReturnValue": 0}) for _ in range(3)]
    service = _volumes(*answers)
    _install(monkeypatch, service)
    stream = invoke_cim_method("Win32_EncryptableVolume", "GetLockStatus", namespace=_BITLOCKER)
    assert service.pulled == 0  # nothing ran yet
    next(stream)
    assert service.pulled == 1


@pytest.mark.os_agnostic
def test_a_non_zero_return_value_is_data_and_reported_unsigned(monkeypatch: pytest.MonkeyPatch) -> None:
    """FVE_E_LOCKED_VOLUME (0x80310000) arrives signed; the record carries it unsigned, and the stream goes on."""
    locked = _answer({"ConversionStatus": None, "ReturnValue": -2144272384})
    service = _volumes(locked, _answer({"ConversionStatus": 1, "ReturnValue": 0}), method="GetConversionStatus")
    _install(monkeypatch, service)
    results = list(invoke_cim_method("Win32_EncryptableVolume", "GetConversionStatus", namespace=_BITLOCKER))
    assert [r.return_value for r in results] == [0x80310000, 0]
    assert results[1].out_parameters == {"ConversionStatus": 1}


@pytest.mark.os_agnostic
def test_out_parameters_are_marshaled_unsigned_by_their_cim_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured on Windows: WipingStatus 0xFFFFFFFF arrived as -1, and a string array as a tuple."""
    values = {"WipingStatus": -1, "Small": -2, "Ids": ("a", "b"), "Flag": True, "Signed": -3, "ReturnValue": 0}
    types = {"WipingStatus": _UINT32, "Small": _UINT16, "Ids": _STRING, "Flag": _BOOLEAN, "Signed": _SINT32}
    service = _volumes(_answer(values, types), method="GetConversionStatus")
    _install(monkeypatch, service)
    result = next(invoke_cim_method("Win32_EncryptableVolume", "GetConversionStatus", namespace=_BITLOCKER))
    assert result.out_parameters == {
        "WipingStatus": 0xFFFFFFFF,
        "Small": 0xFFFE,
        "Ids": ["a", "b"],
        "Flag": True,
        "Signed": -3,
    }


@pytest.mark.os_agnostic
def test_a_method_without_a_return_value_reports_none(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _volumes(_answer({"LockStatus": 2}), lambda _m, _p: None)
    _install(monkeypatch, service)
    results = list(invoke_cim_method("Win32_EncryptableVolume", "GetLockStatus", namespace=_BITLOCKER))
    assert [(r.return_value, r.out_parameters) for r in results] == [(None, {"LockStatus": 2}), (None, {})]


@pytest.mark.os_agnostic
def test_a_return_value_that_is_not_an_integer_stays_in_the_out_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _volumes(_answer({"ReturnValue": True}, {"ReturnValue": _BOOLEAN}))
    _install(monkeypatch, service)
    result = next(invoke_cim_method("Win32_EncryptableVolume", "GetLockStatus", namespace=_BITLOCKER))
    assert (result.return_value, result.out_parameters) == (None, {"ReturnValue": True})


@pytest.mark.os_agnostic
def test_a_method_without_in_parameters_is_called_without_any(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _volumes(_answer({"ReturnValue": 0}))
    _install(monkeypatch, service)
    list(invoke_cim_method("Win32_EncryptableVolume", "GetLockStatus", namespace=_BITLOCKER))
    assert service.instances[0].calls == [("GetLockStatus", None)]


# --- in-parameters ------------------------------------------------------------------------------


def _call_with(
    monkeypatch: pytest.MonkeyPatch, declared: dict[str, int], arguments: dict[str, Any]
) -> dict[str, Any] | None:
    """Run GetKeyProtectorType with ``arguments`` against a method declaring ``declared``; the values sent."""
    service = _volumes(_answer({"ReturnValue": 0}), in_params=declared, method="GetKeyProtectorType")
    _install(monkeypatch, service)
    list(invoke_cim_method("Win32_EncryptableVolume", "GetKeyProtectorType", namespace=_BITLOCKER, arguments=arguments))
    return service.instances[0].calls[0][1]


@pytest.mark.os_agnostic
def test_a_string_argument_is_set_under_its_declared_name(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _call_with(monkeypatch, {"VolumeKeyProtectorID": _STRING}, {"volumekeyprotectorid": _SECRET})
    assert sent == {"VolumeKeyProtectorID": _SECRET}


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("cim_type", "value", "wire"),
    [
        (_UINT32, 3, 3),
        (_UINT32, "3", 3),
        (_UINT32, 0x80000002, -2147483646),  # the signed form WBEM scripting itself uses for a uint32
        (_UINT32, 0xFFFFFFFF, -1),
        (_UINT16, 0xFFFF, -1),
        (_UINT8, 0x80, -128),
        (_SINT32, "-7", -7),
        (_SINT64, 2**40, str(2**40)),  # 64-bit integers travel as decimal strings
        (_UINT64, "18446744073709551615", "18446744073709551615"),
        (_BOOLEAN, True, True),
        (_BOOLEAN, "FALSE", False),
    ],
)
def test_a_value_is_sent_in_the_form_its_cim_type_takes(
    monkeypatch: pytest.MonkeyPatch, cim_type: int, value: Any, wire: Any
) -> None:
    assert _call_with(monkeypatch, {"P": cim_type}, {"P": value}) == {"P": wire}


@pytest.mark.os_agnostic
def test_only_the_given_arguments_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """An optional in-parameter left out stays unset (None), as Invoke-CimMethod leaves it."""
    sent = _call_with(monkeypatch, {"KeyProtectorType": _UINT32, "Other": _STRING}, {"KeyProtectorType": 1})
    assert sent == {"KeyProtectorType": 1, "Other": None}


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("declared", "arguments", "message"),
    [
        (
            {"P": _UINT32},
            {"P": -1},
            r"'P' is an integer parameter and takes an int \(or decimal str\) from 0 to 4294967295",
        ),
        ({"P": _UINT8}, {"P": 256}, "from 0 to 255"),
        ({"P": _UINT32}, {"P": _SECRET}, "'P' is an integer parameter"),
        ({"P": _UINT32}, {"P": True}, "'P' is an integer parameter"),
        ({"P": _BOOLEAN}, {"P": _SECRET}, "'P' is a boolean parameter"),
        ({"P": _BOOLEAN}, {"P": 1}, "'P' is a boolean parameter"),
        ({"P": _STRING}, {"P": 7}, "'P' is a string parameter and takes a str"),
        ({"P": _DATETIME}, {"P": _SECRET}, "'P' has CIM type 101, which invoke_cim_method does not set"),
        (
            {"VolumeKeyProtectorID": _STRING},
            {"Nope": _SECRET},
            "has no in-parameter 'Nope'; it takes: VolumeKeyProtectorID",
        ),
    ],
)
def test_an_argument_that_does_not_fit_the_schema_is_refused_without_its_value(
    monkeypatch: pytest.MonkeyPatch, declared: dict[str, int], arguments: dict[str, Any], message: str
) -> None:
    with pytest.raises(NativeCallError, match=message) as info:
        _call_with(monkeypatch, declared, arguments)
    assert "Win32_EncryptableVolume.GetKeyProtectorType" in str(info.value)
    assert _SECRET not in str(info.value)


@pytest.mark.os_agnostic
def test_arguments_to_a_method_that_takes_none_are_refused_without_their_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _volumes(_answer({"ReturnValue": 0}))
    _install(monkeypatch, service)
    with pytest.raises(NativeCallError, match="GetLockStatus takes no arguments; got Extra") as info:
        list(
            invoke_cim_method(
                "Win32_EncryptableVolume", "GetLockStatus", namespace=_BITLOCKER, arguments={"Extra": _SECRET}
            )
        )
    assert _SECRET not in str(info.value)
    assert service.pulled == 0  # refused before any instance was asked


@pytest.mark.os_agnostic
def test_the_callers_mapping_is_copied_when_called(monkeypatch: pytest.MonkeyPatch) -> None:
    """A change to the caller's dict after the call does not reach the lazily run method."""
    service = _volumes(_answer({"ReturnValue": 0}), in_params={"P": _STRING}, method="GetKeyProtectorType")
    _install(monkeypatch, service)
    arguments: dict[str, Any] = {"P": "first"}
    stream = invoke_cim_method(
        "Win32_EncryptableVolume", "GetKeyProtectorType", namespace=_BITLOCKER, arguments=arguments
    )
    arguments["P"] = "second"
    list(stream)
    assert service.instances[0].calls == [("GetKeyProtectorType", {"P": "first"})]


# --- static methods -----------------------------------------------------------------------------


def _registry(answer: Answer) -> _Service:
    declared = {"hDefKey": _UINT32, "sSubKeyName": _STRING, "sValueName": _STRING}
    klass = _Class("StdRegProv", {"GetStringValue": _Method(declared, static=True)}, answer)
    return _Service(klass, [])


@pytest.mark.os_agnostic
def test_a_static_method_runs_once_on_the_class(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _registry(_answer({"sValue": "26200", "ReturnValue": 0}, {"sValue": _STRING}))
    _install(monkeypatch, service)
    results = list(
        invoke_cim_method(
            "StdRegProv",
            "GetStringValue",
            namespace="root/default",
            arguments={"hDefKey": 0x80000002, "sSubKeyName": "SOFTWARE\\X", "sValueName": "CurrentBuild"},
        )
    )
    assert [(r.instance_path, r.return_value, r.out_parameters) for r in results] == [
        ("StdRegProv", 0, {"sValue": "26200"})
    ]
    assert service.last_wql is None  # no instance query
    assert service.klass.calls == [
        ("GetStringValue", {"hDefKey": -2147483646, "sSubKeyName": "SOFTWARE\\X", "sValueName": "CurrentBuild"})
    ]


@pytest.mark.os_agnostic
def test_a_static_method_takes_no_where_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _registry(_answer({"ReturnValue": 0}))
    _install(monkeypatch, service)
    with pytest.raises(NativeCallError, match="static method: it runs on the class and takes no where filter"):
        list(invoke_cim_method("StdRegProv", "GetStringValue", namespace="root/default", where="x = 1"))
    assert service.klass.calls == []


# --- failures -----------------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_a_method_the_class_lacks_is_a_native_error_with_its_hresult(monkeypatch: pytest.MonkeyPatch) -> None:
    """An allowlisted method an older Windows does not define fails on reading the definition."""
    klass = _Class("Win32_Tpm", {}, _answer({}))
    _install(monkeypatch, _Service(klass, []))
    with pytest.raises(NativeCallError) as info:
        list(invoke_cim_method("Win32_Tpm", "IsReady", namespace=_TPM))
    assert str(info.value).startswith("cannot read the definition of Win32_Tpm.IsReady: ")
    assert "HRESULT 0x80041002" in str(info.value)


@pytest.mark.os_agnostic
def test_a_failed_call_names_the_method_and_instance_never_the_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = com_error_for(0x80041008, "Invalid parameter ")

    def refuse(_method: str, _params: _Params | None) -> _Params:
        raise failure

    service = _volumes(refuse, in_params={"VolumeKeyProtectorID": _STRING}, method="GetKeyProtectorType")
    _install(monkeypatch, service)
    with pytest.raises(NativeCallError) as info:
        list(
            invoke_cim_method(
                "Win32_EncryptableVolume",
                "GetKeyProtectorType",
                namespace=_BITLOCKER,
                arguments={"VolumeKeyProtectorID": _SECRET},
            )
        )
    message = str(info.value)
    assert message.startswith("Win32_EncryptableVolume.GetKeyProtectorType failed on Win32_EncryptableVolume.DeviceID=")
    assert "HRESULT 0x80041008" in message
    assert _SECRET not in message
    assert info.value.__cause__ is failure


@pytest.mark.os_agnostic
def test_a_refused_call_is_an_access_denied_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(_method: str, _params: _Params | None) -> _Params:
        raise error(5, "ExecMethod", "Access is denied.")

    _install(monkeypatch, _volumes(deny))
    with pytest.raises(NativeAccessDeniedError):
        list(invoke_cim_method("Win32_EncryptableVolume", "GetLockStatus", namespace=_BITLOCKER))


@pytest.mark.os_agnostic
def test_a_failed_instance_query_carries_its_hresult(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _volumes(_answer({"ReturnValue": 0}))

    def broken(*_args: Any) -> Iterator[_Target]:
        raise com_error_for(0x80041017, "Invalid query ")

    monkeypatch.setattr(service, "ExecQuery", broken)
    _install(monkeypatch, service)
    with pytest.raises(NativeCallError, match=r"HRESULT 0x80041017"):
        list(invoke_cim_method("Win32_EncryptableVolume", "GetLockStatus", namespace=_BITLOCKER, where="bad"))


@pytest.mark.os_agnostic
def test_a_foreign_error_propagates_unwrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-COM error (a marshaling defect) is not pwshpy's to relabel."""

    def defect(_method: str, _params: _Params | None) -> _Params:
        raise RuntimeError("marshaling defect")

    _install(monkeypatch, _volumes(defect))
    with pytest.raises(RuntimeError, match="marshaling defect"):
        list(invoke_cim_method("Win32_EncryptableVolume", "GetLockStatus", namespace=_BITLOCKER))


# --- real WMI -----------------------------------------------------------------------------------


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="WMI is Windows-only")
def test_the_owner_of_this_process_is_read_through_a_method() -> None:
    """Win32_Process.GetOwner on our own process: one answer, ReturnValue 0, our account name."""
    results = list(invoke_cim_method("Win32_Process", "GetOwner", where=f"ProcessId = {os.getpid()}"))
    assert len(results) == 1
    result = results[0]
    assert result.return_value == 0
    assert result.instance_path == f'Win32_Process.Handle="{os.getpid()}"'
    assert str(result.out_parameters["User"]).lower() == os.environ["USERNAME"].lower()
    sid = next(invoke_cim_method("Win32_Process", "GetOwnerSid", where=f"ProcessId = {os.getpid()}"))
    assert str(sid.out_parameters["Sid"]).startswith("S-1-5-")


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="WMI is Windows-only")
def test_a_static_registry_read_matches_the_registry_reader() -> None:
    """StdRegProv.GetStringValue with HKLM (uint32 0x80000002, high bit set) agrees with get_item_property."""
    subkey = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"
    build = ps.get_item_property(f"HKLM\\{subkey}").where(lambda value: value.name == "CurrentBuild").first()
    assert build is not None
    expected = build.data
    result = next(
        invoke_cim_method(
            "StdRegProv",
            "GetStringValue",
            namespace="root/default",
            arguments={"hDefKey": 0x80000002, "sSubKeyName": subkey, "sValueName": "CurrentBuild"},
        )
    )
    assert (result.instance_path, result.return_value) == ("StdRegProv", 0)
    assert result.out_parameters["sValue"] == expected
