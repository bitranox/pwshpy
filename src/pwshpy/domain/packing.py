"""Pure packing logic - import analysis and runner rendering, no I/O.

The script packer turns a Python entry script plus the local modules it imports into a
single self-extracting ``.ps1``.  Everything in this module is a pure function over text:
parse a module's imports, classify each one, and substitute a rendered payload into the
runner template.  Reading files, building the archive, and writing the result all live in
:mod:`pwshpy.adapters.native.packer`.

Two design points are load-bearing:

* **Only local modules are embedded.** The standard library travels with the interpreter
  uv provisions, and third-party distributions come from the entry's PEP 723 block or
  ``--with``.  An import name is never guessed at as a distribution name (``yaml`` is
  PyYAML, ``cv2`` is opencv-python); guessing would install the wrong package silently.
* **The argument vector travels out of band.** Windows PowerShell 5.1 re-quotes arguments
  when it calls a native executable, mangling empty strings and embedded quotes, and only
  7.3+ fixed it.  The runner therefore base64-encodes the argument vector as JSON into an
  environment variable and :data:`SHIM_SOURCE` rebuilds ``sys.argv`` from it, so the packed
  script sees exactly what the caller typed on every host.

Contents:
    * :func:`iter_import_candidates` - dotted module names a source file imports.
    * :func:`classify_import` - stdlib / local / external for one dotted name.
    * :func:`candidate_relative_paths` - the file paths a dotted name may live at.
    * :func:`extract_script_metadata` - lift the entry's PEP 723 block onto the shim.
    * :func:`extract_requires_python` - the block's ``requires-python``, the request an elevated run makes.
    * :func:`render_shim` - the generated ``__main__`` shim for one entry.
    * :func:`render_runner` - substitute a payload into the runner template.
    * :func:`split_relative_requirement` - a ``--with`` path an elevated run re-anchors.
    * :func:`chunk_base64` - wrap a base64 blob to a fixed line width.
    * :func:`unquote_powershell_literal` - read back a name the PowerShell runner holds quoted.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .enums import ImportKind, RunnerFormat
from .errors import PackError


@dataclass(frozen=True, slots=True)
class PackOptions:
    """How to pack, bundled so :func:`pack_script` takes one options object, not four flags.

    A frozen value object, not a long keyword list: it keeps the packer's signature small and
    lets a caller build the choices once and pass them around.

    Attributes:
        include: Extra files to embed that import analysis cannot see (data files, dynamic
            imports).
        with_packages: Distributions spliced into the runner's ``uv run`` as ``--with`` args.
        root: Directory local imports resolve against; defaults to the entry's parent.
        force: Overwrite the destination if it already exists.
        format: Which runner to emit (see :class:`~pwshpy.domain.enums.RunnerFormat`); ``AUTO``
            picks ``.sh`` for a ``.sh`` destination, else a PowerShell ``.ps1``.
    """

    include: Sequence[str | Path] = ()
    with_packages: Sequence[str] = ()
    root: str | Path | None = None
    force: bool = False
    format: RunnerFormat = RunnerFormat.AUTO


#: The all-defaults options, shared as an immutable singleton so it can be a parameter default
#: without a per-call construction (which the bugbear B008 rule flags).
DEFAULT_PACK_OPTIONS = PackOptions()


@dataclass(frozen=True, slots=True)
class RunnerContent:
    """The rendered inputs :func:`render_runner` substitutes into the runner template.

    Bundled into one value object so the pure renderer takes ``(template, content)`` rather
    than six positional strings that are easy to transpose.

    Attributes:
        payload_b64: Base64 of the payload zip, already line-wrapped.
        payload_sha256: Hex digest of the payload zip; keys the extraction cache.
        entry: Archive-relative path of the entry script.
        uv_args: Extra arguments spliced into the ``uv run`` invocation.
        file_hashes: ``(digest, path)`` pairs the runner verifies before running.
        python_request: The entry's ``requires-python`` (see :func:`extract_requires_python`), which
            an elevated run hands ``uv python find``; ``""`` for none.
    """

    payload_b64: str
    payload_sha256: str
    entry: str
    uv_args: Sequence[str] = ()
    file_hashes: Sequence[FileDigest] = field(default_factory=tuple)
    python_request: str = ""


class FileDigest(NamedTuple):
    """One packed file's SHA-256 hex digest paired with its archive-relative path.

    A named pair, not a bare ``tuple[str, str]``: the runner verifies each file against its
    digest before executing, and a transposed ``(path, digest)`` would embed a manifest that
    silently never matches. Naming the fields makes the order impossible to get wrong at the
    one place they are built.
    """

    digest: str
    path: str


#: Name of the generated shim module inside the payload.  Dunder-prefixed so it cannot
#: collide with a user module, and so an ``import *`` never picks it up.
SHIM_MODULE = "__pwshpy_pack_main__.py"

#: PEP 723 inline-script block delimiters, matched by exact line.  Named so a typo in the
#: parser is a NameError here rather than a block that silently fails to extract.
_PEP723_OPEN = "# /// script"
_PEP723_CLOSE = "# ///"

#: Environment variable carrying the encoded argument vector from runner to shim.
ARGV_ENV_VAR = "PWSHPY_PACK_ARGV"

#: Version tag leading the encoded argument vector.  Without it a single empty-string
#: argument would encode to an empty blob, indistinguishable from "no arguments at all".
ARGV_ENCODING_TAG = "v1"

#: Environment variable through which an elevated run hands the shim the caller's working
#: directory. Elevated, uv starts in a directory only administrators can write (``--directory``),
#: so the script would otherwise run there; the shim changes back first. Unset, it changes nothing.
CWD_ENV_VAR = "PWSHPY_PACK_CWD"

#: The variable an elevated runner sets to the caller's PATH: uv runs with only the PATH
#: directories no other account can modify ([107]), and the shim gives the script the rest back.
PATH_ENV_VAR = "PWSHPY_PACK_PATH"

#: Exit code of a shim that cannot change back to the caller's directory (``EX_SOFTWARE``, the
#: code the runner uses when the script never started).
SHIM_CWD_EXIT = 70

#: The characters PowerShell reads as a single quote inside a single-quoted literal: the ASCII one
#: and the typographic U+2018, U+2019, U+201A and U+201B (measured with pwsh 7.6 and Windows
#: PowerShell 5.1; a look-alike such as the prime U+2032 is not one). By code point, so no
#: typographic character sits in this source.
_POWERSHELL_QUOTES = frozenset(chr(code) for code in (0x27, 0x2018, 0x2019, 0x201A, 0x201B))

#: Placeholders the runner template exposes for substitution.  Distinctive enough that
#: they cannot occur in hand-written PowerShell by accident.
_PLACEHOLDERS = (
    "@@PWSHPY_PAYLOAD_B64@@",
    "@@PWSHPY_PAYLOAD_SHA256@@",
    "@@PWSHPY_ENTRY@@",
    "@@PWSHPY_UV_ARGS@@",
    "@@PWSHPY_FILE_HASHES@@",
    "@@PWSHPY_SHIM@@",
    "@@PWSHPY_ARGV_ENV@@",
    "@@PWSHPY_CWD_ENV@@",
    "@@PWSHPY_UV_ARGS_RELATIVE@@",
    "@@PWSHPY_PYTHON_REQUEST@@",
)

#: The entry name as plain text, for the runners' help comments. Optional (a template may or may not
#: name the entry in prose); safe there because a name with a line break, or ``#>`` in a ``.ps1``,
#: is refused before rendering.
_ENTRY_NAME_PLACEHOLDER = "@@PWSHPY_ENTRY_NAME@@"

#: What ends the PowerShell runner's ``<# ... #>`` help block, which names the entry. A block
#: comment has no escape, so an entry name holding it cannot be packed to a ``.ps1``.
_POWERSHELL_COMMENT_END = "#>"

#: Placeholders the POSIX ``.sh`` runner exposes. It needs no out-of-band argv channel (POSIX
#: ``sh`` forwards ``"$@"`` intact), and an unelevated run executes the entry itself; only a root run
#: goes through the shim (to change back to the caller's directory and give back its PATH), so it
#: names the shim, the variables the shim reads, and the uv args with relative paths re-anchored.
_SH_PLACEHOLDERS = (
    "@@PWSHPY_PAYLOAD_B64@@",
    "@@PWSHPY_PAYLOAD_SHA256@@",
    "@@PWSHPY_ENTRY@@",
    "@@PWSHPY_UV_ARGS@@",
    "@@PWSHPY_FILE_HASHES@@",
    "@@PWSHPY_SHIM@@",
    "@@PWSHPY_ARGV_ENV@@",
    "@@PWSHPY_CWD_ENV@@",
    "@@PWSHPY_PATH_ENV@@",
    "@@PWSHPY_UV_ARGS_ANCHORED@@",
    "@@PWSHPY_PYTHON_REQUEST@@",
)

#: The variable the POSIX runner sets, for a root run, to the caller's directory with one trailing
#: separator; its re-anchored uv args (:func:`split_relative_requirement`) are spelled with it.
_POSIX_PREFIX_VARIABLE = '"$PWSHPY_CALLER_PREFIX"'

#: File suffixes uv reads as a local distribution even with no directory part (``x.whl``).
_DISTRIBUTION_SUFFIXES = (".whl", ".tar.gz", ".zip", ".tar.bz2", ".tar.xz", ".tgz")

#: ``name[extras] @`` in front of a direct reference (PEP 508), whitespace allowed around the ``@``.
_NAMED_REFERENCE = re.compile(r"^\s*[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\s*(?:\[[^\]]*\])?\s*@\s*")

#: The generated ``__main__`` shim.  It changes back to the caller's directory when an
#: elevated run hands one over, rebuilds ``sys.argv`` from the out-of-band environment
#: variable (or keeps its own arguments when there is none, as the ``.sh`` passes them), then
#: executes the real entry as ``__main__`` so ``if __name__ == "__main__"`` fires exactly as it
#: would for a direct ``python entry.py`` run.
#:
#: ``sys.argv[0]`` is set to the *entry* path, not the shim's, so that a packed script
#: calling ``ps.elevate()`` (which re-execs ``sys.executable`` + ``sys.argv``) reproduces
#: the invocation faithfully - the elevated child runs the entry directly, bypassing both
#: the shim and uv, against the environment uv already built.
#:
#: The shim also gives the entry back the caller's full PATH: an elevated run started uv on
#: trusted directories only, and the variable carrying the rest is consumed here so the script
#: never sees it.
SHIM_SOURCE = '''{metadata}"""Generated by pwshpy pack - returns to the caller's cwd, rebuilds argv, runs the entry."""

