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
import tarfile
import zipfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from ...domain.enums import ImportKind, RunnerFormat
from ...domain.errors import PackError
from ...domain.packing import (
    DEFAULT_PACK_OPTIONS,
    SHIM_MODULE,
    FileDigest,
    PackOptions,
    RunnerContent,
    candidate_relative_paths,
    chunk_base64,
    classify_import,
    extract_script_metadata,
    iter_import_candidates,
    render_posix_runner,
    render_runner,
    render_shim,
)
from ...domain.records import PackedScript
from .fileio import write_text

#: The runner templates, shipped as package data next to this module (one per format).
_TEMPLATE_DIR = Path(__file__).with_name("templates")
_TEMPLATE_PATHS = {
    RunnerFormat.PS1: _TEMPLATE_DIR / "pack_runner.ps1",
    RunnerFormat.SH: _TEMPLATE_DIR / "pack_runner.sh",
}

#: Fixed archive timestamp.  Zip's epoch starts in 1980, and a real mtime would change the
#: payload hash between two packs of identical sources, defeating the runner's cache key.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: Permission bits recorded for every archive member (0o644 as a zip external attribute).
_ZIP_MODE = 0o644 << 16

#: The lines bracketing the base64 payload inside a generated PowerShell runner (a here-string).
_PAYLOAD_OPEN = "$PwshPyPayload = @'"
_PAYLOAD_CLOSE = "'@"

#: The heredoc delimiter bracketing the base64 payload inside a generated POSIX ``.sh`` runner.
#: The open line ends with ``<<'<delim>'``; the payload runs until a line equal to ``<delim>``.
_SH_HEREDOC = "__PWSHPY_PAYLOAD_B64__"

#: Prefixes of the line recording the entry name inside a generated runner (PowerShell / POSIX).
_ENTRY_PREFIX = "$PwshPyEntry = '"
_SH_ENTRY_PREFIX = "PWSHPY_ENTRY='"


def pack_script(
    entry: str | Path, dest: str | Path | None = None, *, options: PackOptions = DEFAULT_PACK_OPTIONS
) -> PackedScript:
    """Pack ``entry`` and the local modules it imports into a self-extracting ``.ps1``.

    Args:
        entry: The Python script to pack.
        dest: Output path; defaults to the entry's name with a ``.ps1`` suffix.
        options: How to pack - includes, extra ``--with`` distributions, the import root, and
            whether to overwrite the destination (see :class:`~pwshpy.domain.packing.PackOptions`).

    Returns:
        A :class:`~pwshpy.domain.records.PackedScript` manifest of what was embedded.

    Raises:
        PackError: If the entry is missing or unreadable, a file to embed lies outside the
            root, the destination exists without ``force``, or the destination would clobber
            one of the inputs.

    Example:
        >>> import tempfile, pathlib
        >>> tmp = pathlib.Path(tempfile.mkdtemp())
        >>> _ = (tmp / "tool.py").write_text("import os\\nprint(os.name)\\n")
        >>> manifest = pack_script(tmp / "tool.py")
        >>> manifest.entry, manifest.file_count, manifest.output_path.endswith("tool.ps1")
        ('tool.py', 2, True)
    """
    entry_path = _existing_file(Path(entry), "entry script")
    root_path = Path(options.root).expanduser().resolve() if options.root is not None else entry_path.parent
    fmt = _resolve_format(options.format, dest)
    default_suffix = ".sh" if fmt is RunnerFormat.SH else ".ps1"
    destination = Path(dest).expanduser() if dest is not None else entry_path.with_suffix(default_suffix)
    sources, external = _collect_sources(entry_path, root_path, options.include)
    _guard_destination(destination, sources, force=options.force)

    entry_arc = _archive_name(entry_path, root_path)
    members = {_archive_name(path, root_path): body for path, body in sources.items()}
    metadata = extract_script_metadata(sources[entry_path].decode("utf-8", "replace"))
    if fmt is RunnerFormat.PS1:
        # PowerShell 5.1 re-quotes native-command args, so the entry runs behind a generated
        # __main__ shim that rebuilds argv out of band; the shim (not the entry) is what uv runs,
        # so it carries the entry's PEP 723 block.
        if SHIM_MODULE in members:
            raise PackError(
                f"a file to pack is named {SHIM_MODULE!r}, which is reserved for the generated entry "
                "shim; rename it so the pack does not silently overwrite it"
            )
        members[SHIM_MODULE] = render_shim(entry=entry_arc, metadata=metadata).encode("utf-8")

    payload = _build_archive(members) if fmt is RunnerFormat.PS1 else _build_tar_archive(members)
    payload_sha = hashlib.sha256(payload).hexdigest()
    uv_args = [item for package in options.with_packages for item in ("--with", package)]
    content = RunnerContent(
        payload_b64=chunk_base64(base64.b64encode(payload).decode("ascii")),
        payload_sha256=payload_sha,
        entry=entry_arc,
        uv_args=uv_args,
        file_hashes=[FileDigest(hashlib.sha256(body).hexdigest(), name) for name, body in sorted(members.items())],
    )
    template = _read_template(fmt)
    # POSIX sh forwards "$@" intact and uv reads the entry's block directly, so the .sh runs the
    # entry itself - no shim, no out-of-band argv channel.
    runner = render_runner(template, content) if fmt is RunnerFormat.PS1 else render_posix_runner(template, content)
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
    payload, fmt = _extract_payload(text, packed)
    target = Path(dest).expanduser()
    extract = _extract_zip_members if fmt is RunnerFormat.PS1 else _extract_tar_members
    try:
        written = extract(payload, target, force=force)
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        raise PackError(f"the payload of {source} could not be unpacked: {exc}") from exc
    return PackedScript(
        output_path=str(target),
        entry=_extract_entry_name(text),
        files=written,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        payload_bytes=len(payload),
    )


