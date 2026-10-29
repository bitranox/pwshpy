"""The PATH / PSModulePath rewrite a pwsh 7 child applies at startup, modelled for the env oracle.

``test_env_matches_get_childitem_env`` compares pwshpy's view of the pytest process's environment
with ``Get-ChildItem env:`` read in a pwsh 7 child. pwsh rewrites two variables before any script
runs, so the oracle must expect those rewrites rather than equality. Whether the rewrite changes
anything depends on the shell that launched pytest: under a pwsh 7 parent both variables already
carry pwsh's entries and the rewrite is a no-op; under a Windows PowerShell 5.1 parent (or cmd) it
prepends them. These tests pin both shapes and the idempotence that makes the first one a no-op.
PATHEXT gains ``.CPL`` whatever the parent, unless it already holds it; the cases below are the
ones measured against pwsh 7.6.9 on Windows.
"""

from __future__ import annotations

import pytest
from pwsh_child_env import expected_child_module_path, expected_child_path, expected_child_pathext

_HOME = r"C:\Program Files\PowerShell\7"
_USER_MODULES = r"C:\Users\someone\Documents\PowerShell\Modules"
_SHARED_MODULES = r"C:\Program Files\PowerShell\Modules"
_BUILTIN_MODULES = r"c:\program files\powershell\7\Modules"
_INSERTS = (_USER_MODULES, _SHARED_MODULES, _BUILTIN_MODULES)
_WINPS_MODULES = (
    r"C:\Users\someone\Documents\WindowsPowerShell\Modules;C:\Program Files\WindowsPowerShell\Modules;"
    r"C:\WINDOWS\system32\WindowsPowerShell\v1.0\Modules"
)


@pytest.mark.os_agnostic
def test_path_gains_the_pwsh_home_in_front_under_a_parent_without_it() -> None:
    """A Windows PowerShell 5.1 parent: the child prepends its exe home, the rest stays verbatim."""
    parent = r"C:\Tools\Scripts\;C:\WINDOWS\system32;C:\Program Files\PowerShell\7\;C:\x"
    assert expected_child_path(parent, _HOME) == _HOME + ";" + parent


@pytest.mark.os_agnostic
def test_path_is_unchanged_when_it_already_starts_with_the_pwsh_home() -> None:
    """A pwsh 7 parent already put its home in front, so the child adds nothing."""
    parent = _HOME + r";C:\WINDOWS\system32"
    assert expected_child_path(parent, _HOME) == parent


@pytest.mark.os_agnostic
def test_path_front_check_is_ordinal_and_needs_the_separator() -> None:
    """pwsh compares the front ordinally with the separator, so a case or trailing-slash variant is prepended."""
    assert expected_child_path(_HOME.lower() + ";C:\\x", _HOME) == _HOME + ";" + _HOME.lower() + ";C:\\x"
    assert expected_child_path(_HOME + "\\;C:\\x", _HOME) == _HOME + ";" + _HOME + "\\;C:\\x"


@pytest.mark.os_agnostic
def test_an_empty_path_becomes_the_pwsh_home_alone() -> None:
    """An empty PATH is replaced by the home plus a separator, as pwsh does."""
    assert expected_child_path("", _HOME) == _HOME + ";"


@pytest.mark.os_agnostic
def test_module_path_gains_the_pwsh_dirs_in_front_of_windows_powershell_ones() -> None:
    """A 5.1 parent: user, shared and builtin pwsh module dirs go in front, in that order."""
    expected = ";".join(_INSERTS) + ";" + _WINPS_MODULES
    assert expected_child_module_path(_WINPS_MODULES, _INSERTS) == expected


@pytest.mark.os_agnostic
def test_module_path_skips_entries_already_present_case_and_slash_insensitively() -> None:
    """An entry already present (other case, trailing separator) is not inserted again."""
    parent = _SHARED_MODULES.upper() + "\\;" + _WINPS_MODULES
    expected = _USER_MODULES + ";" + _BUILTIN_MODULES + ";" + parent
    assert expected_child_module_path(parent, _INSERTS) == expected


@pytest.mark.os_agnostic
def test_module_path_insert_drops_the_trailing_separator() -> None:
    """The inserted entry is the normalised one, without its trailing separator."""
    assert expected_child_module_path(r"C:\m", [_USER_MODULES + "\\"]) == _USER_MODULES + r";C:\m"


@pytest.mark.os_agnostic
def test_an_empty_module_path_becomes_the_inserts_alone() -> None:
    """With nothing inherited the child's value is the pwsh dirs alone."""
    assert expected_child_module_path("", _INSERTS) == ";".join(_INSERTS)


@pytest.mark.os_agnostic
def test_both_rewrites_are_idempotent_which_is_why_a_pwsh_parent_sees_no_change() -> None:
    """Applying a rewrite to its own output changes nothing: a pwsh 7 parent already applied it."""
    path_once = expected_child_path(r"C:\WINDOWS\system32", _HOME)
    assert expected_child_path(path_once, _HOME) == path_once
    module_once = expected_child_module_path(_WINPS_MODULES, _INSERTS)
    assert expected_child_module_path(module_once, _INSERTS) == module_once


_PATHEXT = ".COM;.EXE;.BAT;.CMD;.PY;.PYW"


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (_PATHEXT, _PATHEXT + ";.CPL"),
        (_PATHEXT + ";", _PATHEXT + ";.CPL"),
        (".COM;.EXE;;", ".COM;.EXE;;.CPL"),
        (".COM;.EXE;.CPLX", ".COM;.EXE;.CPLX;.CPL"),
        (".COM;.EXE; .CPL", ".COM;.EXE; .CPL;.CPL"),
        ("", ".CPL"),
        (None, ".CPL"),
    ],
)
def test_pathext_gains_cpl_at_the_end_when_no_entry_is_exactly_cpl(parent: str | None, child: str) -> None:
    """pwsh appends ``.CPL`` (after a ``;`` unless the value ends with one); a near-miss entry does not count."""
    assert expected_child_pathext(parent) == child


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "parent", [".COM;.CPL;.EXE", ".COM;.cpl;.EXE", _PATHEXT + ";.CPL", _PATHEXT + ";.CPL;", ".CPL"]
)
def test_pathext_is_unchanged_when_an_entry_is_cpl_in_any_case(parent: str) -> None:
    """An inherited ``.CPL`` entry, in any case and position, leaves the value verbatim."""
    assert expected_child_pathext(parent) == parent


@pytest.mark.os_agnostic
def test_pathext_rewrite_is_idempotent() -> None:
    """A pwsh 7 parent already appended ``.CPL``, so its child changes nothing."""
    once = expected_child_pathext(_PATHEXT)
    assert expected_child_pathext(once) == once
