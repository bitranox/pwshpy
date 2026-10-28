"""Every adapter site that wraps a caught exception goes through marshal.native_error."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pwshpy.domain.errors import NativeCallError

pytestmark = pytest.mark.os_agnostic

_SRC = Path(__file__).resolve().parents[1] / "src" / "pwshpy"
#: Built directly on purpose: a timeout carries no native code, and its constructor needs timeout=.
_ALLOWED_DIRECT = frozenset({"OperationTimeoutError"})


def _family(cls: type[NativeCallError]) -> set[str]:
    """``cls`` and every subclass below it, by name, so a new subclass is guarded the day it lands.

    ``__subclasses__`` sees only classes whose module is imported; every subclass lives in
    ``domain/errors.py`` today, so a subclass defined elsewhere must be imported here too.
    """
    names = {cls.__name__}
    for sub in cls.__subclasses__():
        names |= _family(sub)
    return names


_WRAPPED = frozenset(_family(NativeCallError) - _ALLOWED_DIRECT)


def _offenders() -> tuple[list[str], int]:
    offenders: list[str] = []
    examined = 0
    for path in sorted(_SRC.rglob("*.py")):
        examined += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.cause is not None):
                continue
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in _WRAPPED:
                offenders.append(f"{path.relative_to(_SRC)}:{node.lineno} raise {name}(...) from ...")
    return offenders, examined


def test_the_guarded_names_are_the_whole_family_minus_the_allow_list() -> None:
    assert {"NativeCallError", "NativeAccessDeniedError", "ElevationDeclinedError"} <= _WRAPPED
    assert _family(NativeCallError) >= _ALLOWED_DIRECT, "an allow-list entry that is no subclass guards nothing"


def test_no_wrapping_site_bypasses_the_factory() -> None:
    offenders, examined = _offenders()
    assert examined > 50, f"scanned only {examined} files - wrong root?"
    assert offenders == [], "use marshal.native_error(message, exc):\n" + "\n".join(offenders)
