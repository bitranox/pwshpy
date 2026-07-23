"""Pure packing domain logic: os_agnostic, hermetic, no filesystem at all.

Covers the two rules the packer's correctness rests on - which imports are candidates for
embedding, and that the runner template is fully substituted before it ships.
"""

from __future__ import annotations

import pytest

from pwshpy.domain.enums import ImportKind
from pwshpy.domain.errors import PackError
from pwshpy.domain.packing import (
    ARGV_ENCODING_TAG,
    SHIM_MODULE,
    candidate_relative_paths,
    chunk_base64,
    classify_import,
    extract_script_metadata,
    iter_import_candidates,
    render_runner,
    render_shim,
)

_FULL_TEMPLATE = (
    "@@PWSHPY_PAYLOAD_B64@@|@@PWSHPY_PAYLOAD_SHA256@@|@@PWSHPY_ENTRY@@|"
    "@@PWSHPY_UV_ARGS@@|@@PWSHPY_FILE_HASHES@@|@@PWSHPY_SHIM@@|@@PWSHPY_ARGV_ENV@@"
)


@pytest.mark.os_agnostic
def test_plain_import_yields_module_and_parents() -> None:
    """``import a.b.c`` needs a, a.b and a.b.c - importing a submodule runs its parents."""
    assert sorted(iter_import_candidates("import a.b.c\n")) == ["a", "a.b", "a.b.c"]


@pytest.mark.os_agnostic
def test_from_import_yields_both_readings() -> None:
    """``from pkg import name`` may mean a submodule or an attribute; both are candidates."""
    assert sorted(iter_import_candidates("from pkg import name\n")) == ["pkg", "pkg.name"]


@pytest.mark.os_agnostic
def test_star_import_does_not_yield_a_star_module() -> None:
    """``from pkg import *`` contributes the package only, never a module literally named '*'."""
    assert sorted(iter_import_candidates("from pkg import *\n")) == ["pkg"]


@pytest.mark.os_agnostic
def test_relative_import_anchors_at_the_modules_own_package() -> None:
    """``from . import sib`` inside pkg/mod.py resolves to pkg.sib, not sib."""
    found = sorted(iter_import_candidates("from . import sib\n", module_package=("pkg",)))
    assert found == ["pkg", "pkg.sib"]


@pytest.mark.os_agnostic
def test_relative_import_climbs_one_level_per_extra_dot() -> None:
    """``from ..other import thing`` in a.b climbs to a.other."""
    found = sorted(iter_import_candidates("from ..other import thing\n", module_package=("a", "b")))
    assert "a.other" in found and "a.other.thing" in found


@pytest.mark.os_agnostic
def test_relative_import_escaping_the_root_is_dropped() -> None:
    """A relative import climbing past the pack root yields nothing rather than a wrong file."""
    assert list(iter_import_candidates("from ... import x\n", module_package=("pkg",))) == []


@pytest.mark.os_agnostic
def test_conditional_and_nested_imports_are_found() -> None:
    """ast.walk reaches imports inside try/except and function bodies, not just module level."""
    source = "def f():\n    import lazy\ntry:\n    import optional\nexcept ImportError:\n    optional = None\n"
    found = sorted(iter_import_candidates(source))
    assert found == ["lazy", "optional"]


@pytest.mark.os_agnostic
def test_unparseable_source_raises_pack_error() -> None:
    """A syntax error surfaces as a typed PackError naming the file, not a bare SyntaxError."""
    with pytest.raises(PackError, match=r"tool\.py"):
        list(iter_import_candidates("def (:\n", filename="tool.py"))


@pytest.mark.os_agnostic
def test_local_module_shadows_a_stdlib_name() -> None:
    """A local json.py shadows the stdlib at runtime, so it must be classified local and packed."""
    kind = classify_import("json", stdlib_names=frozenset({"json"}), local_names=frozenset({"json"}))
    assert kind is ImportKind.LOCAL


@pytest.mark.os_agnostic
def test_stdlib_submodule_is_classified_by_its_root() -> None:
    """``json.decoder`` is stdlib because ``json`` is; only the root name is looked up."""
    kind = classify_import("json.decoder", stdlib_names=frozenset({"json"}), local_names=frozenset())
    assert kind is ImportKind.STDLIB


