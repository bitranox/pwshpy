"""native script packer - turn a Python script into a self-extracting ``.ps1``.

Handing a Python tool to someone normally means "install Python, clone this, make a venv,
pip install, then run it".  :func:`pack_script` collapses that to one file: the entry script
and the local modules it imports are embedded as a base64 zip inside a PowerShell runner that
unpacks itself, provisions ``uv`` if the machine has none, runs the script, and exits with the
script's own exit code.  :func:`unpack_script` is the inverse, so a pack can be edited and
packed again.

Only *local* modules are embedded.  Third-party distributions come from the entry's PEP 723
block (uv resolves it, and fetches a matching interpreter) or from ``--with``; an import name
is never guessed at as a distribution name, because it usually is not one - ``yaml`` is PyYAML
and ``cv2`` is opencv-python.  Imports that are neither stdlib nor local are reported in the
manifest's ``external_imports`` so a missing declaration is visible at pack time rather than
at 3am on someone else's machine.

Portable (stdlib + the domain's pure packing logic); works on every OS, and the artefact it
produces runs on Windows PowerShell 5.1 as well as pwsh 7 on any OS.

Contents:
    * :func:`pack_script` - pack an entry script and its local modules into a ``.ps1``.
    * :func:`unpack_script` - restore the sources a packed ``.ps1`` carries.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import sys
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path

from ...domain.enums import ImportKind
from ...domain.errors import PackError
from ...domain.packing import (
    SHIM_MODULE,
    candidate_relative_paths,
    chunk_base64,
    classify_import,
    extract_script_metadata,
    iter_import_candidates,
    render_runner,
    render_shim,
)
from ...domain.records import PackedScript
from .fileio import write_text

#: The runner template, shipped as package data next to this module.
_TEMPLATE_PATH = Path(__file__).with_name("templates") / "pack_runner.ps1"

#: Fixed archive timestamp.  Zip's epoch starts in 1980, and a real mtime would change the
#: payload hash between two packs of identical sources, defeating the runner's cache key.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: Permission bits recorded for every archive member (0o644 as a zip external attribute).
_ZIP_MODE = 0o644 << 16

#: The two lines bracketing the base64 payload inside a generated runner.
_PAYLOAD_OPEN = "$PwshPyPayload = @'"
_PAYLOAD_CLOSE = "'@"

#: Prefix of the line recording the entry name inside a generated runner.
_ENTRY_PREFIX = "$PwshPyEntry = '"


def pack_script(
    entry: str | Path,
    dest: str | Path | None = None,
    *,
    include: Sequence[str | Path] = (),
    with_packages: Sequence[str] = (),
    root: str | Path | None = None,
    force: bool = False,
) -> PackedScript:
    """Pack ``entry`` and the local modules it imports into a self-extracting ``.ps1``.

    Args:
        entry: The Python script to pack.
        dest: Output path; defaults to the entry's name with a ``.ps1`` suffix.
        include: Extra files to embed, for what import analysis cannot see - data files,
            templates, or a module reached through ``importlib``.
        with_packages: Distributions spliced into the runner's ``uv run`` invocation as
            ``--with`` arguments, on top of the entry's PEP 723 block.
        root: Directory local imports resolve against; defaults to the entry's parent.
            Every embedded file must live under it, so the archive has no ``..`` members.
        force: Overwrite ``dest`` if it already exists.

    Returns:
        A :class:`~pwshpy.domain.records.PackedScript` manifest of what was embedded.

    Raises:
        PackError: If the entry is missing or unreadable, a file to embed lies outside
            ``root``, the destination exists without ``force``, or the destination would
            clobber one of the inputs.

    Example:
        >>> import tempfile, pathlib
        >>> tmp = pathlib.Path(tempfile.mkdtemp())
        >>> _ = (tmp / "tool.py").write_text("import os\\nprint(os.name)\\n")
        >>> manifest = pack_script(tmp / "tool.py")
        >>> manifest.entry, manifest.file_count, manifest.output_path.endswith("tool.ps1")
        ('tool.py', 2, True)
    """
    entry_path = _existing_file(Path(entry), "entry script")
    root_path = Path(root).expanduser().resolve() if root is not None else entry_path.parent
    destination = Path(dest).expanduser() if dest is not None else entry_path.with_suffix(".ps1")
    sources, external = _collect_sources(entry_path, root_path, include)
    _guard_destination(destination, sources, force=force)

    entry_arc = _archive_name(entry_path, root_path)
    members = {_archive_name(path, root_path): _read_bytes(path) for path in sources}
    metadata = extract_script_metadata(_read_bytes(entry_path).decode("utf-8", "replace"))
    members[SHIM_MODULE] = render_shim(entry=entry_arc, metadata=metadata).encode("utf-8")

    payload = _build_archive(members)
    payload_sha = hashlib.sha256(payload).hexdigest()
    uv_args = [item for package in with_packages for item in ("--with", package)]
    runner = render_runner(
        _read_template(),
        payload_b64=chunk_base64(base64.b64encode(payload).decode("ascii")),
        payload_sha256=payload_sha,
        entry=entry_arc,
        uv_args=uv_args,
        file_hashes=[(hashlib.sha256(body).hexdigest(), name) for name, body in sorted(members.items())],
    )
    _write_runner(destination, runner)
    return PackedScript(
        output_path=str(destination),
        entry=entry_arc,
        files=sorted(members),
        payload_sha256=payload_sha,
        payload_bytes=len(payload),
        uv_args=uv_args,
        external_imports=external,
        has_script_metadata=bool(metadata),
    )


def unpack_script(source: str | Path, dest: str | Path, *, force: bool = False) -> PackedScript:
    """Restore the sources embedded in a packed ``.ps1`` into ``dest``.

    The generated ``__main__`` shim is machinery rather than source, so it is skipped: what
    lands in ``dest`` is exactly what was packed, ready to edit and pack again.

    Args:
        source: A ``.ps1`` produced by :func:`pack_script`.
        dest: Directory to write the sources into; created if absent.
        force: Overwrite files that already exist in ``dest``.

    Returns:
        A :class:`~pwshpy.domain.records.PackedScript` describing what was restored, with
        ``output_path`` set to ``dest``.

    Raises:
        PackError: If the file carries no recognisable payload, the payload is corrupt, an
            archive member would escape ``dest``, or a target exists without ``force``.

    Example:
        >>> import tempfile, pathlib
        >>> tmp = pathlib.Path(tempfile.mkdtemp())
        >>> _ = (tmp / "tool.py").write_text("print(1)\\n")
        >>> packed = pack_script(tmp / "tool.py")
        >>> unpack_script(packed.output_path, tmp / "out").files
        ['tool.py']
    """
    packed = _existing_file(Path(source), "packed script")
    text = _read_bytes(packed).decode("utf-8", "replace")
    payload = _extract_payload(text, packed)
    target = Path(dest).expanduser()
    written: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in sorted(archive.namelist()):
                if name == SHIM_MODULE:
                    continue
                _write_member(target, name, archive.read(name), force=force)
                written.append(name)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackError(f"the payload of {source} could not be unpacked: {exc}") from exc
    return PackedScript(
        output_path=str(target),
        entry=_extract_entry_name(text),
        files=written,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        payload_bytes=len(payload),
    )


def _collect_sources(entry: Path, root: Path, include: Iterable[str | Path]) -> tuple[list[Path], list[str]]:
    """Walk ``entry``'s local imports breadth-first; return the files and the external names.

    A dotted name counts as local when a file for it exists under ``root``.  Resolution is
    tried first and classification second, because a local file shadows a standard-library
    module of the same name at runtime and must therefore be packed rather than assumed.
    """
    stdlib = frozenset(sys.stdlib_module_names)
    found: dict[Path, None] = {entry: None}
    external: set[str] = set()
    pending = [entry]
    while pending:
        current = pending.pop()
        source = _read_bytes(current).decode("utf-8", "replace")
        package = _package_parts(current, root)
        for dotted in iter_import_candidates(source, module_package=package, filename=str(current)):
            resolved = _resolve_local(dotted, root)
            local_names: frozenset[str] = frozenset({dotted}) if resolved is not None else frozenset()
            kind = classify_import(dotted, stdlib_names=stdlib, local_names=local_names)
            if kind is ImportKind.EXTERNAL:
                external.add(dotted.split(".", 1)[0])
            elif kind is ImportKind.LOCAL and resolved is not None and resolved not in found:
                found[resolved] = None
                pending.append(resolved)
    for extra in include:
        found[_existing_file(Path(extra), "included file")] = None
    for path in found:
        _require_under_root(path, root)
    return sorted(found), sorted(external)


def _resolve_local(dotted: str, root: Path) -> Path | None:
    """Return the file a dotted name resolves to under ``root``, or ``None`` if there is none."""
    for relative in candidate_relative_paths(dotted):
        candidate = root / relative
        if candidate.is_file():
            return candidate.resolve()
    return None


def _package_parts(module: Path, root: Path) -> tuple[str, ...]:
    """Return the package parts a module file sits in, relative to ``root``.

    ``pkg/mod.py`` and ``pkg/__init__.py`` both anchor relative imports at ``("pkg",)``:
    a package's ``__init__`` resolves ``from . import x`` within itself, not its parent.
    """
    try:
        relative = module.resolve().relative_to(root)
    except ValueError:
        return ()
    return relative.parts[:-1]


def _archive_name(path: Path, root: Path) -> str:
    """Return the POSIX-style archive member name for ``path``."""
    return path.resolve().relative_to(root).as_posix()


def _require_under_root(path: Path, root: Path) -> None:
    """Fail unless ``path`` lives under ``root`` - an archive must have no ``..`` members."""
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise PackError(f"{path} lies outside the pack root {root}; widen it with root=") from exc


def _existing_file(path: Path, label: str) -> Path:
    """Resolve ``path``, failing with a typed error when it is missing or not a file."""
    resolved = path.expanduser()
    if not resolved.is_file():
        raise PackError(f"{label} not found: {path}")
    return resolved.resolve()


def _read_bytes(path: Path) -> bytes:
    """Read a file, mapping an I/O failure into the pwshpy error tree."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PackError(f"cannot read {path}: {exc}") from exc


