"""native filesystem: os_agnostic, hermetic tests over a tmp path.

Every case drives a real temp directory, so the read + mutating operations are
verified on every OS without mocking.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pwshpy.adapters.native.filesystem import NativeFileSystem
from pwshpy.domain.enums import FileItemType
from pwshpy.domain.errors import NativeAccessDeniedError, NativeCallError
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


#: Guarded by ``sys.platform`` because ``os.geteuid`` does not exist on Windows.
_POSIX_NON_ROOT = sys.platform != "win32" and os.geteuid() != 0


@pytest.mark.os_posix
@pytest.mark.skipif(not _POSIX_NON_ROOT, reason="needs POSIX search permission enforced (non-root)")
def test_a_denied_entry_stat_is_raised_as_is_not_as_a_listing_error(tmp_path: Path) -> None:
    """A directory readable but not searchable: listing names works, stat-ing an entry is EACCES.

    The entry's own typed error ("cannot stat ...") must reach the caller unchanged, not be caught
    again by the listing's OSError clause because a denied error is a PermissionError too.
    """
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "a.txt").write_text("x", encoding="utf-8")
    locked.chmod(0o600)
    try:
        with pytest.raises(NativeAccessDeniedError, match=r"^cannot stat ") as caught:
            list(NativeFileSystem().get_child_item(str(locked)))
    finally:
        locked.chmod(0o700)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert not isinstance(caught.value.__cause__, NativeCallError)


def _locked_child(tmp_path: Path) -> tuple[Path, Path]:
    """A directory at mode 000 holding ``x``: the caller cannot search it, so ``x`` is unreadable."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x").write_text("x", encoding="utf-8")
    locked.chmod(0o000)
    return locked, locked / "x"


@pytest.mark.os_posix
@pytest.mark.skipif(not _POSIX_NON_ROOT, reason="needs POSIX search permission enforced (non-root)")
def test_get_item_below_an_unsearchable_directory_is_denied_not_missing(tmp_path: Path) -> None:
    """Get-Item on a denied path is an access error (UnauthorizedAccessException), not PathNotFound."""
    locked, child = _locked_child(tmp_path)
    try:
        with pytest.raises(NativeAccessDeniedError, match=r"^cannot read ") as caught:
            NativeFileSystem().get_item(str(child))
    finally:
        locked.chmod(0o700)
    assert isinstance(caught.value.__cause__, PermissionError)


@pytest.mark.os_posix
@pytest.mark.skipif(not _POSIX_NON_ROOT, reason="needs POSIX search permission enforced (non-root)")
def test_test_path_below_an_unsearchable_directory_is_false_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test-Path on a denied path returns False and reports the denial (pwsh: a non-terminating error)."""
    locked, child = _locked_child(tmp_path)
    try:
        with caplog.at_level("WARNING", logger="pwshpy.adapters.native.filesystem"):
            answer = NativeFileSystem().test_path(str(child))
    finally:
        locked.chmod(0o700)
    assert answer is False
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert str(child) in warnings[0].getMessage()
    assert "denied" in warnings[0].getMessage()


@pytest.mark.os_agnostic
def test_test_path_on_a_missing_path_is_false_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A path that does not exist, or lies below a file, is plainly absent: no warning."""
    (tmp_path / "file").write_text("x", encoding="utf-8")
    fs = NativeFileSystem()
    with caplog.at_level("WARNING", logger="pwshpy.adapters.native.filesystem"):
        assert fs.test_path(str(tmp_path / "nope")) is False
        assert fs.test_path(str(tmp_path / "file" / "below")) is False
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


@pytest.mark.os_agnostic
def test_get_item_below_a_file_is_path_not_found(tmp_path: Path) -> None:
    """A component that is a file means the path can never exist: "path not found", not an access error."""
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(NativeCallError, match=r"^path not found: ") as caught:
        NativeFileSystem().get_item(str(tmp_path / "file" / "below"))
    assert not isinstance(caught.value, NativeAccessDeniedError)


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="a symlink loop needs POSIX symlinks")
def test_a_symlink_loop_is_absent_without_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """ELOOP is in pathlib's not-found set: get_item says "path not found", test_path False, no warning."""
    loop = tmp_path / "a"
    loop.symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(loop)
    fs = NativeFileSystem()
    with caplog.at_level("WARNING", logger="pwshpy.adapters.native.filesystem"):
        assert fs.test_path(str(loop)) is False
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    with pytest.raises(NativeCallError, match=r"^path not found: ") as caught:
        fs.get_item(str(loop))
    assert not isinstance(caught.value, NativeAccessDeniedError)


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="ERROR_INVALID_NAME is a Windows error")
def test_a_wildcard_path_on_windows_is_absent_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A wildcard fails stat with winerror 123 (ERROR_INVALID_NAME), in pathlib's not-found set: no warning.

    The directory must exist: below a missing one Windows answers winerror 3 (ENOENT) first.
    """
    wildcard = str(tmp_path / "*.log")
    fs = NativeFileSystem()
    with caplog.at_level("WARNING", logger="pwshpy.adapters.native.filesystem"):
        assert fs.test_path(wildcard) is False
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    with pytest.raises(NativeCallError, match=r"^path not found: ") as caught:
        fs.get_item(wildcard)
    assert not isinstance(caught.value, NativeAccessDeniedError)


@pytest.mark.os_agnostic
def test_a_nul_in_the_path_keeps_its_answer(tmp_path: Path) -> None:
    """A NUL keeps the answer it had: test_path says False, get_item says "path not found"."""
    fs = NativeFileSystem()
    bad = str(tmp_path / "a\x00b")
    assert fs.test_path(bad) is False
    with pytest.raises(NativeCallError, match=r"^path not found: "):
        fs.get_item(bad)
