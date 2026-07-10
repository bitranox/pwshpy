"""native filesystem - typed file/directory operations over ``pathlib`` + ``shutil``.

Native, portable equivalents of the PowerShell filesystem provider cmdlets, returning
typed :class:`~pwshpy.domain.records.FileSystemItem` records instead of the
``PSCustomObject`` grab-bag ``Get-ChildItem`` yields.  Read (``get_child_item`` /
``get_item`` / ``get_content`` / ``test_path``) and mutate (``new_item`` / ``copy_item``
/ ``move_item`` / ``remove_item``); every fallible call maps to
:class:`~pwshpy.domain.errors.NativeCallError`.

Contents:
    * :class:`NativeFileSystem` - the filesystem operations (read + mutating).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from ...domain.enums import FileItemType
from ...domain.errors import NativeCallError
from ...domain.records import FileSystemItem


def _to_item(entry: Path) -> FileSystemItem:
    """Marshal a path into a typed FileSystemItem (size ``None`` for a directory)."""
    try:
        stat = entry.stat()
        is_dir = entry.is_dir()
    except OSError as exc:
        raise NativeCallError(f"cannot stat {str(entry)!r}: {exc}") from exc
    return FileSystemItem(
        path=str(entry),
        name=entry.name,
        is_directory=is_dir,
        size=None if is_dir else stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


class NativeFileSystem:
    """File/directory operations as typed records (native, portable).

    Example:
        >>> fs = NativeFileSystem()
        >>> callable(fs.get_child_item)
        True
    """

    def get_child_item(self, path: str, *, recurse: bool = False) -> Iterator[FileSystemItem]:
        """List a directory's entries as records (like Get-ChildItem); ``recurse`` walks the tree."""
        root = Path(path)
        try:
            entries = root.rglob("*") if recurse else root.iterdir()
        except OSError as exc:
            raise NativeCallError(f"cannot list {path!r}: {exc}") from exc
        try:
            for entry in entries:
                yield _to_item(entry)
        except OSError as exc:
            raise NativeCallError(f"error while listing {path!r}: {exc}") from exc

    def get_item(self, path: str) -> FileSystemItem:
        """Return one file/directory as a record (like Get-Item)."""
        target = Path(path)
        if not target.exists():
            raise NativeCallError(f"path not found: {path!r}")
        return _to_item(target)

    def get_content(self, path: str, *, encoding: str = "utf-8") -> str:
        """Return a file's whole text content (like ``Get-Content -Raw``).

        Reads the entire file into memory - fine for config-sized files; for a large or
        unbounded file use :meth:`get_content_lines`, which streams.
        """
        try:
            return Path(path).read_text(encoding=encoding)
        except OSError as exc:
            raise NativeCallError(f"cannot read {path!r}: {exc}") from exc

    def get_content_lines(self, path: str, *, encoding: str = "utf-8") -> Iterator[str]:
        """Yield a file's lines lazily, **memory-bounded** (like ``Get-Content`` line-by-line).

        The file is opened eagerly (so a missing/unreadable path raises straight away) but
        read one line at a time, so even a multi-gigabyte file stays flat in memory.
        """
        try:
            # Open eagerly so a bad path raises here, not on first iteration; the generator
            # below closes it via `with handle` (SIM115's context-manager requirement is met there).
            handle = Path(path).open(encoding=encoding)  # noqa: SIM115
        except OSError as exc:
            raise NativeCallError(f"cannot read {path!r}: {exc}") from exc

        def _lines() -> Iterator[str]:
            with handle:
                try:
                    yield from handle
                except OSError as exc:
                    raise NativeCallError(f"error while reading {path!r}: {exc}") from exc

        return _lines()

    def test_path(self, path: str) -> bool:
        """Return whether ``path`` exists (like Test-Path)."""
        return Path(path).exists()

    def new_item(self, path: str, *, item_type: FileItemType = FileItemType.FILE) -> FileSystemItem:
        """Create a file or directory (per ``item_type``); return it (like New-Item) (**mutating**)."""
        target = Path(path)
        try:
            if item_type is FileItemType.DIRECTORY:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
        except OSError as exc:
            raise NativeCallError(f"cannot create {path!r}: {exc}") from exc
        return _to_item(target)

    def copy_item(self, source: str, destination: str, *, recurse: bool = False) -> None:
        """Copy a file, or a directory tree when ``recurse`` (like Copy-Item) (**mutating**)."""
        src = Path(source)
        try:
            if src.is_dir() and recurse:
                shutil.copytree(src, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(src, destination)
        except OSError as exc:
            raise NativeCallError(f"cannot copy {source!r} -> {destination!r}: {exc}") from exc

    def move_item(self, source: str, destination: str) -> None:
        """Move/rename a file or directory (like Move-Item) (**mutating**)."""
        try:
            shutil.move(source, destination)
        except OSError as exc:
            raise NativeCallError(f"cannot move {source!r} -> {destination!r}: {exc}") from exc

    def remove_item(self, path: str, *, recurse: bool = False) -> None:
        """Delete a file, or a directory (``recurse`` removes its contents too) (**mutating**)."""
        target = Path(path)
        try:
            if target.is_dir():
                shutil.rmtree(target) if recurse else target.rmdir()
            else:
                target.unlink()
        except OSError as exc:
            raise NativeCallError(f"cannot remove {path!r}: {exc}") from exc


__all__ = ["NativeFileSystem"]