def _collect_sources(entry: Path, root: Path, include: Iterable[str | Path]) -> tuple[dict[Path, bytes], list[str]]:
    """Walk ``entry``'s local imports breadth-first; return the files and the external names.

    A dotted name counts as local when a file for it exists under ``root``.  Resolution is
    tried first and classification second, because a local file shadows a standard-library
    module of the same name at runtime and must therefore be packed rather than assumed.
    """
    stdlib = frozenset(sys.stdlib_module_names)
    # Each file is read exactly once here and its bytes cached, so the caller reuses them for
    # the archive and the metadata rather than re-reading from disk two more times.
    found: dict[Path, bytes] = {}
    external: set[str] = set()
    pending = [entry]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        body = _read_bytes(current)
        found[current] = body
        package = _package_parts(current, root)
        for dotted in iter_import_candidates(
            body.decode("utf-8", "replace"), module_package=package, filename=str(current)
        ):
            resolved = _resolve_local(dotted, root)
            local_names: frozenset[str] = frozenset({dotted}) if resolved is not None else frozenset()
            kind = classify_import(dotted, stdlib_names=stdlib, local_names=local_names)
            if kind is ImportKind.EXTERNAL:
                external.add(dotted.split(".", 1)[0])
            elif kind is ImportKind.LOCAL and resolved is not None and resolved not in found:
                pending.append(resolved)
    for extra in include:
        path = _existing_file(Path(extra), "included file")
        if path not in found:
            found[path] = _read_bytes(path)
    for path in found:
        _require_under_root(path, root)
    # ``from pkg import thing`` yields the candidate ``pkg.thing``, which resolves to no file
    # when ``thing`` is a function or a constant. Its root would then look external even though
    # ``pkg`` was just packed, so drop any name whose root does resolve locally.
    external = {name for name in external if _resolve_local(name, root) is None}
    return found, sorted(external)


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


def _resolve_format(fmt: RunnerFormat, dest: str | Path | None) -> RunnerFormat:
    """Resolve ``AUTO`` to a concrete runner format, by the destination's extension.

    A ``.sh`` destination means the POSIX runner; anything else (including no destination) means
    the PowerShell runner.  An explicit non-``AUTO`` format is returned unchanged.
    """
    if fmt is not RunnerFormat.AUTO:
        return fmt
    if dest is not None and Path(dest).suffix.lower() == ".sh":
        return RunnerFormat.SH
    return RunnerFormat.PS1


def _read_template(fmt: RunnerFormat) -> str:
    """Read the packaged runner template for ``fmt``."""
    try:
        return _TEMPLATE_PATHS[fmt].read_text(encoding="utf-8")
    except OSError as exc:
        raise PackError(f"the packed-runner template is missing from the installation: {exc}") from exc


