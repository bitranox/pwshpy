""".NET adapter - hosts the PowerShell 7.6 SDK in-process via pythonnet on .NET 10.

Reached through the optional ``[full]`` extra (pythonnet).  The .NET assemblies are
the installed **PowerShell 7.6** ones (``System.Management.Automation`` + the
``Microsoft.PowerShell.*`` set, which target .NET 10); an ``AssemblyResolve`` hook
loads them and their dependencies from the PowerShell home directory.  Nothing
here touches .NET until :func:`run` is first called - importing the module is
free, so a base install stays pure-Python.

The engine runs **in-process** (a shared, reused ``Runspace`` behind a lock) - it
never spawns ``pwsh.exe``.  Each returned PSObject marshals into the same domain
object model: wrapped primitives become plain Python values, structured objects
become :class:`~pwshpy.domain.records.PSObjectRecord`.

Trust boundary: ``run`` executes arbitrary PowerShell with the caller's
privileges - never feed it unvalidated external input.

Contents:
    * :func:`is_available` - whether the ``[full]`` extra (pythonnet) is importable.
    * :func:`run` - execute a script in the hosted engine and return marshaled objects.
    * :func:`invoke` - run a single cmdlet with **safely bound** parameters, returning
      a :class:`~pwshpy.domain.records.PSInvocationResult` (output plus every side stream).

Concurrency: by default a single shared ``Runspace`` serves every call behind a lock,
so ``$global:`` state persists across calls.  Setting ``PWSHPY_RUNSPACE_POOL_SIZE`` to an
integer > 1 instead opens a ``RunspacePool`` of that size and runs calls WITHOUT the
serializing lock, trading shared-state persistence for concurrency.
"""

# This adapter is untyped .NET interop: pythonnet loads the PowerShell SDK at runtime, so the
# `clr` / `System` / `System.Management.Automation` modules and their members have no static
# types. Confine the resulting pyright noise to this file (the "typed facade over untyped
# third-party" rule), like the psutil / win32com adapters.
# pyright: reportMissingImports=false, reportMissingTypeStubs=false, reportUnusedImport=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...domain.errors import FeatureUnavailableError, PowerShellError

_INSTALL_HINT = (
    ".NET (hosts PowerShell 7.6 in-process) is unavailable: the optional dependencies "
    "are not installed. Install them with: pip install pwshpy[full] "
    "(also requires the .NET 10 runtime and PowerShell 7.6 present)."
)
_DOTNET_HINT = (
    ".NET could not start the .NET runtime: pwshpy[full] is installed but the "
    ".NET 10 runtime was not found. Install the .NET 10 runtime (or set DOTNET_ROOT)."
)

_lock = threading.Lock()
_runspace: Any = None  # a shared, opened Runspace reused across calls (single-runspace mode)
_pool: Any = None  # an opened RunspacePool (pool mode; concurrent, no shared $global state)
_powershell_cls: Any = None  # System.Management.Automation.PowerShell
_ps_types: dict[str, Any] = {}  # cached .NET type refs for output marshaling (PSObject, IEnumerable, ...)
_ready = False


def _pool_size() -> int:
    """Read the requested RunspacePool size from the environment (0/1 -> single runspace).

    Example:
        >>> import os
        >>> os.environ.pop("PWSHPY_RUNSPACE_POOL_SIZE", None) and None
        >>> _pool_size()
        0
    """
    raw = os.environ.get("PWSHPY_RUNSPACE_POOL_SIZE", "")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def is_available() -> bool:
    """Return whether the ``[full]`` extra (pythonnet) is importable.

    This answers "is the EXTRA installed", NOT "does .NET work" - the extra is only the
    first of three requirements (extra, .NET 10 runtime, PowerShell 7.6 SDK). Use
    :func:`is_runtime_available` when you need to know whether a call would actually
    succeed; a guard built on this one passes on a box that has pythonnet but no runtime,
    and the call then raises :class:`FeatureUnavailableError`.

    Example:
        >>> isinstance(is_available(), bool)
        True
    """
    return importlib.util.find_spec("pythonnet") is not None


def is_runtime_available() -> bool:
    """Return whether .NET can actually start (extra AND runtime AND SDK all present).

    :func:`is_available` only reports the extra, so it says True on a machine carrying
    pythonnet without a .NET runtime - exactly the shape of the dev box, where the
    project venv installs ``[full]`` but no runtime exists. The honest way to answer
    "would a call work" is to attempt the one-time init and see, so this delegates to
    :func:`_init` rather than re-deriving its preconditions, which would drift from it.

    Cost is bounded: it short-circuits on a spec lookup when the extra is absent, and
    ``_init`` caches success in ``_ready``, so a box that HAS .NET pays the
    initialization it was going to pay anyway. A box that lacks it pays one failed load.

    Example:
        >>> isinstance(is_runtime_available(), bool)
        True
    """
    if not is_available():
        return False
    try:
        _init()
    except FeatureUnavailableError:
        return False
    return True


