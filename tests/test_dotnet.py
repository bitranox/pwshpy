""".NET hosted PowerShell: real in-process execution.

``local_only`` - requires the ``[full]`` extra (pythonnet) plus the .NET 10 runtime
and the PowerShell 7.6 SDK, which only the dev box has; ``make test`` skips it.

The guard is ``is_runtime_available()``, NOT ``is_available()``: the latter reports only
whether the extra is importable, so it says True on a box that has pythonnet but no .NET
runtime - and every test here then fails with FeatureUnavailableError instead of skipping.
That is not hypothetical: the project venv installs ``[full]`` while this box has no
runtime, so these 16 tests fail the moment they are run from it. Collection on a base
install still costs only a spec lookup, since the check short-circuits when the extra
is absent.
"""

from __future__ import annotations

import pytest

from pwshpy import ps
from pwshpy.adapters.powershell import is_runtime_available
from pwshpy.domain.errors import PowerShellError
from pwshpy.domain.records import PSInvocationResult, PSObjectRecord

pytestmark = [
    pytest.mark.local_only,
    pytest.mark.skipif(
        not is_runtime_available(),
        reason=".NET needs the [full] extra AND the .NET 10 runtime AND the PowerShell 7.6 SDK",
    ),
]


def test_run_marshals_primitives() -> None:
    """Wrapped primitives come back as plain Python values, in order."""
    assert ps.run("40 + 2; 'hi'.ToUpper()") == [42, "HI"]


def test_run_hosts_powershell_7_6() -> None:
    """The in-process engine reports PowerShell 7.6.x."""
    assert ps.run("$PSVersionTable.PSVersion.Major; $PSVersionTable.PSVersion.Minor") == [7, 6]


def test_run_marshals_structured_object() -> None:
    """A structured object becomes a PSObjectRecord with a property bag."""
    items = ps.run("[pscustomobject]@{Name='x'; Count=3}")
    assert len(items) == 1
    record = items[0]
    assert isinstance(record, PSObjectRecord)
    assert record.properties["Name"] == "x"
    assert record.properties["Count"] == 3


def test_run_surfaces_errors_as_powershell_error() -> None:
    """A script writing to the error stream raises PowerShellError."""
    with pytest.raises(PowerShellError):
        ps.run("Write-Error 'boom'")


def test_run_times_out_and_cancels() -> None:
    """A script that outruns its timeout is stopped and raises PowerShellError."""
    with pytest.raises(PowerShellError):
        ps.run("Start-Sleep -Seconds 10", timeout=0.5)


def test_run_is_in_process_never_spawns_pwsh() -> None:
    """The hosting process is our Python interpreter, never a spawned pwsh.exe."""
    out = ps.run("[System.Diagnostics.Process]::GetCurrentProcess().ProcessName")
    assert out[0].lower() != "pwsh"


def test_shared_runspace_persists_state_across_calls() -> None:
    """The runspace is shared/reused, so a global set in one call is visible in the next."""
    ps.run("$global:pwshpy_probe = 123")
    assert ps.run("$global:pwshpy_probe") == [123]


# --- ps.cmdlet: safe parameter binding + streams -----------------------------


def test_cmdlet_binds_parameters_and_returns_output() -> None:
    """ps.cmdlet runs a real cmdlet with bound params and marshals its output."""
    result = ps.cmdlet("Write-Output", InputObject=42)
    assert isinstance(result, PSInvocationResult)
    assert result.output == [42]
    assert result.had_errors is False


def test_cmdlet_parameter_values_are_data_not_code() -> None:
    """A parameter value full of PowerShell metacharacters is passed as literal data.

    If it were string-interpolated into a script, the embedded ``Write-Error`` would
    fire; bound as a parameter it is just the string that comes back out.
    """
    payload = "'; Write-Error 'INJECTED'; $x = @("
    result = ps.cmdlet("Write-Output", InputObject=payload)
    assert result.output == [payload]
    assert result.had_errors is False
    assert result.errors == []


def test_cmdlet_captures_warning_stream() -> None:
    """A cmdlet writing to the warning stream surfaces it in .warnings, not as output/error."""
    result = ps.cmdlet("Write-Warning", Message="heads up")
    assert result.warnings == ["heads up"]
    assert result.output == []
    assert result.had_errors is False


def test_cmdlet_does_not_raise_on_error_stream() -> None:
    """Unlike run, cmdlet returns errors in the result instead of raising."""
    result = ps.cmdlet("Write-Error", Message="soft fail")
    assert result.had_errors is True
    assert any("soft fail" in e for e in result.errors)


def test_cmdlet_list_parameter_becomes_an_array() -> None:
    """A Python list parameter is marshaled to a PowerShell array the cmdlet enumerates."""
    result = ps.cmdlet("Write-Output", InputObject=[1, 2, 3])
    assert result.output == [1, 2, 3]


def test_run_marshals_nested_array_property() -> None:
    """A structured object's array-valued property marshals to a Python list, not str()."""
    items = ps.run("[pscustomobject]@{Items=@(1,2,3); Name='k'}")
    assert len(items) == 1
    record = items[0]
    assert isinstance(record, PSObjectRecord)
    assert record.properties["Items"] == [1, 2, 3]
    assert record.properties["Name"] == "k"


# --- ps.get_command: parameter introspection ---------------------------------


def test_get_command_introspects_parameters() -> None:
    """ps.get_command projects a real cmdlet's parameter metadata into a typed CommandInfo."""
    info = ps.get_command("Get-Item")
    assert info.name == "Get-Item"
    assert info.command_type == "Cmdlet"
    names = {parameter.name for parameter in info.parameters}
    assert "Path" in names
    assert "LiteralPath" in names


def test_get_command_unknown_raises() -> None:
    """An unknown command surfaces Get-Command's error as PowerShellError."""
    with pytest.raises(PowerShellError):
        ps.get_command("Definitely-Not-A-Real-Cmdlet-XYZ")
