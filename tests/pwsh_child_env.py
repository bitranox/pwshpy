"""What a pwsh 7 child does to PATH and PSModulePath at startup, as pure functions.

The env oracle (``test_portable_pwsh_oracle.test_env_matches_get_childitem_env``) reads
``Get-ChildItem env:`` in a pwsh 7 child of the pytest process. Before that script runs, pwsh 7
rewrites two variables of its own process, so the child's values are NOT the values it inherited:

* ``PATH``: ``ConsoleHost`` puts the directory of ``pwsh.exe`` plus ``;`` in front, unless PATH
  already starts with exactly that text (an ordinal, case-sensitive check); an empty PATH becomes
  that text alone. Source: PowerShell ``src/Microsoft.PowerShell.ConsoleHost/host/msh/ConsoleHost.cs``.
* ``PSModulePath``: ``ModuleIntrinsics.SetModulePath`` inserts the user, shared and builtin pwsh
  module dirs, in that order, in FRONT of the inherited value, skipping any already present
  (compared case-insensitively with the trailing separator trimmed), and leaves the inherited text
  as it was. Source: ``src/System.Management.Automation/engine/Modules/ModuleIntrinsics.cs``
  (``GetModulePath`` and ``UpdatePath``).

Both rewrites are idempotent, so they are visible only when pytest was NOT itself started under
pwsh 7: under a pwsh 7 parent the inherited values already carry pwsh's entries and the child
changes nothing, under a Windows PowerShell 5.1 parent it prepends them. Modelling them lets the
oracle compare these two variables exactly too, whichever shell launched pytest.

Deliberately not modelled (each would surface as an oracle failure, never pass silently): a
``%VAR%`` reference inside the inherited PSModulePath (pwsh expands it), a process value equal to
the user-scope registry value (pwsh then appends the machine value), module paths overridden in
``powershell.config.json``, and pwsh installed as a dotnet global tool.
"""

from __future__ import annotations

from collections.abc import Iterable

_SEPARATOR = ";"
_DIRECTORY_SEPARATORS = "\\/"


def expected_child_path(parent_path: str, exe_home: str) -> str:
    """Return the PATH a pwsh 7 child holds after startup, given the PATH it inherited.

    Args:
        parent_path: PATH as the parent process holds it.
        exe_home: Directory of the child's ``pwsh.exe`` (``[Environment]::ProcessPath``'s parent).

    Returns:
        The inherited PATH with ``exe_home;`` in front, or unchanged when it already starts so.

    Examples:
        >>> expected_child_path("D:/tools", "D:/pwsh")
        'D:/pwsh;D:/tools'
        >>> expected_child_path("D:/pwsh;D:/tools", "D:/pwsh")
        'D:/pwsh;D:/tools'
    """
    front = exe_home + _SEPARATOR
    if not parent_path:
        return front
    return parent_path if parent_path.startswith(front) else front + parent_path


def _trimmed(entry: str) -> str:
    """Strip whitespace and ONE trailing directory separator, as .NET TrimEndingDirectorySeparator does."""
    entry = entry.strip()
    return entry[:-1] if len(entry) > 1 and entry[-1] in _DIRECTORY_SEPARATORS else entry


def _entries(value: str) -> list[str]:
    """Split a combined path string into its non-empty, trimmed entries."""
    return [_trimmed(part) for part in value.split(_SEPARATOR) if part.strip()]


def expected_child_module_path(parent_module_path: str, insert_paths: Iterable[str]) -> str:
    """Return the PSModulePath a pwsh 7 child holds after startup, given the value it inherited.

    Args:
        parent_module_path: PSModulePath as the parent process holds it (non-empty in practice).
        insert_paths: The child's user, shared and builtin module dirs, in that order (each may
            itself be a combined, ``;``-separated value).

    Returns:
        The missing insert entries, each followed by ``;``, in front of the unchanged inherited
        value; the inserts alone when nothing was inherited.

    Examples:
        >>> expected_child_module_path("D:/b", ["D:/a", "d:/B/"])
        'D:/a;D:/b'
    """
    seen = {entry.casefold() for entry in _entries(parent_module_path)}
    prefix: list[str] = []
    for combined in insert_paths:
        for entry in _entries(combined):
            if entry.casefold() not in seen:
                prefix.append(entry)
                seen.add(entry.casefold())
    if not parent_module_path:
        return _SEPARATOR.join(prefix)
    return "".join(entry + _SEPARATOR for entry in prefix) + parent_module_path


__all__ = ["expected_child_module_path", "expected_child_path"]