def _powershell_home() -> Path:
    """Locate the PowerShell 7 install directory (holds the SDK assemblies)."""
    override = os.environ.get("PWSHPY_POWERSHELL_HOME")
    if override:
        return Path(override)
    exe = shutil.which("pwsh")
    if exe:
        return Path(exe).resolve().parent
    return Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "PowerShell" / "7"


def _init() -> None:
    """Load the CLR + PowerShell SDK and open the shared runspace/pool (once, under the lock)."""
    global _runspace, _pool, _powershell_cls, _ready  # noqa: PLW0603 - intentional one-time host singleton
    if _ready:
        return
    home = _powershell_home()
    sma = home / "System.Management.Automation.dll"
    if not sma.exists():
        msg = (
            f".NET requires the PowerShell 7.6 SDK assemblies, not found at {home}. "
            "Install PowerShell 7.6, or set PWSHPY_POWERSHELL_HOME to its directory."
        )
        raise FeatureUnavailableError(msg)
    # Point clr_loader at a user-scope .NET install (~/.dotnet) when no DOTNET_ROOT is set and no
    # machine-wide install is on the default search path; a machine install is found automatically.
    dotnet_user = Path.home() / ".dotnet"
    if "DOTNET_ROOT" not in os.environ and dotnet_user.exists():
        os.environ["DOTNET_ROOT"] = str(dotnet_user)
    try:
        from pythonnet import load

        load("coreclr")
    except Exception as exc:  # a missing/broken .NET runtime; surface a clear message, not a CLR dump
        raise FeatureUnavailableError(f"{_DOTNET_HINT} ({exc})") from exc

    import clr  # noqa: F401 - registers the CLR import hook
    from System import AppDomain, ResolveEventHandler
    from System.Reflection import Assembly

    def _resolve(_sender: Any, args: Any) -> Any:
        short = str(args.Name).split(",", 1)[0]
        candidate = home / f"{short}.dll"
        return Assembly.LoadFrom(str(candidate)) if candidate.exists() else None

    AppDomain.CurrentDomain.AssemblyResolve += ResolveEventHandler(_resolve)
    Assembly.LoadFrom(str(sma))

    from System.Collections import IDictionary, IEnumerable
    from System.Management.Automation import PowerShell, PSObject
    from System.Management.Automation.Runspaces import RunspaceFactory

    size = _pool_size()
    if size > 1:
        _pool = RunspaceFactory.CreateRunspacePool(1, size)
        _pool.Open()
    else:
        runspace = RunspaceFactory.CreateRunspace()
        runspace.Open()
        _runspace = runspace
    _powershell_cls = PowerShell
    _ps_types.update({"PSObject": PSObject, "IEnumerable": IEnumerable, "IDictionary": IDictionary})
    _ready = True


def _marshal_arg(value: Any) -> Any:
    """Marshal a Python value into a .NET parameter/argument (the SAFE binding path).

    Values are passed to ``AddParameter``/``AddArgument`` as discrete bound objects -
    never string-interpolated into a script - so they can never break out into
    executable PowerShell.  Primitives pass through pythonnet; a list/tuple becomes a
    .NET ``object[]`` (so a cmdlet sees an array); a dict becomes a ``Hashtable`` (for
    splat/``-Property``-style params); anything else is stringified.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        from System import Array, Object

        arr = Array.CreateInstance(Object, len(value))
        for index, item in enumerate(value):
            arr[index] = _marshal_arg(item)
        return arr
    if isinstance(value, dict):
        from System.Collections import Hashtable

        table = Hashtable()
        for key, item in value.items():
            table[str(key)] = _marshal_arg(item)
        return table
    return str(value)


def _marshal_value(value: Any) -> Any:
    """Normalize a single .NET/PS output value to a JSON-friendly Python type.

    Primitives pass through; a nested ``PSObject`` recurses into a
    :class:`~pwshpy.domain.records.PSObjectRecord`; a dictionary/array becomes a
    Python dict/list (recursively); any other .NET type is stringified.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    pso = _ps_types.get("PSObject")
    if pso is not None and isinstance(value, pso):
        return _marshal(value)
    idict = _ps_types.get("IDictionary")
    if idict is not None and isinstance(value, idict):
        return {str(key): _marshal_value(value[key]) for key in value.Keys}
    ienum = _ps_types.get("IEnumerable")
    if ienum is not None and isinstance(value, ienum):
        return [_marshal_value(item) for item in value]
    return str(value)  # DateTime, Guid, and other .NET types we do not model -> string


