"""Every public ``Ps`` method documents each parameter under ``Args:`` and its result under ``Returns:``.

A heading-exists grep passes over an incomplete ``Args:`` block, so this check compares the
documented names against the signature: a parameter added later without its entry is a red test.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Any

import pytest

from pwshpy.composition import Ps

#: A Google-style section header: a capitalised word (or two) and a colon alone on its line.
_HEADER = re.compile(r"^(?P<name>[A-Z][A-Za-z]*(?: [A-Za-z]+)?):\s*$")

#: An ``Args:`` entry: the parameter name (stars allowed), an optional ``(type)``, then a colon.
_ARG_ENTRY = re.compile(r"^(?P<name>\*{0,2}[A-Za-z_][A-Za-z0-9_]*)(?: \([^)]*\))?:")


def _sections(doc: str) -> dict[str, list[str]]:
    """Split a cleaned docstring into its sections, each mapped to its indented body lines."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in doc.splitlines():
        header = _HEADER.match(line)
        if header:
            current = sections.setdefault(header.group("name"), [])
            continue
        if line and not line[0].isspace():
            current = None
        elif current is not None:
            current.append(line)
    return sections


def _documented_args(body: list[str]) -> set[str]:
    """Return the parameter names an ``Args:`` body documents, stars stripped."""
    indents = [len(line) - len(line.lstrip()) for line in body if line.strip()]
    if not indents:
        return set()
    entry_indent = min(indents)
    names: set[str] = set()
    for line in body:
        if line.strip() and len(line) - len(line.lstrip()) == entry_indent:
            entry = _ARG_ENTRY.match(line.strip())
            if entry:
                names.add(entry.group("name").lstrip("*"))
    return names


def _missing_sections(method: Callable[..., Any]) -> list[str]:
    """Name every docstring gap of ``method``: undocumented parameters and a missing result section."""
    sections = _sections(inspect.getdoc(method) or "")
    signature = inspect.signature(method)
    parameters = [name for name in signature.parameters if name != "self"]
    documented = _documented_args(sections.get("Args", []))
    missing = [f"Args entry for {name!r}" for name in parameters if name not in documented]
    if signature.return_annotation not in (None, "None", inspect.Signature.empty):
        wanted = "Yields" if inspect.isgeneratorfunction(method) else "Returns"
        if wanted not in sections:
            missing.append(f"{wanted}: section")
    return missing


def _public_methods() -> list[tuple[str, Callable[..., Any]]]:
    return [
        (name, member) for name, member in vars(Ps).items() if not name.startswith("_") and inspect.isfunction(member)
    ]


@pytest.mark.os_agnostic
def test_every_public_facade_method_documents_its_args_and_result() -> None:
    gaps = {name: missing for name, method in _public_methods() if (missing := _missing_sections(method))}
    report = "\n".join(f"{name}: {', '.join(missing)}" for name, missing in sorted(gaps.items()))
    assert not gaps, f"{len(gaps)} Ps methods have docstring gaps:\n{report}"


@pytest.mark.os_agnostic
def test_the_check_sees_a_complete_docstring_as_complete() -> None:
    # get_windows_image documents both parameters and its result, so the parser must find them.
    assert _documented_args(_sections(inspect.getdoc(Ps.get_windows_image) or "").get("Args", [])) == {"path", "index"}
    assert _missing_sections(Ps.get_windows_image) == []


@pytest.mark.os_agnostic
def test_the_check_reports_every_gap_of_an_undocumented_method() -> None:
    def undocumented(self: object, name: str, *args: int, timeout: float = 1.0, **params: str) -> int:
        """Do something, documenting nothing."""
        return 0

    assert _missing_sections(undocumented) == [
        "Args entry for 'name'",
        "Args entry for 'args'",
        "Args entry for 'timeout'",
        "Args entry for 'params'",
        "Returns: section",
    ]


@pytest.mark.os_agnostic
def test_the_check_counts_star_entries_and_ignores_continuation_lines() -> None:
    def documented(self: object, name: str, *args: int, **params: str) -> None:
        """Do something.

        Args:
            name: The name. A continuation line follows,
                timeout: which looks like an entry but is indented deeper.
            *args: Positional values.
            **params: Keyword values.
        """

    assert _documented_args(_sections(inspect.getdoc(documented) or "").get("Args", [])) == {"name", "args", "params"}
    assert _missing_sections(documented) == []
