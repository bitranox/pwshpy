"""Pure packing domain logic: os_agnostic, hermetic, no filesystem at all.

Covers the two rules the packer's correctness rests on - which imports are candidates for
embedding, and that the runner template is fully substituted before it ships.
"""

from __future__ import annotations

import shlex

import pytest

from pwshpy.domain.enums import ImportKind
from pwshpy.domain.errors import PackError
from pwshpy.domain.packing import (
    ARGV_ENCODING_TAG,
    SHIM_MODULE,
    FileDigest,
    RunnerContent,
    candidate_relative_paths,
    chunk_base64,
    classify_import,
    extract_requires_python,
    extract_script_metadata,
    iter_import_candidates,
    render_posix_runner,
    render_runner,
    render_shim,
    split_relative_requirement,
)

_FULL_TEMPLATE = (
    "@@PWSHPY_PAYLOAD_B64@@|@@PWSHPY_PAYLOAD_SHA256@@|@@PWSHPY_ENTRY@@|"
    "@@PWSHPY_UV_ARGS@@|@@PWSHPY_FILE_HASHES@@|@@PWSHPY_SHIM@@|@@PWSHPY_ARGV_ENV@@|"
    "@@PWSHPY_CWD_ENV@@|@@PWSHPY_UV_ARGS_RELATIVE@@|@@PWSHPY_PYTHON_REQUEST@@"
)

_SH_TEMPLATE = (
    "@@PWSHPY_PAYLOAD_B64@@|@@PWSHPY_PAYLOAD_SHA256@@|@@PWSHPY_ENTRY@@|@@PWSHPY_UV_ARGS@@|@@PWSHPY_FILE_HASHES@@|"
    "@@PWSHPY_SHIM@@|@@PWSHPY_ARGV_ENV@@|@@PWSHPY_CWD_ENV@@|@@PWSHPY_UV_ARGS_ANCHORED@@|@@PWSHPY_PYTHON_REQUEST@@"
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
        RunnerContent(
            payload_b64="Ym9keQ==",
            payload_sha256="ab12",
            entry="tool.py",
            uv_args=["--with", "rich"],
            file_hashes=[FileDigest("ff", "tool.py")],
            python_request=">=3.11",
        ),
    )
    assert "@@PWSHPY" not in rendered
    assert rendered.split("|") == [
        "Ym9keQ==",
        "ab12",
        "'tool.py'",
        "'--with','rich'",
        "ff *tool.py",
        SHIM_MODULE,
        "PWSHPY_PACK_ARGV",
        "PWSHPY_PACK_CWD",
        "'',''",
        "'>=3.11'",
    ]


@pytest.mark.os_agnostic
def test_render_runner_rejects_a_template_missing_a_placeholder() -> None:
    """Shipping a runner with a literal placeholder would fail cryptically on the target host."""
    with pytest.raises(PackError, match="@@PWSHPY_ENTRY@@"):
        render_runner(
            _FULL_TEMPLATE.replace("@@PWSHPY_ENTRY@@", ""),
            RunnerContent(payload_b64="x", payload_sha256="y", entry="e"),
        )


@pytest.mark.os_agnostic
def test_uv_args_are_single_quoted_for_powershell() -> None:
    """Values reach PowerShell as literal single-quoted strings, with quotes doubled."""
    rendered = render_runner(
        _FULL_TEMPLATE,
        RunnerContent(payload_b64="x", payload_sha256="y", entry="e", uv_args=["it's"]),
    )
    assert "'it''s'" in rendered


@pytest.mark.os_agnostic
def test_render_posix_runner_substitutes_its_placeholders() -> None:
    """The .sh runner has no shim/argv placeholders; the rest must all be filled."""
    rendered = render_posix_runner(
        _SH_TEMPLATE,
        RunnerContent(
            payload_b64="Ym9keQ==",
            payload_sha256="ab12",
            entry="tool.py",
            uv_args=["--with", "rich"],
            file_hashes=[FileDigest("ff", "tool.py")],
            python_request=">=3.11",
        ),
    )
    assert "@@PWSHPY" not in rendered
    payload, sha, entry, uv_args, hashes, shim, argv_env, cwd_env, anchored, request = rendered.split("|")
    assert shlex.split(request) == [">=3.11"]
    assert [payload, sha, shim, argv_env, cwd_env] == [
        "Ym9keQ==",
        "ab12",
        SHIM_MODULE,
        "PWSHPY_PACK_ARGV",
        "PWSHPY_PACK_CWD",
    ]
    assert _sh_uv_args(anchored) == ["--with", "rich"]
    assert [*shlex.split(entry), *shlex.split(hashes)] == ["tool.py", "ff  tool.py"]
    assert _sh_uv_args(uv_args) == ["--with", "rich"]


