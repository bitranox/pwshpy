"""Scheduled-tasks native adapter over the Task Scheduler COM API.

A fake ``Schedule.Service`` (injected at the adapter's connect seam) exercises the
folder recursion, marshaling, and the streaming/laziness contract on every OS; a
real structural test walks the live task tree on Windows.  Exact live behaviour is
pinned against Get-ScheduledTask in ``test_scheduled_tasks_pwsh_oracle``.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from pwshpy.adapters.native import scheduled_tasks as st_mod
from pwshpy.adapters.native.scheduled_tasks import iter_scheduled_tasks
from pwshpy.domain.enums import TaskState
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import ScheduledTaskInfo


class _FakeReg:
    def __init__(self, author: str | None, description: str | None) -> None:
        self.Author = author
        self.Description = description


class _FakeDef:
    def __init__(self, registration: _FakeReg) -> None:
        self.RegistrationInfo = registration


class _FakeTask:
    def __init__(self, name: str, state: int, enabled: bool, author: str = "", description: str = "") -> None:
        self.Name = name
        self.State = state
        self.Enabled = enabled
        self.Definition = _FakeDef(_FakeReg(author, description))


class _FakeCollection:
    """A 1-indexed COM-style collection (``.Count`` / ``.Item(i)``)."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    @property
    def Count(self) -> int:  # noqa: N802 - COM API name
        return len(self._items)

    def Item(self, index: int) -> Any:  # noqa: N802 - COM API name
        return self._items[index - 1]


class _FakeFolder:
    def __init__(self, path: str, tasks: list[Any], subfolders: list[Any]) -> None:
        self.Path = path
        self._tasks = tasks
        self._subfolders = subfolders

    def GetTasks(self, flag: int) -> _FakeCollection:  # noqa: N802 - COM API name
        return _FakeCollection(self._tasks)

    def GetFolders(self, flag: int) -> _FakeCollection:  # noqa: N802 - COM API name
        return _FakeCollection(self._subfolders)


class _FakeService:
    def __init__(self, root: _FakeFolder) -> None:
        self._root = root

    def Connect(self) -> None:  # noqa: N802 - COM API name
        return None

    def GetFolder(self, path: str) -> _FakeFolder:  # noqa: N802 - COM API name
        return self._root


@pytest.mark.os_agnostic
def test_iter_scheduled_tasks_recurses_and_marshals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tasks are collected from nested folders and marshaled, folder path first."""
    sub = _FakeFolder("\\Sub", [_FakeTask("Alpha", 3, True, "me", "d"), _FakeTask("Beta", 1, False)], [])
    root = _FakeFolder("\\", [], [sub])
    monkeypatch.setattr(st_mod, "_connect_scheduler", lambda: _FakeService(root))

    items = list(iter_scheduled_tasks())
    assert [t.task_name for t in items] == ["Alpha", "Beta"]

    alpha = items[0]
    assert alpha.task_path == "\\Sub\\"
    assert alpha.state is TaskState.READY
    assert alpha.enabled is True
    assert alpha.author == "me"
    assert alpha.description == "d"

    beta = items[1]
    assert beta.state is TaskState.DISABLED
    assert beta.enabled is False


@pytest.mark.os_agnostic
def test_iter_scheduled_tasks_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pulling one task never descends into a later folder - the walk is lazy/streaming."""

    class _CountingFolder:
        def __init__(self) -> None:
            self.Path = "\\Later"
            self.tasks_requested = False

        def GetTasks(self, flag: int) -> _FakeCollection:  # noqa: N802 - COM API name
            self.tasks_requested = True
            return _FakeCollection([_FakeTask("Z", 3, True)])

        def GetFolders(self, flag: int) -> _FakeCollection:  # noqa: N802 - COM API name
            return _FakeCollection([])

    good = _FakeFolder("\\Good", [_FakeTask("A", 3, True)], [])
    later = _CountingFolder()
    root = _FakeFolder("\\", [], [good, later])
    monkeypatch.setattr(st_mod, "_connect_scheduler", lambda: _FakeService(root))

    first = next(iter_scheduled_tasks())
    assert first.task_name == "A"
    assert later.tasks_requested is False  # the later folder was not descended into


@pytest.mark.os_agnostic
def test_iter_scheduled_tasks_wraps_folder_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GetFolder failure (bad path / access denied) surfaces as NativeCallError."""

    class _BadService:
        def Connect(self) -> None:  # noqa: N802 - COM API name
            return None

        def GetFolder(self, path: str) -> Any:  # noqa: N802 - COM API name
            raise OSError("no such folder")

    monkeypatch.setattr(st_mod, "_connect_scheduler", _BadService)
    with pytest.raises(NativeCallError):
        list(iter_scheduled_tasks("\\Nope"))


@pytest.mark.os_agnostic
def test_iter_scheduled_tasks_skips_inaccessible_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task whose definition can't be read is skipped, not fatal (like Get-ScheduledTask)."""

    class _BadTask:
        Name = "Bad"
        State = 3
        Enabled = True

        @property
        def Definition(self) -> Any:  # noqa: N802 - COM API name
            raise OSError("access denied")

    folder = _FakeFolder("\\F", [_BadTask(), _FakeTask("Good", 3, True)], [])
    root = _FakeFolder("\\", [], [folder])
    monkeypatch.setattr(st_mod, "_connect_scheduler", lambda: _FakeService(root))
    assert [t.task_name for t in iter_scheduled_tasks()] == ["Good"]


@pytest.mark.os_agnostic
def test_task_none_author_and_description_coalesce(monkeypatch: pytest.MonkeyPatch) -> None:
    """COM returning None for an unset Author/Description coalesces to an empty string."""
    task = _FakeTask("T", 3, True)
    task.Definition = _FakeDef(_FakeReg(None, None))
    folder = _FakeFolder("\\F", [task], [])
    root = _FakeFolder("\\", [], [folder])
    monkeypatch.setattr(st_mod, "_connect_scheduler", lambda: _FakeService(root))
    info = next(iter_scheduled_tasks())
    assert info.author == ""
    assert info.description == ""


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler COM is Windows-only")
def test_iter_scheduled_tasks_reads_real_tasks() -> None:
    """Against the real Task Scheduler, the adapter streams typed records."""
    tasks = [task for _, task in zip(range(5), iter_scheduled_tasks(), strict=False)]
    assert tasks
    assert all(isinstance(t, ScheduledTaskInfo) for t in tasks)
    assert all(t.task_path.endswith("\\") for t in tasks)