def _guard_destination(destination: Path, sources: Iterable[Path], *, force: bool) -> None:
    """Refuse to overwrite an input, or an existing output without ``force``."""
    if destination.resolve() in {path.resolve() for path in sources}:
        raise PackError(f"the destination {destination} is one of the files being packed")
    if destination.exists() and not force:
        raise PackError(f"{destination} already exists; pass force to overwrite it")


def _build_archive(members: Mapping[str, bytes]) -> bytes:
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


def _build_tar_archive(members: Mapping[str, bytes]) -> bytes:
    """Build a deterministic uncompressed tar for the POSIX runner (``tar`` is POSIX-standard).

    Uncompressed so the bytes are trivially deterministic (no gzip mtime); sorted members with a
    fixed mtime/mode and zeroed owner, so identical sources hash identically - the payload sha256
    is the runner's cache key, exactly as for the zip.  Not compressed matters little: the payload
    is base64-embedded and the sources are small scripts.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in sorted(members):
            body = members[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            info.mtime = 0
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _extract_payload(text: str, source: Path) -> tuple[bytes, RunnerFormat]:
    """Pull the base64 payload out of a generated runner; return its bytes and the runner format.

    A PowerShell runner brackets the payload in a here-string (``$PwshPyPayload = @'`` ...
    ``'@``); a POSIX runner brackets it in a heredoc (a line ending ``<<'__PWSHPY_PAYLOAD_B64__'``
    ... a line equal to ``__PWSHPY_PAYLOAD_B64__``).  The bracket that matches tells the format.
    """
    lines = text.splitlines()
    ps1 = _slice_payload(lines, lambda s: s == _PAYLOAD_OPEN, lambda s: s == _PAYLOAD_CLOSE)
    if ps1 is not None:
        return _decode_payload(ps1, source), RunnerFormat.PS1
    sh = _slice_payload(lines, lambda s: s.endswith(f"<<'{_SH_HEREDOC}'"), lambda s: s == _SH_HEREDOC)
    if sh is not None:
        return _decode_payload(sh, source), RunnerFormat.SH
    raise PackError(f"{source} does not look like a pwshpy pack (no payload block found)")


def _slice_payload(
    lines: list[str], is_open: Callable[[str], bool], is_close: Callable[[str], bool]
) -> list[str] | None:
    """Return the lines strictly between the first matching open and close markers, or ``None``."""
    try:
        start = next(index for index, line in enumerate(lines) if is_open(line.strip()))
        end = next(index for index in range(start + 1, len(lines)) if is_close(lines[index].strip()))
    except StopIteration:
        return None
    return lines[start + 1 : end]


def _decode_payload(payload_lines: list[str], source: Path) -> bytes:
    """Decode the base64 lines of a payload block."""
    try:
        return base64.b64decode("".join(line.strip() for line in payload_lines), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PackError(f"the payload in {source} is not valid base64: {exc}") from exc


def _extract_entry_name(text: str) -> str:
    """Read the entry name a generated runner records, or ``""`` when absent (either format)."""
    for line in text.splitlines():
        stripped = line.strip()
        for prefix in (_ENTRY_PREFIX, _SH_ENTRY_PREFIX):
            if stripped.startswith(prefix) and stripped.endswith("'"):
                return stripped[len(prefix) : -1]
    return ""


def _extract_zip_members(payload: bytes, target: Path, *, force: bool) -> list[str]:
    """Restore a zip payload (PowerShell format) under ``target``, skipping the generated shim."""
    written: list[str] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in sorted(archive.namelist()):
            if name == SHIM_MODULE:
                continue
            _write_member(target, name, archive.read(name), force=force)
            written.append(name)
    return written


def _extract_tar_members(payload: bytes, target: Path, *, force: bool) -> list[str]:
    """Restore a tar payload (POSIX format) under ``target``.

    Each regular file is read and written through :func:`_write_member`, whose ``relative_to``
    guard rejects any member that would escape ``target`` (tar-slip); directory/link members are
    skipped since the packer only ever writes regular files.  The ``.sh`` format has no shim, but
    the name is skipped defensively.
    """
    written: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as tar:
        for member in sorted(tar.getmembers(), key=lambda m: m.name):
            if not member.isfile() or member.name == SHIM_MODULE:
                continue
            extracted = tar.extractfile(member)
            body = extracted.read() if extracted is not None else b""
            _write_member(target, member.name, body, force=force)
            written.append(member.name)
    return written


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