@pytest.mark.os_agnostic
def test_render_posix_runner_rejects_a_template_missing_a_placeholder() -> None:
    """A literal placeholder in a shipped .sh would break on the target host."""
    with pytest.raises(PackError, match="@@PWSHPY_ENTRY@@"):
        render_posix_runner(
            _SH_TEMPLATE.replace("@@PWSHPY_ENTRY@@", ""),
            RunnerContent(payload_b64="x", payload_sha256="y", entry="e"),
        )


def _sh_uv_args(word: str) -> list[str]:
    """What the .sh runner makes of the rendered uv args: one sh word, then the list it holds.

    shlex in POSIX mode reads sh quoting, so this is the assignment followed by the runner's eval.
    """
    (assigned,) = shlex.split(word)
    return shlex.split(assigned)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "value",
    ["it's", "pkg$HOME", "a`id`b", 'say "hi"', "back\\slash", "pkg[ab]*?", "~/x"],
    ids=["quote", "dollar", "backtick", "double-quote", "backslash", "glob", "tilde"],
)
def test_posix_uv_args_survive_the_assignment_and_the_eval_literally(value: str) -> None:
    """[90]: a uv arg reaches uv as given, whatever sh would otherwise expand in it."""
    rendered = render_posix_runner(
        _SH_TEMPLATE,
        RunnerContent(payload_b64="x", payload_sha256="y", entry="e", uv_args=["--with", value]),
    )
    assert _sh_uv_args(rendered.split("|")[3]) == ["--with", value]


#: The characters PowerShell reads as a single quote: the ASCII one and four typographic ones
#: (measured with pwsh 7.6: each ends an unescaped single-quoted literal). By code point, so no
#: typographic character is typed into this file.
_POWERSHELL_QUOTES = (0x27, 0x2018, 0x2019, 0x201A, 0x201B)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("code_point", _POWERSHELL_QUOTES, ids=[f"U+{c:04X}" for c in _POWERSHELL_QUOTES])
def test_every_powershell_quote_in_a_uv_arg_is_doubled(code_point: int) -> None:
    """[61]: a quote PowerShell recognises is doubled, the way EscapeSingleQuotedStringContent does it."""
    quote = chr(code_point)
    rendered = render_runner(
        _FULL_TEMPLATE,
        RunnerContent(payload_b64="x", payload_sha256="y", entry="e", uv_args=[f"a{quote}b"]),
    )
    assert rendered.split("|")[3] == f"'a{quote}{quote}b'"


@pytest.mark.os_agnostic
def test_a_prime_is_not_a_powershell_quote() -> None:
    """[61]: only the five quote characters are doubled; a look-alike such as U+2032 stays single."""
    prime = chr(0x2032)
    rendered = render_runner(
        _FULL_TEMPLATE,
        RunnerContent(payload_b64="x", payload_sha256="y", entry="e", uv_args=[f"a{prime}b"]),
    )
    assert rendered.split("|")[3] == f"'a{prime}b'"


@pytest.mark.os_agnostic
def test_the_entry_name_is_a_quoted_literal_in_the_powershell_runner() -> None:
    """[62]: the entry name lands as a PowerShell literal, so it's.py keeps the runner parseable."""
    template = "$PwshPyEntry = @@PWSHPY_ENTRY@@|" + _FULL_TEMPLATE.replace("@@PWSHPY_ENTRY@@|", "")
    rendered = render_runner(template, RunnerContent(payload_b64="x", payload_sha256="y", entry="it's.py"))
    assert rendered.startswith("$PwshPyEntry = 'it''s.py'|")


@pytest.mark.os_agnostic
def test_the_entry_name_and_file_list_are_quoted_words_in_the_posix_runner() -> None:
    """[62]: the entry name and the hash list each land as one sh word holding the value as given."""
    rendered = render_posix_runner(
        _SH_TEMPLATE,
        RunnerContent(payload_b64="x", payload_sha256="y", entry="it's.py", file_hashes=[FileDigest("ff", "it's.py")]),
    )
    _, _, entry, _, hashes = rendered.split("|")[:5]
    assert shlex.split(entry) == ["it's.py"]
    assert shlex.split(hashes) == ["ff  it's.py"]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("name", ["a\nb.py", "a\rb.py"], ids=["newline", "carriage-return"])
@pytest.mark.parametrize("render", [render_runner, render_posix_runner], ids=["ps1", "sh"])
def test_a_line_break_in_a_packed_name_is_refused(name: str, render: object) -> None:
    """[62]: both runners keep the entry and the file list one name per line, so a line break cannot be packed."""
    template = _FULL_TEMPLATE if render is render_runner else _SH_TEMPLATE
    content = RunnerContent(payload_b64="x", payload_sha256="y", entry="e.py", file_hashes=[FileDigest("ff", name)])
    with pytest.raises(PackError, match="line break"):
        render(template, content)  # type: ignore[operator]