import base64
import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENTRY = os.path.join(_HERE, {entry!r})


def _return_to_the_callers_directory():
    """Change to the directory an elevated runner names: uv itself started elsewhere.

    A directory the elevated account cannot enter (an NFS export with root squashing) ends the
    run with one line naming it, before any of the script runs.
    """
    directory = os.environ.pop({cwd_env!r}, None)
    if directory is None:
        return
    try:
        os.chdir(directory)
    except OSError as error:
        reason = error.strerror or str(error)
        sys.stderr.write("pwshpy pack: cannot return to the working directory " + directory + ": " + reason + "\\n")
        raise SystemExit({cwd_exit!r})


def _restore_the_callers_path():
    """Give the script the PATH its caller had: the elevated runner ran uv on trusted directories only."""
    path = os.environ.pop({path_env!r}, None)
    if path is not None:
        os.environ["PATH"] = path


def _argv_from_environment():
    """Decode the out-of-band argument vector written by the PowerShell runner.

    Each argument is UTF-8 then base64, joined with "|" behind a version tag, so an
    empty-string argument survives and no separator can occur inside a piece. Without the
    variable (the POSIX runner forwards its arguments as they are) the shim's own are kept.
    """
    blob = os.environ.pop({env_var!r}, None)
    if blob is None:
        return sys.argv[1:]
    if not blob:
        return []
    tag, separator, rest = blob.partition("|")
    if tag != {tag!r}:
        raise SystemExit("pwshpy pack: unsupported argument encoding " + tag)
    if not separator:
        return []
    return [base64.b64decode(piece).decode("utf-8") for piece in rest.split("|")]


