""".NET ``invoke`` (the ``ps.cmdlet`` engine): os_agnostic unit tests over a fake shell.

These never load .NET.  A fake ``PowerShell`` shell records how the engine binds a
command and its parameters, so the SAFE-BINDING contract (values go through
``AddParameter``/``AddArgument``, never string-interpolated into a script) and the
stream marshaling are verified on any OS.  Real in-process behavior is covered by the
``local_only`` tests in ``test_tier_b.py`` / ``test_tier_b_pool.py``.
"""
# The fake mirrors the .NET PowerShell API, whose members are PascalCase; silence the
# Python naming rules for this test double only.
# ruff: noqa: N802

from __future__ import annotations

from typing import Any

import pytest

from pwshpy.adapters.powershell import hosted
from pwshpy.domain.errors import PowerShellError
from pwshpy.domain.records import CommandInfo, PSInvocationResult, PSObjectRecord


def _identity(value: Any) -> Any:
    """Marshaling stand-in for the fake path: return the value unchanged."""
    return value


class _FakeStreams:
    def __init__(self, **kwargs: list[Any]) -> None:
        self.Error = kwargs.get("errors", [])
        self.Warning = kwargs.get("warnings", [])
        self.Verbose = kwargs.get("verbose", [])
        self.Debug = kwargs.get("debug", [])
        self.Information = kwargs.get("information", [])


class _FakeShell:
    """Records the pipeline the engine builds; returns canned output + streams."""

    def __init__(self, *, results: list[Any], had_errors: bool, streams: dict[str, list[Any]]) -> None:
        self.commands: list[str] = []
        self.arguments: list[Any] = []
        self.parameters: list[tuple[str, Any]] = []
        self.scripts: list[str] = []
        self._results = results
        self.HadErrors = had_errors
        self.Streams = _FakeStreams(**streams)
        self.disposed = False
        self.Runspace: Any = None
        self.RunspacePool: Any = None

    def AddScript(self, script: str) -> _FakeShell:
        self.scripts.append(script)
        return self

    def AddCommand(self, command: str) -> _FakeShell:
        self.commands.append(command)
        return self

    def AddArgument(self, argument: Any) -> _FakeShell:
        self.arguments.append(argument)
        return self

    def AddParameter(self, name: str, value: Any) -> _FakeShell:
        self.parameters.append((name, value))
        return self

    def Invoke(self) -> list[Any]:
        return self._results

    def Stop(self) -> None: ...

    def Dispose(self) -> None:
        self.disposed = True


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    results: list[Any] | None = None,
    had_errors: bool = False,
    identity_marshal: bool = True,
    **streams: list[Any],
) -> _FakeShell:
    """Wire a single fake shell into the engine and return it for inspection."""
    shell = _FakeShell(results=results or [], had_errors=had_errors, streams=streams)

    class _Factory:
        @staticmethod
        def Create() -> _FakeShell:
            return shell

    monkeypatch.setattr(hosted, "is_available", lambda: True)
    monkeypatch.setattr(hosted, "_ensure_ready", lambda: None)
    monkeypatch.setattr(hosted, "_pool", None, raising=False)
    monkeypatch.setattr(hosted, "_runspace", object(), raising=False)
    monkeypatch.setattr(hosted, "_powershell_cls", _Factory, raising=False)
    if identity_marshal:
        monkeypatch.setattr(hosted, "_marshal_arg", _identity)
        monkeypatch.setattr(hosted, "_marshal", _identity)
    return shell


@pytest.mark.os_agnostic
def test_invoke_binds_parameters_never_interpolates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parameters and positional args are bound discretely - the script path is never touched."""
    shell = _install(monkeypatch, results=[1])
    injection = "C:/; Write-Error 'pwned'; #"
    hosted.invoke("Get-ChildItem", "positional", Path=injection, Recurse=True)

    assert shell.commands == ["Get-ChildItem"]
    assert shell.arguments == ["positional"]
    assert ("Path", injection) in shell.parameters  # bound as data, not concatenated
    assert ("Recurse", True) in shell.parameters
    assert shell.scripts == []  # AddScript was NEVER used -> no interpolation surface
    assert shell.disposed is True


@pytest.mark.os_agnostic
def test_invoke_collects_all_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """invoke returns a PSInvocationResult carrying output plus every side stream."""
    _install(
        monkeypatch,
        results=["out"],
        had_errors=True,
        errors=["boom"],
        warnings=["careful"],
        verbose=["v"],
        debug=["d"],
        information=["i"],
    )
    result = hosted.invoke("Do-Thing")

    assert isinstance(result, PSInvocationResult)
    assert result.output == ["out"]
    assert result.errors == ["boom"]
    assert result.warnings == ["careful"]
    assert result.verbose == ["v"]
    assert result.debug == ["d"]
    assert result.information == ["i"]
    assert result.had_errors is True


@pytest.mark.os_agnostic
def test_invoke_does_not_raise_on_error_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike run, invoke surfaces errors in the result rather than raising."""
    _install(monkeypatch, results=[], had_errors=True, errors=["nope"])
    result = hosted.invoke("Do-Thing")  # must not raise
    assert result.had_errors is True
    assert result.errors == ["nope"]