def _marshal(obj: Any) -> Any:
    """Marshal one result PSObject: a wrapped primitive -> Python value, else a PSObjectRecord."""
    from ...domain.records import PSObjectRecord

    base = getattr(obj, "BaseObject", obj)
    if base is None or isinstance(base, (str, int, float, bool)):
        return base
    properties = {str(prop.Name): _marshal_value(prop.Value) for prop in obj.Properties}
    type_names = obj.TypeNames
    type_name = str(type_names[0]) if type_names.Count else ""
    return PSObjectRecord(type_name=type_name, properties=properties)


def _collect_streams(shell: Any) -> dict[str, list[str]]:
    """Render every non-output PowerShell stream to a list of strings."""
    streams = shell.Streams
    return {
        "errors": [str(record) for record in streams.Error],
        "warnings": [str(record) for record in streams.Warning],
        "verbose": [str(record) for record in streams.Verbose],
        "debug": [str(record) for record in streams.Debug],
        "information": [str(record) for record in streams.Information],
    }


def _build_result(shell: Any, results: Any) -> Any:
    """Assemble a PSInvocationResult from a completed shell (output + all streams)."""
    from ...domain.records import PSInvocationResult

    return PSInvocationResult(
        output=[_marshal(obj) for obj in results],
        had_errors=bool(shell.HadErrors),
        **_collect_streams(shell),
    )