_return_to_the_callers_directory()
_restore_the_callers_path()
sys.argv = [_ENTRY] + _argv_from_environment()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
runpy.run_path(_ENTRY, run_name="__main__")
'''


def iter_import_candidates(
    source: str, *, module_package: Sequence[str] = (), filename: str = "<pack>"
) -> Iterator[str]:
    """Yield the dotted module names ``source`` imports, resolving relative imports.

    Both halves of ``from pkg import name`` are yielded (``pkg`` and ``pkg.name``), because
    ``name`` may be either a submodule to embed or an attribute of the package; the caller
    resolves which by looking for a file.  Parent packages of a dotted import are yielded
    too, since importing ``a.b.c`` executes ``a/__init__.py`` and ``a/b/__init__.py``.

    Args:
        source: Python source text.
        module_package: Package parts the module lives in, used to anchor relative imports
            (``("pkg",)`` for ``pkg/mod.py``).
        filename: Name used in the error message when the source does not parse.

    Raises:
        PackError: If ``source`` is not valid Python.

    Example:
        >>> sorted(iter_import_candidates("import a.b\\nfrom c import d"))
        ['a', 'a.b', 'c', 'c.d']
        >>> sorted(iter_import_candidates("from . import sib", module_package=("pkg",)))
        ['pkg', 'pkg.sib']
        >>> sorted(iter_import_candidates("from .. import top", module_package=("a", "b")))
        ['a', 'a.top']
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise PackError(f"{filename} is not valid Python: {exc}") from exc
    seen: set[str] = set()
    for node in ast.walk(tree):
        for dotted in _node_candidates(node, tuple(module_package)):
            if dotted and dotted not in seen:
                seen.add(dotted)
                yield dotted


def _node_candidates(node: ast.AST, package: tuple[str, ...]) -> Iterator[str]:
    """Yield the dotted names one import node contributes (with their parent packages)."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield from _with_parents(alias.name)
    elif isinstance(node, ast.ImportFrom):
        base = _resolve_from_base(node, package)
        if base is None:
            return
        if base:
            yield from _with_parents(base)
        for alias in node.names:
            if alias.name != "*":
                yield ".".join(filter(None, (base, alias.name)))


def _resolve_from_base(node: ast.ImportFrom, package: tuple[str, ...]) -> str | None:
    """Resolve the base package of a ``from ... import`` node, or ``None`` if it escapes.

    A relative import climbs ``level - 1`` packages from the module's own package.  One
    that climbs past the pack root cannot be embedded, so it is dropped rather than
    silently resolved to the wrong file.
    """
    if not node.level:
        return node.module or ""
    climb = node.level - 1
    if climb > len(package):
        return None
    anchor = package[: len(package) - climb] if climb else package
    return ".".join((*anchor, node.module)) if node.module else ".".join(anchor)


def _with_parents(dotted: str) -> Iterator[str]:
    """Yield ``a``, ``a.b``, ``a.b.c`` for ``a.b.c`` - importing a submodule runs its parents."""
    parts = dotted.split(".")
    for index in range(1, len(parts) + 1):
        yield ".".join(parts[:index])


def classify_import(dotted: str, *, stdlib_names: frozenset[str], local_names: frozenset[str]) -> ImportKind:
    """Classify a dotted import as stdlib, local, or external.

    ``local_names`` wins over ``stdlib_names``: a file sitting next to the entry shadows a
    standard-library module of the same name at runtime, so the pack must carry it too.

    Example:
        >>> std, loc = frozenset({"json"}), frozenset({"helper"})
        >>> classify_import("json.decoder", stdlib_names=std, local_names=loc).value
        'stdlib'
        >>> classify_import("helper", stdlib_names=std, local_names=loc).value
        'local'
        >>> classify_import("rich.console", stdlib_names=std, local_names=loc).value
        'external'
    """
    if dotted in local_names:
        return ImportKind.LOCAL
    if dotted.split(".", 1)[0] in stdlib_names:
        return ImportKind.STDLIB
    return ImportKind.EXTERNAL


def extract_script_metadata(source: str) -> str:
    """Return the entry's PEP 723 ``script`` block verbatim, or ``""`` if it has none.

    uv reads inline metadata from the file it is asked to run, and a pack runs the
    generated shim rather than the entry directly, so the block has to travel onto the
    shim - otherwise the declared dependencies and ``requires-python`` are silently
    ignored and the packed script fails on its first third-party import.

    Only the first block counts, matching PEP 723: a second one is a spec error, and
    duplicating it onto the shim would make uv reject the run outright.

    Example:
        >>> src = '# /// script\\n# dependencies = ["rich"]\\n# ///\\nimport rich\\n'
        >>> extract_script_metadata(src)
        '# /// script\\n# dependencies = ["rich"]\\n# ///\\n'
        >>> extract_script_metadata("import os\\n")
        ''
    """
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if line.rstrip() != _PEP723_OPEN:
            continue
        for end in range(index + 1, len(lines)):
            stripped = lines[end].rstrip()
            if stripped == _PEP723_CLOSE:
                return "\n".join(lines[index : end + 1]) + "\n"
            if not (stripped == "#" or stripped.startswith("# ")):
                break
        raise PackError(f"the entry's PEP 723 script block is not closed by a {_PEP723_CLOSE!r} line")
    return ""


#: The ``requires-python`` key at the start of a (stripped) line, bare or as a quoted TOML key:
#: ``"requires-python" =`` and an indented key are valid TOML that uv honours (measured), and a longer
#: bare key such as ``requires-python-note`` is another key.
_REQUIRES_PYTHON_KEY = re.compile(r"""(?:requires-python|"requires-python"|'requires-python')(?=\s*=)""")

#: A whole ``requires-python`` line inside a PEP 723 block (stripped); the value is a basic or
#: literal TOML string.
_REQUIRES_PYTHON_LINE = re.compile(
    _REQUIRES_PYTHON_KEY.pattern + r"""\s*=\s*"""
    r"""(?:"(?P<basic>[^"\\]*)"|'(?P<literal>[^']*)')"""
    r"""\s*(?:#.*)?"""
)

#: The characters a PEP 440 version specifier set is written with. The value travels into both runners
#: and onto uv's command line, so anything else is refused rather than quoted.
_SPECIFIER_CHARACTERS = re.compile(r"[0-9A-Za-z.,<>=!~*+ ]*")


#: How many quote characters open or close a TOML multi-line string, and the longest run that may close
#: one: up to two quote characters of content can sit right before the closing delimiter.
_MULTI_LINE_DELIMITER = 3
_LONGEST_CLOSING_RUN = 5


def _scan_toml_line(line: str, open_string: str = "") -> tuple[int, str]:
    """Scan one TOML line: arrays opened minus closed, and the multi-line string still open at its end.

    Brackets inside a string or after a comment do not count. ``open_string`` is the delimiter
    (``\"\"\"`` or ``'''``) of a multi-line string an earlier line left open, so the line starts inside
    it; the second value is the delimiter left open by this line, or ``""``. A single-line string
    never spans lines (TOML forbids it), so only a multi-line one carries over.

    Example:
        >>> _scan_toml_line('dependencies = [  # "]" in a comment')
        (1, '')
        >>> _scan_toml_line('  "a[1]", ["b\\\\"]"],')
        (0, '')
        >>> _scan_toml_line("]")
        (-1, '')
        >>> _scan_toml_line('x = \"\"\" [')
        (0, '\"\"\"')
        >>> _scan_toml_line("[x] ''', [", "'''")
        (1, '')
    """
    change = 0
    index = 0
    quote = open_string
    while index < len(line):
        if quote:
            index, quote = _string_end(line, index, quote)
            continue
        character = line[index]
        if character == "#":
            break
        if character in "\"'":
            # Three quotes open a multi-line string; fewer are a single-line one (or an empty one).
            multi_line = character * _MULTI_LINE_DELIMITER
            quote = multi_line if line.startswith(multi_line, index) else character
            index += len(quote)
            continue
        change += {"[": 1, "]": -1}.get(character, 0)
        index += 1
    return change, quote if len(quote) == _MULTI_LINE_DELIMITER else ""


def _string_end(line: str, start: int, quote: str) -> tuple[int, str]:
    """Where the ``quote``-delimited string that ``line[start:]`` is inside ends, and the quote still open.

    Returns ``(index past its closing delimiter, "")``, or ``(len(line), quote)`` when it runs past
    the line. Only a basic string (``"``) has backslash escapes; a literal one (``'``) ends at its next
    delimiter. A multi-line string's run of three to five quotes closes it: TOML lets up to two quote
    characters of content sit right before the delimiter (``\"\"\"a\"\"\"\"`` is ``a"``).
    """
    mark = quote[0]
    index = start
    while index < len(line):
        if mark == '"' and line[index] == "\\":
            index += 2
            continue
        if line[index] != mark:
            index += 1
            continue
        run = len(line[index:]) - len(line[index:].lstrip(mark))
        if len(quote) == 1:
            return index + 1, ""
        if run >= _MULTI_LINE_DELIMITER:
            return index + min(run, _LONGEST_CLOSING_RUN), ""
        index += run
    return len(line), quote


def extract_requires_python(metadata: str) -> str:
    """Return the ``requires-python`` of a PEP 723 block (as :func:`extract_script_metadata` returns it), or ``""``.

    An elevated run names its interpreter to uv outright (``--python <path>``, so no ``.venv`` above
    the directory uv starts in is probed), and uv then only warns when that interpreter is outside the
    script's ``requires-python``. So the runner asks ``uv python find`` for a managed Python matching
    this request first. Only a top-level key counts (a ``[tool...]`` table may hold its own; a line of
    a multi-line string is content, never a header or the key, see :func:`_scan_toml_line`), and only
    a plain quoted string of specifier characters is accepted: anything else would be a runner value
    no reader can check at a glance.

    Raises:
        PackError: When the key is there but is not such a string.

    Example:
        >>> extract_requires_python('# /// script\\n# requires-python = ">=3.11,<3.14"\\n# ///\\n')
        '>=3.11,<3.14'
        >>> extract_requires_python('# /// script\\n# dependencies = []\\n# ///\\n')
        ''
        >>> extract_requires_python("")
        ''
    """
    depth = 0
    open_string = ""
    for raw in metadata.splitlines()[1:-1]:
        # TOML allows whitespace before a key and a table header, so neither has to start the line.
        line = (raw[2:] if raw.startswith("# ") else raw[1:]).strip()
        # A line that starts inside a multi-line string is its content, whatever it spells. A line
        # opening with '[' inside a multi-line array (`#   ["x"],`) is one of its elements; only one
        # outside every open array is a table header, which ends the top level.
        if not open_string and line.startswith("[") and depth == 0:
            return ""
        if not open_string and _REQUIRES_PYTHON_KEY.match(line):
            return _requires_python_value(line)
        change, open_string = _scan_toml_line(line, open_string)
        depth = max(depth + change, 0)
    return ""


def _requires_python_value(line: str) -> str:
    """The specifier set a ``requires-python`` line (stripped) assigns.

    Raises:
        PackError: When the value is not one quoted string of specifier characters.
    """
    match = _REQUIRES_PYTHON_LINE.fullmatch(line)
    value = None if match is None else match.group("basic") or match.group("literal") or ""
    if value is None or _SPECIFIER_CHARACTERS.fullmatch(value) is None:
        raise PackError(
            f"the entry's requires-python must be one quoted string of version specifiers, got: {line.strip()!r}"
        )
    return value.strip()


def render_shim(*, entry: str, metadata: str) -> str:
    """Render the generated ``__main__`` shim that fronts ``entry`` inside a pack.

    Example:
        >>> shim = render_shim(entry="tool.py", metadata="")
        >>> "runpy.run_path(_ENTRY" in shim and "'tool.py'" in shim
        True
        >>> render_shim(entry="t.py", metadata="# /// script\\n# ///\\n").startswith("# /// script")
        True
    """
    return SHIM_SOURCE.format(
        metadata=metadata,
        entry=entry,
        env_var=ARGV_ENV_VAR,
        tag=ARGV_ENCODING_TAG,
        cwd_env=CWD_ENV_VAR,
        cwd_exit=SHIM_CWD_EXIT,
        path_env=PATH_ENV_VAR,
    )


def candidate_relative_paths(dotted: str) -> tuple[str, str]:
    """Return the ``(module.py, package/__init__.py)`` paths a dotted name may live at.

    Example:
        >>> candidate_relative_paths("pkg.helper")
        ('pkg/helper.py', 'pkg/helper/__init__.py')
    """
    stem = dotted.replace(".", "/")
    return f"{stem}.py", f"{stem}/__init__.py"


def chunk_base64(blob: str, width: int = 120) -> str:
    """Wrap a base64 blob to ``width`` characters per line for embedding in a here-string.

    Example:
        >>> chunk_base64("abcdef", width=2)
        'ab\\ncd\\nef'
        >>> chunk_base64("", width=4)
        ''
    """
    if width < 1:
        raise PackError(f"base64 line width must be positive, got {width}")
    return "\n".join(blob[index : index + width] for index in range(0, len(blob), width))


def render_runner(template: str, content: RunnerContent) -> str:
    """Substitute a payload into the runner template, returning the finished ``.ps1`` text.

    The entry name and every uv argument land as PowerShell single-quoted literals (see
    :func:`_powershell_literal`); the file list sits in a single-quoted here-string, which no
    ``<digest> *<path>`` line can end.

    Args:
        template: The runner template text, carrying every ``@@PWSHPY_*@@`` placeholder.
        content: The rendered payload inputs (see :class:`RunnerContent`).

    Raises:
        PackError: If the template is missing a placeholder, which would ship a runner with
            a literal ``@@PWSHPY_...@@`` in it, or a packed name cannot be written into the
            runner (a line break in any name, ``#>`` in the entry name).

    Example:
        >>> content = RunnerContent(payload_b64="Ym9keQ==", payload_sha256="ab12", entry="t.py",
        ...                         uv_args=["--with", "rich"], file_hashes=[FileDigest("ff", "t.py")])
        >>> render_runner("|".join(_PLACEHOLDERS), content).split("|")  # doctest: +NORMALIZE_WHITESPACE
        ['Ym9keQ==', 'ab12', "'t.py'", "'--with','rich'", 'ff *t.py', '__pwshpy_pack_main__.py',
         'PWSHPY_PACK_ARGV', 'PWSHPY_PACK_CWD', "'',''", "''"]
    """
    missing = [token for token in _PLACEHOLDERS if token not in template]
    if missing:
        raise PackError(f"runner template is missing placeholder(s): {', '.join(missing)}")
    _refuse_unrepresentable_names(content)
    if _POWERSHELL_COMMENT_END in content.entry:
        raise PackError(
            f"cannot pack {content.entry!r} to a .ps1: {_POWERSHELL_COMMENT_END!r} in the entry name would "
            "end the runner's help block early; rename the script"
        )
    return _substitute(
        template,
        {
            "@@PWSHPY_PAYLOAD_B64@@": content.payload_b64,
            "@@PWSHPY_PAYLOAD_SHA256@@": content.payload_sha256,
            "@@PWSHPY_ENTRY@@": _powershell_literal(content.entry),
            _ENTRY_NAME_PLACEHOLDER: content.entry,
            "@@PWSHPY_UV_ARGS@@": _powershell_string_array(content.uv_args),
            "@@PWSHPY_FILE_HASHES@@": "\n".join(f"{fh.digest} *{fh.path}" for fh in content.file_hashes),
            "@@PWSHPY_SHIM@@": SHIM_MODULE,
            "@@PWSHPY_ARGV_ENV@@": ARGV_ENV_VAR,
            "@@PWSHPY_CWD_ENV@@": CWD_ENV_VAR,
            "@@PWSHPY_PATH_ENV@@": PATH_ENV_VAR,
            "@@PWSHPY_UV_ARGS_RELATIVE@@": _powershell_relative_suffixes(content.uv_args),
            "@@PWSHPY_PYTHON_REQUEST@@": _powershell_literal(content.python_request),
        },
    )


def _refuse_unrepresentable_names(content: RunnerContent) -> None:
    """Refuse a packed name holding a line break, which neither runner can carry.

    Both keep the file list one ``<digest> <path>`` per line and name the entry in comment lines,
    so a line break would split a name in two or start a line of code. No real script or data
    file needs one, so the pack is refused rather than the name encoded.

    Raises:
        PackError: Naming the first such name.

    Example:
        >>> _refuse_unrepresentable_names(RunnerContent(payload_b64="", payload_sha256="", entry="it's.py"))
    """
    for name in (content.entry, *(fh.path for fh in content.file_hashes)):
        if "\n" in name or "\r" in name:
            raise PackError(f"cannot pack {name!r}: a name holding a line break cannot be written into a runner")


def _substitute(template: str, replacements: dict[str, str]) -> str:
    """Replace every placeholder in ONE pass, so a value spelling a placeholder is left as it is.

    Replacing one placeholder after another would substitute again inside a value already written
    (an entry named ``@@PWSHPY_SHIM@@.py``). The replacement is a function, never a template
    string, so a backslash in a value is not read as a group reference.

    Example:
        >>> _substitute("@@A@@ @@B@@", {"@@A@@": "@@B@@", "@@B@@": "b"})
        '@@B@@ b'
    """
    pattern = re.compile("|".join(re.escape(token) for token in replacements))
    return pattern.sub(lambda match: replacements[match.group(0)], template)


def _powershell_string_array(values: Sequence[str]) -> str:
    """Render values as a PowerShell single-quoted list, each through :func:`_powershell_literal`.

    Example:
        >>> _powershell_string_array(["--with", "it's"])
        "'--with','it''s'"
        >>> _powershell_string_array([])
        ''
    """
    return ",".join(_powershell_literal(value) for value in values)


def _powershell_literal(value: str) -> str:
    """Quote ``value`` as one PowerShell single-quoted literal, every quote character doubled.

    A single-quoted PowerShell string is literal - no ``$`` expansion, no backtick escapes - so
    only a quote character needs care, and PowerShell reads five as one: the ASCII quote and the
    typographic U+2018 to U+201B. Doubling only the ASCII one left a typographic quote to end the
    literal early (the runner then failed to parse, or, with an even number of them, parsed into a
    different value). This is what ``CodeGeneration.EscapeSingleQuotedStringContent`` does, which
    the runner itself uses at run time, so the two always agree.

    Example:
        >>> _powershell_literal("it's")
        "'it''s'"
        >>> _powershell_literal("a" + chr(0x2019) + "b") == "'a" + chr(0x2019) * 2 + "b'"
        True
    """
    return "'" + "".join(char * 2 if char in _POWERSHELL_QUOTES else char for char in value) + "'"


def unquote_powershell_literal(body: str) -> str:
    """Undo :func:`_powershell_literal` on the text between a literal's outer quotes.

    Each quote character the packer doubled is read back as one, the way PowerShell reads it.

    Example:
        >>> unquote_powershell_literal("it''s.py")
        "it's.py"
        >>> unquote_powershell_literal(_powershell_literal("a" + chr(0x201A) + "b")[1:-1]) == "a" + chr(0x201A) + "b"
        True
    """
    value: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        value.append(char)
        doubled = char in _POWERSHELL_QUOTES and body[index + 1 : index + 2] == char
        index += 2 if doubled else 1
    return "".join(value)


def render_posix_runner(template: str, content: RunnerContent) -> str:
    """Substitute a payload into the POSIX ``.sh`` runner template.

    Like :func:`render_runner` but for ``/bin/sh``: the shim/argv placeholders do not apply
    (POSIX ``sh`` forwards ``"$@"`` and uv reads the entry's PEP 723 block directly). The entry
    name and the ``<digest>  <path>`` file list each land as one single-quoted ``sh`` word, and
    ``uv_args`` as ONE single-quoted word holding the single-quoted list (the runner assigns it as
    is and ``eval``-expands it at the run), so a ``'``, ``$``, backtick or ``"`` in any of them is
    taken literally, never parsed as shell.

    Raises:
        PackError: If the template is missing a ``@@PWSHPY_*@@`` placeholder, or a packed name
            holds a line break.

    Example:
        >>> content = RunnerContent(payload_b64="Ym9keQ==", payload_sha256="ab12", entry="t.py",
        ...                         uv_args=["--with", "httpx>=2"], file_hashes=[FileDigest("ff", "t.py")])
        >>> rendered = render_posix_runner("|".join(_SH_PLACEHOLDERS), content).split("|")
        >>> print(rendered[2], rendered[3], rendered[4])
        't.py' ''\\''--with'\\'' '\\''httpx>=2'\\''' 'ff  t.py'
    """
    missing = [token for token in _SH_PLACEHOLDERS if token not in template]
    if missing:
        raise PackError(f"POSIX runner template is missing placeholder(s): {', '.join(missing)}")
    _refuse_unrepresentable_names(content)
    hashes = "\n".join(f"{fh.digest}  {fh.path}" for fh in content.file_hashes)
    return _substitute(
        template,
        {
            "@@PWSHPY_PAYLOAD_B64@@": content.payload_b64,
            "@@PWSHPY_PAYLOAD_SHA256@@": content.payload_sha256,
            "@@PWSHPY_ENTRY@@": _posix_shell_word(content.entry),
            _ENTRY_NAME_PLACEHOLDER: content.entry,
            "@@PWSHPY_UV_ARGS@@": _posix_shell_word(_posix_shell_args(content.uv_args)),
            "@@PWSHPY_FILE_HASHES@@": _posix_shell_word(hashes),
            "@@PWSHPY_SHIM@@": SHIM_MODULE,
            "@@PWSHPY_ARGV_ENV@@": ARGV_ENV_VAR,
            "@@PWSHPY_CWD_ENV@@": CWD_ENV_VAR,
            "@@PWSHPY_PATH_ENV@@": PATH_ENV_VAR,
            "@@PWSHPY_UV_ARGS_ANCHORED@@": _posix_shell_word(_posix_anchored_args(content.uv_args)),
            "@@PWSHPY_PYTHON_REQUEST@@": _posix_shell_word(content.python_request),
        },
    )


def _posix_shell_args(values: Sequence[str]) -> str:
    """Render values as a POSIX ``sh`` single-quoted list, escaping embedded single quotes.

    A single-quoted ``sh`` word is fully literal (no ``$``, no globbing, no redirection), so a
    dependency spec like ``httpx>=2`` is safe - unquoted, its ``>`` would be a redirect.  The one
    character needing care is ``'`` itself, escaped the POSIX way as ``'\\''``.

    Example:
        >>> _posix_shell_args(["--with", "httpx>=2", "it's"])
        "'--with' 'httpx>=2' 'it'\\\\''s'"
        >>> _posix_shell_args([])
        ''
    """
    return " ".join(_posix_shell_word(value) for value in values)


def _posix_shell_word(value: str) -> str:
    """Quote ``value`` as one single-quoted POSIX ``sh`` word, literal whatever it holds.

    Example:
        >>> print(_posix_shell_word("a$b`c"))
        'a$b`c'
        >>> print(_posix_shell_word("it's"))
        'it'\\''s'
    """
    return "'" + value.replace("'", "'\\''") + "'"


def split_relative_requirement(spec: str) -> tuple[str, str] | None:
    """Split a ``--with`` value that names a path relative to the working directory, else ``None``.

    Returns ``(head, location)``: ``head`` is the ``name @ `` in front of a direct reference (empty
    for a bare path), ``location`` the relative path and anything after it. An elevated run starts
    uv in another directory, so the runner puts the caller's directory between the two and the
    value means what it means unelevated. Relative is what uv reads as a local path that is not
    absolute: ``./x``, ``../x``, anything with a directory separator, or a bare distribution file
    name (``x.whl``). A URL, a ``file:`` reference, an absolute or home-relative path, an
    environment-variable reference and a plain requirement (``rich>=13``) are left alone.

    Example:
        >>> split_relative_requirement("./dist/tool-1.0-py3-none-any.whl")
        ('', './dist/tool-1.0-py3-none-any.whl')
        >>> split_relative_requirement("tool @ ../tool")
        ('tool @ ', '../tool')
        >>> split_relative_requirement("tool-1.0.tar.gz")
        ('', 'tool-1.0.tar.gz')
        >>> [split_relative_requirement(s) for s in ("rich>=13", "/abs/x.whl", "file:./x", "~/x.whl")]
        [None, None, None, None]
    """
    named = _NAMED_REFERENCE.match(spec)
    head = named.group(0) if named and not spec.lstrip().startswith(".") else ""
    location = spec[len(head) :]
    return (head, location) if _is_relative_location(location) else None


def _is_relative_location(location: str) -> bool:
    """Whether ``location`` is a local path relative to the working directory (see the caller)."""
    token = re.split(r"[\s;]", location.strip(), maxsplit=1)[0]
    if not token or "://" in token or token.lower().startswith("file:"):
        return False
    if token[0] in "/\\~$" or re.match(r"^[A-Za-z]:", token):
        return False
    return "/" in token or "\\" in token or token in {".", ".."} or token.lower().endswith(_DISTRIBUTION_SUFFIXES)


def _anchored_values(values: Sequence[str]) -> Iterator[tuple[str, tuple[str, str] | None]]:
    """Yield each uv arg with its relative-path split when it is a ``--with`` value, else ``None``."""
    previous = ""
    for value in values:
        yield value, split_relative_requirement(value) if previous == "--with" else None
        previous = value


def _powershell_relative_suffixes(values: Sequence[str]) -> str:
    """Render, per uv arg, the relative path an elevated .ps1 run puts the caller's directory in front of.

    A PowerShell list parallel to the uv args: ``''`` for a value left as it is, else the value's
    relative-path tail (the head is the rest of the value). Data rather than an expression, so the
    runner code that joins the caller's directory in is code a linter sees.

    Example:
        >>> print(_powershell_relative_suffixes(["--with", "tool @ ./x.whl", "--with", "rich"]))
        '','./x.whl','',''
    """
    return ",".join(_powershell_literal(split[1] if split else "") for _, split in _anchored_values(values))


def _posix_anchored_args(values: Sequence[str]) -> str:
    """Render uv args as the ``sh`` list of :func:`_posix_shell_args`, relative ``--with`` paths re-anchored.

    A relative path becomes ``'head'"$PWSHPY_CALLER_PREFIX"'location'``: the runner's ``eval``
    expands that one variable, inside double quotes, so its value is never split or globbed.

    Example:
        >>> print(_posix_anchored_args(["--with", "./x.whl"]))
        '--with' ''"$PWSHPY_CALLER_PREFIX"'./x.whl'
    """
    rendered: list[str] = []
    for value, split in _anchored_values(values):
        if split is None:
            rendered.append(_posix_shell_word(value))
            continue
        head, location = split
        rendered.append(_posix_shell_word(head) + _POSIX_PREFIX_VARIABLE + _posix_shell_word(location))
    return " ".join(rendered)


__all__ = [
    "ARGV_ENCODING_TAG",
    "ARGV_ENV_VAR",
    "CWD_ENV_VAR",
    "DEFAULT_PACK_OPTIONS",
    "PATH_ENV_VAR",
    "FileDigest",
    "PackOptions",
    "RunnerContent",
    "SHIM_MODULE",
    "SHIM_CWD_EXIT",
    "SHIM_SOURCE",
    "candidate_relative_paths",
    "chunk_base64",
    "classify_import",
    "extract_requires_python",
    "extract_script_metadata",
    "iter_import_candidates",
    "render_posix_runner",
    "render_runner",
    "render_shim",
    "split_relative_requirement",
    "unquote_powershell_literal",
]