def _write_runner(destination: Path, runner: str) -> None:
    """Write the artefact through the shared UTF-8-no-BOM writer, creating parent dirs.

    ``write_text`` is the project's one predictable-bytes seam, but it does not create
    directories, and a bare ``OSError`` escaping here would break the invariant that every
    pwshpy failure is a :class:`PwshPyError`.
    """
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_text(destination, runner)
    except OSError as exc:
        raise PackError(f"cannot write {destination}: {exc}") from exc


def _read_template() -> str:
    """Read the packaged runner template."""
    try:
        return _TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise PackError(f"the packed-runner template is missing from the installation: {exc}") from exc


def _guard_destination(destination: Path, sources: Iterable[Path], *, force: bool) -> None:
    """Refuse to overwrite an input, or an existing output without ``force``."""
    if destination.resolve() in {path.resolve() for path in sources}:
        raise PackError(f"the destination {destination} is one of the files being packed")
    if destination.exists() and not force:
        raise PackError(f"{destination} already exists; pass force to overwrite it")


def _build_archive(members: dict[str, bytes]) -> bytes:
    """Build a deterministic zip: sorted members, fixed timestamps and modes.

    Determinism is what makes the payload hash a usable cache key - two packs of identical
    sources must produce byte-identical output, which a real mtime would break.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(filename=name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = _ZIP_MODE
            archive.writestr(info, members[name])
    return buffer.getvalue()


def _extract_payload(text: str, source: Path) -> bytes:
    """Pull the base64 payload out of a generated runner and decode it."""
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == _PAYLOAD_OPEN)
        end = next(index for index in range(start + 1, len(lines)) if lines[index].strip() == _PAYLOAD_CLOSE)
    except StopIteration as exc:
        raise PackError(f"{source} does not look like a pwshpy pack (no payload block found)") from exc
    try:
        return base64.b64decode("".join(lines[start + 1 : end]), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PackError(f"the payload in {source} is not valid base64: {exc}") from exc


def _extract_entry_name(text: str) -> str:
    """Read the entry name a generated runner records, or ``""`` when absent."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_ENTRY_PREFIX) and stripped.endswith("'"):
            return stripped[len(_ENTRY_PREFIX) : -1]
    return ""


def _write_member(target: Path, name: str, body: bytes, *, force: bool) -> None:
    """Write one archive member under ``target``, refusing to let it escape."""
    destination = (target / name).resolve()
    try:
        destination.relative_to(target.resolve())
    except ValueError as exc:
        raise PackError(f"archive member {name!r} would be written outside {target}") from exc
    if destination.exists() and not force:
        raise PackError(f"{destination} already exists; pass force to overwrite it")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
    except OSError as exc:
        raise PackError(f"cannot write {destination}: {exc}") from exc


__all__ = ["pack_script", "unpack_script"]
