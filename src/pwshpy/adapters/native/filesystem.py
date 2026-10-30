"""native filesystem - typed file/directory operations over ``pathlib`` + ``shutil``.

Native, portable equivalents of the PowerShell filesystem provider cmdlets, returning
typed :class:`~pwshpy.domain.records.FileSystemItem` records instead of the
``PSCustomObject`` grab-bag ``Get-ChildItem`` yields.  Read (``get_child_item`` /
``get_item`` / ``get_content`` / ``test_path``) and mutate (``new_item`` / ``copy_item``
/ ``move_item`` / ``remove_item``); every fallible call maps to
:class:`~pwshpy.domain.errors.NativeCallError`: an ``OSError``, an unknown encoding
(``LookupError``) and text that is not valid in the encoding (``UnicodeDecodeError``).
A ``ValueError`` that is a usage error (a NUL in a path) stays a ``ValueError``.

Contents:
    * :class:`NativeFileSystem` - the filesystem operations (read + mutating).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from ...domain.enums import FileItemType
from ...domain.errors import NativeAccessDeniedError, NativeCallError
from ...domain.records import FileSystemItem
from .marshal import native_error

logger = logging.getLogger(__name__)


def _exists(target: Path) -> bool:
    """Whether ``target`` exists; a refusal raises, typed, on every Python.

    Not ``Path.exists()``: on 3.10-3.13 it lets a raw ``PermissionError`` out, and on 3.14 it
    answers False for it, so a denied path would read as missing.
    """
    try:
        target.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False  # ENOTDIR: a component is a file, so the path can never exist
    except ValueError:
        return False  # a NUL in the path: Path.exists() answered False here, keep that answer
    except OSError as exc:
        raise native_error(f"cannot read {str(target)!r}: {exc}", exc) from exc
    return True


def _to_item(entry: Path) -> FileSystemItem:
    """Marshal a path into a typed FileSystemItem (size ``None`` for a directory)."""
    try:
        stat = entry.stat()
        is_dir = entry.is_dir()
    except OSError as exc:
        raise native_error(f"cannot stat {str(entry)!r}: {exc}", exc) from exc
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
            raise native_error(f"cannot list {path!r}: {exc}", exc) from exc
        try:
            for entry in entries:
                yield _to_item(entry)
        except NativeCallError:
            raise  # _to_item already typed it; a denied stat is a PermissionError too, never re-wrap
        except OSError as exc:
            raise native_error(f"error while listing {path!r}: {exc}", exc) from exc

    def get_item(self, path: str) -> FileSystemItem:
        """Return one file/directory as a record (like Get-Item)."""
        target = Path(path)
        if not _exists(target):
            raise NativeCallError(f"path not found: {path!r}")
        return _to_item(target)

    def get_content(self, path: str, *, encoding: str = "utf-8") -> str:
        """Return a file's whole text content (like ``Get-Content -Raw``).

        Reads the entire file into memory - fine for config-sized files; for a large or
        unbounded file use :meth:`get_content_lines`, which streams.
        """
        try:
            return Path(path).read_text(encoding=encoding)
        # UnicodeDecodeError is a ValueError, named here so a NUL-in-path ValueError stays raw.
        except (OSError, LookupError, UnicodeDecodeError) as exc:
            raise native_error(f"cannot read {path!r}: {exc}", exc) from exc

    def get_content_lines(self, path: str, *, encoding: str = "utf-8") -> Iterator[str]:
        """Yield a file's lines lazily, **memory-bounded** (like ``Get-Content`` line-by-line).

        The file is opened eagerly (so a missing/unreadable path raises straight away) but
        read one line at a time, so even a multi-gigabyte file stays flat in memory.
        """
        try:
            # Open eagerly so a bad path raises here, not on first iteration; the generator
            # below closes it via `with handle` (SIM115's context-manager requirement is met there).
            handle = Path(path).open(encoding=encoding)  # noqa: SIM115
        except (OSError, LookupError) as exc:
            raise native_error(f"cannot read {path!r}: {exc}", exc) from exc

        def _lines() -> Iterator[str]:
            with handle:
                try:
                    yield from handle
                except (OSError, UnicodeDecodeError) as exc:
                    raise native_error(f"error while reading {path!r}: {exc}", exc) from exc

        return _lines()

    def test_path(self, path: str) -> bool:
        """Return whether ``path`` exists (like Test-Path).

        A path the caller may not read answers False, as Test-Path does, and logs one WARNING
        naming the path, where Test-Path writes a non-terminating error. A path that plainly does
        not exist answers False without a warning.
        """
        try:
            return _exists(Path(path))
        except NativeAccessDeniedError as exc:
            logger.warning("test_path: access to %r is denied, reporting it absent: %s", path, exc)
        except NativeCallError as exc:
            logger.warning("test_path: cannot check %r, reporting it absent: %s", path, exc)
        return False

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
            raise native_error(f"cannot create {path!r}: {exc}", exc) from exc
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
            raise native_error(f"cannot copy {source!r} -> {destination!r}: {exc}", exc) from exc

    def move_item(self, source: str, destination: str) -> None:
        """Move/rename a file or directory (like Move-Item) (**mutating**)."""
        try:
            shutil.move(source, destination)
        except OSError as exc:
            raise native_error(f"cannot move {source!r} -> {destination!r}: {exc}", exc) from exc

    def remove_item(self, path: str, *, recurse: bool = False) -> None:
        """Delete a file, or a directory (``recurse`` removes its contents too) (**mutating**)."""
        target = Path(path)
        try:
            if target.is_dir():
                shutil.rmtree(target) if recurse else target.rmdir()
            else:
                target.unlink()
        except OSError as exc:
            raise native_error(f"cannot remove {path!r}: {exc}", exc) from exc


__all__ = ["NativeFileSystem"]
