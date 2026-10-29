"""Every public ``Ps`` method documents each parameter under ``Args:`` and its result under ``Returns:``.

A heading-exists grep passes over an incomplete ``Args:`` block, so this check compares the
documented names against the signature: a parameter added later without its entry is a red test.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterator
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
    # A parameter removed while its entry stays is the commonest drift, so a stale entry is a gap too.
    missing += [f"Args entry for nonexistent {name!r}" for name in sorted(documented - set(parameters))]
    if signature.return_annotation is inspect.Signature.empty:
        missing.append("return annotation")
    elif signature.return_annotation not in (None, "None"):
        wanted = "Yields" if inspect.isgeneratorfunction(method) else "Returns"
        if wanted not in sections:
            missing.append(f"{wanted}: section")
        elif not any(line.strip() for line in sections[wanted]):
            missing.append(f"{wanted}: section is empty")
    return missing


#: The public members ``Ps`` had when this guard was written; fewer means the walk stopped seeing them.
_MIN_PUBLIC_METHODS = 91


def _public_members() -> dict[str, object]:
    return {name: member for name, member in vars(Ps).items() if not name.startswith("_")}


def _public_methods() -> list[tuple[str, Callable[..., Any]]]:
    return [(name, member) for name, member in _public_members().items() if inspect.isfunction(member)]


@pytest.mark.os_agnostic
def test_the_walk_sees_every_public_facade_member() -> None:
    # A staticmethod, property or wrapper is not a plain function and would be skipped silently.
    others = sorted(name for name, member in _public_members().items() if not inspect.isfunction(member))
    assert others == [], f"public Ps members the docstring check cannot read: {others}"
    assert len(_public_methods()) >= _MIN_PUBLIC_METHODS


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


@pytest.mark.os_agnostic
def test_the_check_reports_an_entry_for_a_parameter_that_does_not_exist() -> None:
    def stale(self: object, a: int) -> None:
        """Do something.

        Args:
            a: Kept.
            b: Removed from the signature, entry left behind.
        """

    assert _missing_sections(stale) == ["Args entry for nonexistent 'b'"]


@pytest.mark.os_agnostic
def test_the_check_reports_an_empty_result_section_and_a_missing_annotation() -> None:
    def empty_returns(self: object) -> int:
        """Do something.

        Returns:
        """
        return 0

    def unannotated(self: object):  # no return annotation: the case under test
        """Do something."""

    assert _missing_sections(empty_returns) == ["Returns: section is empty"]
    assert _missing_sections(unannotated) == ["return annotation"]


@pytest.mark.os_agnostic
def test_the_check_wants_yields_for_a_generator() -> None:
    def generator(self: object) -> Iterator[int]:
        """Do something.

        Returns:
            Not the right section for a generator.
        """
        yield 0

    def documented_generator(self: object) -> Iterator[int]:
        """Do something.

        Yields:
            Each value.
        """
        yield 0

    assert _missing_sections(generator) == ["Yields: section"]
    assert _missing_sections(documented_generator) == []