@pytest.mark.os_agnostic
def test_unknown_module_is_external() -> None:
    """Anything neither stdlib nor local is external - uv's problem, not the packer's."""
    kind = classify_import("rich.console", stdlib_names=frozenset({"json"}), local_names=frozenset())
    assert kind is ImportKind.EXTERNAL


@pytest.mark.os_agnostic
def test_candidate_paths_cover_module_and_package() -> None:
    """A dotted name may be a module file or a package directory."""
    assert candidate_relative_paths("pkg.helper") == ("pkg/helper.py", "pkg/helper/__init__.py")


@pytest.mark.os_agnostic
def test_pep723_block_is_extracted_verbatim() -> None:
    """The block is lifted exactly as written - uv parses it, so it must not be reformatted."""
    source = '# /// script\n# requires-python = ">=3.10"\n# dependencies = ["rich"]\n# ///\nimport rich\n'
    assert extract_script_metadata(source) == source[: source.index("import rich")]


@pytest.mark.os_agnostic
def test_absent_pep723_block_is_empty_string() -> None:
    """A script with no inline metadata packs fine; it just declares nothing."""
    assert extract_script_metadata("import os\nprint(os.name)\n") == ""


@pytest.mark.os_agnostic
def test_unterminated_pep723_block_raises() -> None:
    """An unclosed block is a spec error worth catching at pack time, not on the target host."""
    with pytest.raises(PackError, match="not closed"):
        extract_script_metadata('# /// script\n# dependencies = ["rich"]\nimport rich\n')


@pytest.mark.os_agnostic
def test_shim_carries_metadata_and_targets_the_entry() -> None:
    """The shim gets the entry's block, because uv reads metadata from the file it is handed."""
    shim = render_shim(entry="tool.py", metadata="# /// script\n# ///\n")
    assert shim.startswith("# /// script\n# ///\n")
    assert "'tool.py'" in shim
    assert ARGV_ENCODING_TAG in shim


@pytest.mark.os_agnostic
def test_chunk_base64_wraps_and_leaves_content_intact() -> None:
    """Wrapping is presentational; joining the lines must give the original blob back."""
    blob = "A" * 250
    wrapped = chunk_base64(blob, width=120)
    assert wrapped.splitlines() == ["A" * 120, "A" * 120, "A" * 10]
    assert "".join(wrapped.splitlines()) == blob


@pytest.mark.os_agnostic
def test_render_runner_substitutes_every_placeholder() -> None:
    """No @@PWSHPY_...@@ may survive into a shipped runner."""
    rendered = render_runner(
        _FULL_TEMPLATE,
        payload_b64="Ym9keQ==",
        payload_sha256="ab12",
        entry="tool.py",
        uv_args=["--with", "rich"],
        file_hashes=[("ff", "tool.py")],
    )
    assert "@@PWSHPY" not in rendered
    assert rendered.split("|") == [
        "Ym9keQ==",
        "ab12",
        "tool.py",
        "'--with','rich'",
        "ff *tool.py",
        SHIM_MODULE,
        "PWSHPY_PACK_ARGV",
    ]


@pytest.mark.os_agnostic
def test_render_runner_rejects_a_template_missing_a_placeholder() -> None:
    """Shipping a runner with a literal placeholder would fail cryptically on the target host."""
    with pytest.raises(PackError, match="@@PWSHPY_ENTRY@@"):
        render_runner(
            _FULL_TEMPLATE.replace("@@PWSHPY_ENTRY@@", ""),
            payload_b64="x",
            payload_sha256="y",
            entry="e",
            uv_args=[],
            file_hashes=[],
        )


@pytest.mark.os_agnostic
def test_uv_args_are_single_quoted_for_powershell() -> None:
    """Values reach PowerShell as literal single-quoted strings, with quotes doubled."""
    rendered = render_runner(
        _FULL_TEMPLATE,
        payload_b64="x",
        payload_sha256="y",
        entry="e",
        uv_args=["it's"],
        file_hashes=[],
    )
    assert "'it''s'" in rendered