def _invoke(shell: Any, timeout: float | None) -> Any:
    """Invoke the pipeline, stopping it (cancel) if it outruns ``timeout``."""
    if timeout is None:
        return shell.Invoke()
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["ok"] = shell.Invoke()
        except Exception as exc:  # carried across the thread boundary and re-raised in the caller
            box["err"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        shell.Stop()
        worker.join(5)
        raise PowerShellError(f".NET script exceeded its {timeout}s timeout and was stopped.")
    if "err" in box:
        raise box["err"]
    return box["ok"]


def _ensure_ready() -> None:
    """Initialize the host once, under the lock (idempotent)."""
    with _lock:
        _init()


def _new_shell() -> Any:
    """Create a PowerShell bound to the shared runspace, or the pool in pool mode."""
    shell = _powershell_cls.Create()
    if _pool is not None:
        shell.RunspacePool = _pool
    else:
        shell.Runspace = _runspace
    return shell


def _dispatch(configure: Callable[[Any], None], timeout: float | None) -> Any:
    """Run one hosted invocation and return its :class:`PSInvocationResult`.

    ``configure`` adds the script or command+parameters to the shell.  In the default
    single-runspace mode the whole invocation is serialized on ``_lock`` (a runspace is
    not concurrency-safe); in pool mode the ``RunspacePool`` handles concurrency, so no
    lock is held across the call.
    """
    if not is_available():
        raise FeatureUnavailableError(_INSTALL_HINT)
    _ensure_ready()
    guard: Any = contextlib.nullcontext() if _pool is not None else _lock
    with guard:
        shell = _new_shell()
        configure(shell)
        try:
            try:
                results = _invoke(shell, timeout)
            except PowerShellError:
                raise  # already ours (e.g. the timeout path)
            except Exception as exc:  # a terminating .NET exception -> keep the PwshPyError invariant
                raise PowerShellError(f".NET invocation failed: {exc}") from exc
            return _build_result(shell, results)
        finally:
            shell.Dispose()


def run(script: str, *, timeout: float | None = None) -> list[Any]:
    """Execute ``script`` in the hosted PowerShell engine and return marshaled objects.

    Args:
        script: PowerShell source to run in-process. An explicit trust boundary -
            never feed it unvalidated external input.
        timeout: Optional seconds before the host stops (cancels) the pipeline.

    Returns:
        One marshaled item per output object (primitives stay primitive; structured
        objects become :class:`~pwshpy.domain.records.PSObjectRecord`).

    Raises:
        FeatureUnavailableError: If the ``[full]`` extra or the .NET/PowerShell host is absent.
        PowerShellError: If the script writes to the error stream or exceeds the timeout.

    Example:
        >>> run("Get-Process")  # doctest: +SKIP
        [PSObjectRecord(type_name='System.Diagnostics.Process', ...), ...]
    """
    result = _dispatch(lambda shell: shell.AddScript(script), timeout)
    if result.had_errors and result.errors:
        raise PowerShellError(result.errors[0])
    return list(result.output)


#: A fixed script (no interpolation) that projects a command's parameter metadata into
#: plain objects; the command name is bound SAFELY as the ``$Name`` parameter, never
#: string-interpolated. ``AddScript(...).AddParameter("Name", name)`` feeds the param block.
_GET_COMMAND_SCRIPT = """
param([string]$Name)
$cmd = Get-Command -Name $Name -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $cmd) { return }
[pscustomobject]@{
    Name = $cmd.Name
    CommandType = $cmd.CommandType.ToString()
    Module = [string]$cmd.ModuleName
    Parameters = @($cmd.Parameters.Values | ForEach-Object {
        $mandatory = $false
        foreach ($attr in $_.Attributes) {
            if ($attr -is [System.Management.Automation.ParameterAttribute] -and $attr.Mandatory) { $mandatory = $true }
        }
        [pscustomobject]@{
            Name = $_.Name
            Type = $_.ParameterType.Name
            Mandatory = $mandatory
            Aliases = ($_.Aliases -join ',')
        }
    })
}
"""


def get_command(name: str) -> Any:
    """Introspect a command's parameter metadata as a typed CommandInfo (.NET).

    Runs ``Get-Command`` in the hosted engine and projects its ``CommandInfo`` into a
    :class:`~pwshpy.domain.records.CommandInfo` (name, command type, module, and each
    parameter's name / .NET type / mandatory flag / aliases).  The command name is
    bound as a script parameter, never string-interpolated.

    Raises:
        FeatureUnavailableError: If the ``[full]`` extra or the host is absent.
        PowerShellError: If the command does not exist (Get-Command errors).

    Example:
        >>> get_command("Get-Item")  # doctest: +SKIP
        CommandInfo(name='Get-Item', command_type='Cmdlet', ...)
    """
    result = _dispatch(lambda shell: shell.AddScript(_GET_COMMAND_SCRIPT).AddParameter("Name", name), None)
    if result.had_errors and result.errors:
        raise PowerShellError(result.errors[0])
    if not result.output:
        raise PowerShellError(f"Get-Command returned no metadata for {name!r}.")
    return _to_command_info(result.output[0])


def _to_command_info(record: Any) -> Any:
    """Build a CommandInfo from the marshaled ``Get-Command`` PSObjectRecord."""
    from ...domain.records import CommandInfo, CommandParameter, PSObjectRecord

    props = record.properties if isinstance(record, PSObjectRecord) else {}
    parameters = []
    for raw in props.get("Parameters") or []:
        fields = raw.properties if isinstance(raw, PSObjectRecord) else {}
        aliases = [alias for alias in str(fields.get("Aliases") or "").split(",") if alias]
        parameters.append(
            CommandParameter(
                name=str(fields.get("Name") or ""),
                type=str(fields.get("Type") or ""),
                mandatory=bool(fields.get("Mandatory")),
                aliases=aliases,
            )
        )
    return CommandInfo(
        name=str(props.get("Name") or ""),
        command_type=str(props.get("CommandType") or ""),
        module=str(props.get("Module") or ""),
        parameters=parameters,
    )


def invoke(name: str, *args: Any, timeout: float | None = None, **params: Any) -> Any:
    """Run a single cmdlet with **safely bound** parameters; return the full result.

    Parameters are bound through ``AddParameter``/``AddArgument`` as discrete values,
    never string-interpolated into a script, so a value can never break out into
    executable PowerShell.  Use PowerShell parameter names as keyword arguments
    (``invoke("Get-ChildItem", Path="C:/", Recurse=True)``); a ``bool`` binds to a
    ``[switch]`` and a list becomes an array.

    Unlike :func:`run`, this does NOT raise on the error stream - the errors are
    returned in the result's ``errors``/``had_errors`` so the caller can decide.

    Args:
        name: The cmdlet/command name (e.g. ``"Get-Service"``).
        *args: Positional arguments, bound in order via ``AddArgument``.
        timeout: Optional seconds before the host stops (cancels) the pipeline.
        **params: Named parameters, bound via ``AddParameter`` (PascalCase PS names).

    Returns:
        A :class:`~pwshpy.domain.records.PSInvocationResult` with the marshaled
        ``output`` plus the error/warning/verbose/debug/information streams.

    Raises:
        FeatureUnavailableError: If the ``[full]`` extra or the .NET/PowerShell host is absent.
        PowerShellError: If the invocation exceeds the timeout.

    Example:
        >>> invoke("Get-Item", Path="C:/")  # doctest: +SKIP
        PSInvocationResult(output=[PSObjectRecord(...)], ...)
    """

    def _configure(shell: Any) -> None:
        shell.AddCommand(name)
        for arg in args:
            shell.AddArgument(_marshal_arg(arg))
        for key, value in params.items():
            shell.AddParameter(key, _marshal_arg(value))

    return _dispatch(_configure, timeout)


__all__ = ["get_command", "invoke", "is_available", "run"]