@pytest.mark.os_agnostic
def test_a_comment_terminator_in_the_entry_name_is_refused_for_powershell() -> None:
    """[62]: the .ps1 names its entry inside its <# #> help block, which a '#>' in the name would end."""
    with pytest.raises(PackError, match="#>"):
        render_runner(_FULL_TEMPLATE, RunnerContent(payload_b64="x", payload_sha256="y", entry="a#>b.py"))
    rendered = render_posix_runner(_SH_TEMPLATE, RunnerContent(payload_b64="x", payload_sha256="y", entry="a#>b.py"))
    assert shlex.split(rendered.split("|")[2]) == ["a#>b.py"]


@pytest.mark.os_agnostic
def test_a_placeholder_spelled_in_a_value_is_not_substituted_again() -> None:
    """A value is substituted once: an entry named like a later placeholder keeps its own name."""
    rendered = render_runner(
        _FULL_TEMPLATE,
        RunnerContent(payload_b64="x", payload_sha256="y", entry="@@PWSHPY_SHIM@@.py", uv_args=["--with", "z"]),
    )
    assert rendered.split("|")[2] == "'@@PWSHPY_SHIM@@.py'"


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("./x.whl", ("", "./x.whl")),
        ("../up/x", ("", "../up/x")),
        (".", ("", ".")),
        ("wheels/x.whl", ("", "wheels/x.whl")),
        ("wheels\\x.whl", ("", "wheels\\x.whl")),
        ("x-1.0-py3-none-any.whl", ("", "x-1.0-py3-none-any.whl")),
        ("x-1.0.tar.gz", ("", "x-1.0.tar.gz")),
        ("tool @ ./tool", ("tool @ ", "./tool")),
        ("tool[cli]@../tool ; python_version > '3'", ("tool[cli]@", "../tool ; python_version > '3'")),
        ("./a@b/x.whl", ("", "./a@b/x.whl")),
    ],
)
def test_a_relative_with_path_is_split_for_re_anchoring(spec: str, expected: tuple[str, str]) -> None:
    """[57]: what uv reads as a path relative to its working directory is split off its ``name @``."""
    assert split_relative_requirement(spec) == expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "spec",
    [
        "rich",
        "rich>=13",
        "pkg[ab]",
        "/abs/x.whl",
        "C:\\wheels\\x.whl",
        "\\\\server\\share\\x.whl",
        "~/x.whl",
        "$HOME/x.whl",
        "file:./x",
        "tool @ https://example.invalid/tool.whl",
        "git+https://example.invalid/r.git",
        "tool @ file:///abs/tool",
        "",
    ],
)
def test_anything_else_is_left_alone(spec: str) -> None:
    """[57]: a requirement, URL, absolute or home-relative path is never re-anchored."""
    assert split_relative_requirement(spec) is None


@pytest.mark.os_agnostic
def test_only_with_values_are_re_anchored_in_both_runners() -> None:
    """[57]: a relative --with value is spelled from the caller-prefix variable; nothing else changes."""
    content = RunnerContent(
        payload_b64="x", payload_sha256="y", entry="e", uv_args=["--with", "./x.whl", "--with", "rich"]
    )
    ps1 = render_runner(_FULL_TEMPLATE, content).split("|")
    assert ps1[8] == "'','./x.whl','',''"
    assert ps1[3] == "'--with','./x.whl','--with','rich'"
    sh = render_posix_runner(_SH_TEMPLATE, content).split("|")
    (anchored,) = shlex.split(sh[8])
    assert anchored == "'--with' ''\"$PWSHPY_CALLER_PREFIX\"'./x.whl' '--with' 'rich'"
    assert _sh_uv_args(sh[3]) == ["--with", "./x.whl", "--with", "rich"]


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ('# /// script\n# requires-python = ">=3.11,<3.14"\n# ///\n', ">=3.11,<3.14"),
        ("# /// script\n# requires-python = '>=3.12'  # a comment\n# ///\n", ">=3.12"),
        ('# /// script\n# dependencies = ["rich"]\n# ///\n', ""),
        ('# /// script\n# [tool.x]\n# requires-python = ">=3.12"\n# ///\n', ""),
        ("", ""),
    ],
    ids=["basic-string", "literal-string-and-comment", "absent", "inside-a-table", "no-block"],
)
def test_requires_python_is_read_from_the_top_level_of_the_block(block: str, expected: str) -> None:
    """[A1]: the request an elevated run hands uv python find is the block's own requires-python."""
    assert extract_requires_python(block) == expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "line",
    ['requires-python = ">=3.11; rm -rf /"', "requires-python = 3.11", 'requires-python = """>=3.11"""'],
    ids=["shell-characters", "not-a-string", "multi-line-string"],
)
def test_a_requires_python_that_is_not_a_plain_specifier_string_is_refused(line: str) -> None:
    """[A1]: the value travels into both runners and onto uv's command line; anything odd is refused."""
    with pytest.raises(PackError, match="requires-python"):
        extract_requires_python(f"# /// script\n# {line}\n# ///\n")