@pytest.mark.os_agnostic
def test_run_returns_output_and_raises_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """run returns just the output list, and raises PowerShellError on an error stream."""
    _install(monkeypatch, results=[10, 20])
    assert hosted.run("1..2") == [10, 20]

    _install(monkeypatch, results=[], had_errors=True, errors=["kaboom"])
    with pytest.raises(PowerShellError, match="kaboom"):
        hosted.run("Write-Error kaboom")


@pytest.mark.os_agnostic
def test_run_uses_the_script_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """run binds via AddScript (the arbitrary-script path), not AddCommand."""
    shell = _install(monkeypatch, results=[1])
    hosted.run("Get-Process | Select-Object -First 1")
    assert shell.scripts == ["Get-Process | Select-Object -First 1"]
    assert shell.commands == []


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 0), ("0", 0), ("1", 1), ("4", 4), ("-3", 0), ("notint", 0)],
)
def test_pool_size_parses_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
    """_pool_size reads the requested size as a non-negative int (garbage/negative -> 0).

    A value of 0 or 1 still means single-runspace mode - that ``> 1`` decision lives in
    ``_init``; ``_pool_size`` only parses the number.
    """
    if raw == "":
        monkeypatch.delenv("PWSHPY_RUNSPACE_POOL_SIZE", raising=False)
    else:
        monkeypatch.setenv("PWSHPY_RUNSPACE_POOL_SIZE", raw)
    pool_size = hosted._pool_size  # pyright: ignore[reportPrivateUsage] - white-box test of an engine helper
    assert pool_size() == expected


@pytest.mark.os_agnostic
def test_marshal_value_primitives_and_fallback() -> None:
    """Primitives pass through; an unmodeled object with no cached .NET types stringifies."""
    marshal_value = hosted._marshal_value  # pyright: ignore[reportPrivateUsage] - white-box test of an engine helper
    assert marshal_value(None) is None
    assert marshal_value("x") == "x"
    assert marshal_value(3) == 3
    assert marshal_value(True) is True

    class _Weird:
        def __str__(self) -> str:
            return "weird"

    assert marshal_value(_Weird()) == "weird"


@pytest.mark.os_agnostic
def test_to_command_info_builds_typed_record() -> None:
    """_to_command_info maps the marshaled Get-Command object into a typed CommandInfo."""
    param = PSObjectRecord(
        type_name="", properties={"Name": "Path", "Type": "String", "Mandatory": True, "Aliases": "PSPath,LP"}
    )
    obj = PSObjectRecord(
        type_name="",
        properties={"Name": "Get-Item", "CommandType": "Cmdlet", "Module": "M", "Parameters": [param]},
    )
    to_command_info = hosted._to_command_info  # pyright: ignore[reportPrivateUsage] - white-box test
    info = to_command_info(obj)
    assert isinstance(info, CommandInfo)
    assert (info.name, info.command_type, info.module) == ("Get-Item", "Cmdlet", "M")
    assert info.parameters[0].name == "Path"
    assert info.parameters[0].mandatory is True
    assert info.parameters[0].aliases == ["PSPath", "LP"]


@pytest.mark.os_agnostic
def test_get_command_dispatches_and_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_command runs the introspection script and returns the typed CommandInfo."""
    obj = PSObjectRecord(
        type_name="", properties={"Name": "Get-Foo", "CommandType": "Function", "Module": "", "Parameters": []}
    )

    def _fake_dispatch(_configure: Any, _timeout: Any) -> PSInvocationResult:
        return PSInvocationResult(output=[obj], had_errors=False)

    monkeypatch.setattr(hosted, "_dispatch", _fake_dispatch)
    info = hosted.get_command("Get-Foo")
    assert info.name == "Get-Foo"
    assert info.command_type == "Function"
    assert info.parameters == []


@pytest.mark.os_agnostic
def test_marshal_value_collection_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """When an IEnumerable .NET type is cached, a matching value marshals element-wise."""
    ps_types: dict[str, Any] = hosted._ps_types  # pyright: ignore[reportPrivateUsage] - white-box test
    marshal_value = hosted._marshal_value  # pyright: ignore[reportPrivateUsage] - white-box test of an engine helper
    monkeypatch.setitem(ps_types, "IEnumerable", tuple)
    monkeypatch.setitem(ps_types, "PSObject", type(None))
    monkeypatch.setitem(ps_types, "IDictionary", type(None))
    assert marshal_value((1, "a", 2)) == [1, "a", 2]
