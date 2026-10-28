"""Every adapter site that wraps a caught exception goes through marshal.native_error."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.os_agnostic

_SRC = Path(__file__).resolve().parents[1] / "src" / "pwshpy"
#: Built directly on purpose: a timeout carries no native code, and its constructor needs timeout=.
_ALLOWED_DIRECT = frozenset({"OperationTimeoutError"})
_WRAPPED = frozenset({"NativeCallError", "NativeAccessDeniedError", "ElevationDeclinedError"})


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
            if name in _WRAPPED and name not in _ALLOWED_DIRECT:
                offenders.append(f"{path.relative_to(_SRC)}:{node.lineno} raise {name}(...) from ...")
    return offenders, examined


def test_no_wrapping_site_bypasses_the_factory() -> None:
    offenders, examined = _offenders()
    assert examined > 50, f"scanned only {examined} files - wrong root?"
    assert offenders == [], "use marshal.native_error(message, exc):\n" + "\n".join(offenders)
