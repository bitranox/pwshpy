"""native filesystem: os_agnostic, hermetic tests over a tmp path.

Every case drives a real temp directory, so the read + mutating operations are
verified on every OS without mocking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pwshpy.adapters.native.filesystem import NativeFileSystem
from pwshpy.domain.enums import FileItemType
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import FileSystemItem


@pytest.mark.os_agnostic
def test_get_child_item_lists_entries(tmp_path: Path) -> None:
    """get_child_item yields a typed record per entry; recurse walks the tree."""
    (tmp_path / "a.txt").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("yy")
    fs = NativeFileSystem()

    flat = {i.name: i for i in fs.get_child_item(str(tmp_path))}
    assert set(flat) == {"a.txt", "sub"}
    assert flat["a.txt"].is_directory is False
    assert flat["a.txt"].size == 1
    assert flat["sub"].is_directory is True
    assert flat["sub"].size is None

    names = {i.name for i in fs.get_child_item(str(tmp_path), recurse=True)}
    assert "b.txt" in names


@pytest.mark.os_agnostic
def test_get_item_and_content(tmp_path: Path) -> None:
    """get_item returns a record; get_content returns the file text."""
    target = tmp_path / "hello.txt"
    target.write_text("grüße", encoding="utf-8")
    fs = NativeFileSystem()
    item = fs.get_item(str(target))
    assert isinstance(item, FileSystemItem)
    assert item.name == "hello.txt"
    assert fs.get_content(str(target)) == "grüße"


@pytest.mark.os_agnostic
def test_get_content_lines_streams_lazily(tmp_path: Path) -> None:
    """get_content_lines yields lines one at a time, so .take(N) reads only the first lines."""
    target = tmp_path / "big.txt"
    target.write_text("l1\nl2\nl3\n", encoding="utf-8")
    fs = NativeFileSystem()
    assert list(fs.get_content_lines(str(target))) == ["l1\n", "l2\n", "l3\n"]
    # partial consumption does not read (or need) the rest
    first = next(iter(fs.get_content_lines(str(target))))
    assert first == "l1\n"


@pytest.mark.os_agnostic
def test_get_content_lines_missing_raises(tmp_path: Path) -> None:
    """A missing file raises NativeCallError eagerly (at open, not first iteration)."""
    with pytest.raises(NativeCallError):
        NativeFileSystem().get_content_lines(str(tmp_path / "nope"))


@pytest.mark.os_agnostic
def test_test_path(tmp_path: Path) -> None:
    """test_path reports existence."""
    fs = NativeFileSystem()
    assert fs.test_path(str(tmp_path)) is True
    assert fs.test_path(str(tmp_path / "nope")) is False


@pytest.mark.os_agnostic
def test_new_item_file_and_directory(tmp_path: Path) -> None:
    """new_item creates a file or a directory and returns the record."""
    fs = NativeFileSystem()
    f = fs.new_item(str(tmp_path / "deep" / "f.txt"))
    assert (tmp_path / "deep" / "f.txt").is_file()
    assert f.is_directory is False
    d = fs.new_item(str(tmp_path / "d"), item_type=FileItemType.DIRECTORY)
    assert (tmp_path / "d").is_dir()
    assert d.is_directory is True


@pytest.mark.os_agnostic
def test_copy_move_remove(tmp_path: Path) -> None:
    """copy_item, move_item, remove_item work for files and (recursively) directories."""
    fs = NativeFileSystem()
    src = tmp_path / "src.txt"
    src.write_text("data")
    fs.copy_item(str(src), str(tmp_path / "copy.txt"))
    assert (tmp_path / "copy.txt").read_text() == "data"

    fs.move_item(str(tmp_path / "copy.txt"), str(tmp_path / "moved.txt"))
    assert not (tmp_path / "copy.txt").exists()
    assert (tmp_path / "moved.txt").exists()

    tree = tmp_path / "tree"
    (tree / "inner").mkdir(parents=True)
    (tree / "inner" / "x").write_text("x")
    fs.copy_item(str(tree), str(tmp_path / "tree_copy"), recurse=True)
    assert (tmp_path / "tree_copy" / "inner" / "x").exists()

    fs.remove_item(str(tmp_path / "moved.txt"))
    assert not (tmp_path / "moved.txt").exists()
    fs.remove_item(str(tree), recurse=True)
    assert not tree.exists()


@pytest.mark.os_agnostic
def test_errors_wrap_in_nativecallerror(tmp_path: Path) -> None:
    """Missing paths raise NativeCallError, not a bare OSError."""
    fs = NativeFileSystem()
    with pytest.raises(NativeCallError):
        fs.get_item(str(tmp_path / "missing"))
    with pytest.raises(NativeCallError):
        fs.get_content(str(tmp_path / "missing"))
    with pytest.raises(NativeCallError):
        fs.remove_item(str(tmp_path / "missing"))
