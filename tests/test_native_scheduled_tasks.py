"""Scheduled-tasks native adapter over the Task Scheduler COM API.

A fake ``Schedule.Service`` (injected at the adapter's connect seam) exercises the
folder recursion, marshaling, and the streaming/laziness contract on every OS; a
real structural test walks the live task tree on Windows.  Exact live behaviour is
pinned against Get-ScheduledTask in ``test_scheduled_tasks_pwsh_oracle``.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any

import pytest

from pwshpy.adapters.native import scheduled_tasks as st_mod
from pwshpy.adapters.native import task_readback
from pwshpy.adapters.native.scheduled_tasks import iter_scheduled_tasks
from pwshpy.domain.enums import TaskRunLevel, TaskState
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import ScheduledTaskInfo


class _FakeReg:
    def __init__(self, author: str | None, description: str | None) -> None:
        self.Author = author
        self.Description = description


class _FakePrincipal:
    def __init__(self, user_id: str = "", run_level: int = 0) -> None:
        self.UserId = user_id
        self.LogonType = 3
        self.RunLevel = run_level


class _FakeDef:
    def __init__(self, registration: _FakeReg, principal: _FakePrincipal | None = None) -> None:
        self.RegistrationInfo = registration
        self.Principal = principal or _FakePrincipal()


class _FakeTask:
    def __init__(self, name: str, state: int, enabled: bool, author: str = "", description: str = "") -> None:
        self.Name = name
        self.State = state
        self.Enabled = enabled
        self.Definition = _FakeDef(_FakeReg(author, description))
        self.LastRunTime = datetime(1999, 11, 30, tzinfo=timezone.utc)  # never ran (measured; pywin32's UTC label)
        self.LastTaskResult = 0x41303  # SCHED_S_TASK_HAS_NOT_RUN
        self.NextRunTime = datetime(1899, 12, 30, tzinfo=timezone.utc)  # no next run: the VT_DATE zero


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


def _sid_as_given(value: str) -> str:
    """Stand-in for sid_string (an external edge: LookupAccountName) that returns its input."""
    return value


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
    with pytest.raises(NativeCallError) as caught:
        list(iter_scheduled_tasks("\\Nope"))
    # Named like every other unreadable item: a raw COM error tuple alone does not say which folder.
    assert "'\\Nope'" in str(caught.value)
    assert "no such folder" in str(caught.value)


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


@pytest.mark.os_agnostic
def test_listing_reads_principal_run_level_and_run_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing carries the principal SID, run level and [120] run info, like the controller."""
    ran = _FakeTask("Ran", 3, True)
    ran.Definition = _FakeDef(_FakeReg("", ""), _FakePrincipal("S-1-5-18", 1))
    ran.LastRunTime = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)  # local wall time, pywin32's UTC label
    ran.LastTaskResult = -2147216609  # a negative HRESULT as pywin32 can return it
    ran.NextRunTime = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)
    never = _FakeTask("Never", 3, True)
    root = _FakeFolder("\\", [ran, never], [])
    monkeypatch.setattr(st_mod, "_connect_scheduler", lambda: _FakeService(root))
    monkeypatch.setattr(task_readback, "sid_string", _sid_as_given)  # external edge: LookupAccountName
    first, second = list(iter_scheduled_tasks())
    assert (first.user_id, first.run_level) == ("S-1-5-18", TaskRunLevel.HIGHEST)
    assert first.last_run_time == datetime(2026, 10, 21, 12, 0).astimezone(timezone.utc)  # read as local
    assert first.last_task_result == 0x8004131F  # read unsigned, like Get-ScheduledTaskInfo
    assert first.next_run_time == datetime(2026, 10, 22, 3, 0).astimezone(timezone.utc)
    assert (second.user_id, second.run_level) == ("", TaskRunLevel.LIMITED)
    assert (second.last_run_time, second.last_task_result, second.next_run_time) == (None, None, None)


# An account name that resolves nowhere: off Windows sid_string cannot resolve any name
# (no pywin32), on Windows LookupAccountName finds no such account - both raise.
_UNRESOLVABLE = "pwshpy-test-no-such-account-7f3a"


@pytest.mark.os_agnostic
def test_listing_keeps_a_task_whose_principal_cannot_be_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable principal (deleted account) reads user_id "" - the task is never dropped."""
    orphan = _FakeTask("Orphan", 3, True)
    orphan.Definition = _FakeDef(_FakeReg("", ""), _FakePrincipal(_UNRESOLVABLE, 1))
    orphan.LastTaskResult = 0
    root = _FakeFolder("\\", [orphan], [])
    monkeypatch.setattr(st_mod, "_connect_scheduler", lambda: _FakeService(root))
    (info,) = list(iter_scheduled_tasks())
    assert info.task_name == "Orphan"
    assert info.user_id == ""
    assert info.run_level is TaskRunLevel.HIGHEST  # the other fields still read back
    assert info.last_task_result == 0


@pytest.mark.os_agnostic
def test_listing_keeps_a_task_whose_run_info_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run-info property COM refuses reads None for that field only; the task stays listed."""

    class _HalfReadableTask(_FakeTask):
        @property
        def LastRunTime(self) -> Any:  # noqa: N802 - COM API name
            raise OSError("access denied")

        @LastRunTime.setter
        def LastRunTime(self, value: Any) -> None:  # noqa: N802 - COM API name
            del value  # the base __init__ assigns it; this task refuses to read it back

    task = _HalfReadableTask("Half", 3, True)
    task.LastTaskResult = 1
    root = _FakeFolder("\\", [task], [])
    monkeypatch.setattr(st_mod, "_connect_scheduler", lambda: _FakeService(root))
    (info,) = list(iter_scheduled_tasks())
    assert info.task_name == "Half"
    assert info.last_run_time is None
    assert info.last_task_result == 1


class _DeniedTask(_FakeTask):
    """A task whose definition COM refuses to read (a protected or corrupt task)."""

    @property
    def Definition(self) -> Any:  # noqa: N802 - COM API name
        raise OSError("access denied")

    @Definition.setter
    def Definition(self, value: Any) -> None:  # noqa: N802 - COM API name
        del value  # the base __init__ assigns it; this task refuses to read it back


class _DeniedTasksFolder(_FakeFolder):
    """A folder whose task list cannot be enumerated (GetTasks raises), its subfolders still readable."""

    def GetTasks(self, flag: int) -> _FakeCollection:  # noqa: N802 - COM API name
        raise OSError("access denied")


class _DeniedSubfoldersFolder(_FakeFolder):
    """A folder whose tasks read fine but whose subfolders cannot be listed (GetFolders raises)."""

    def GetFolders(self, flag: int) -> _FakeCollection:  # noqa: N802 - COM API name
        raise OSError("access denied")


class _DeniedItemCollection(_FakeCollection):
    """A subfolder collection whose items cannot be opened (Item raises)."""

    def Item(self, index: int) -> Any:  # noqa: N802 - COM API name
        raise OSError("access denied")


class _DeniedChildFolder(_FakeFolder):
    """A folder that lists one subfolder it then refuses to open."""

    def GetFolders(self, flag: int) -> _FakeCollection:  # noqa: N802 - COM API name
        return _DeniedItemCollection([object()])


def _tree_with(unreadable: _FakeFolder) -> _FakeFolder:
    """A root holding a readable task, then ``unreadable``, then a readable sibling folder."""
    after = _FakeFolder("\\After", [_FakeTask("Later", 3, True)], [])
    return _FakeFolder("\\", [_FakeTask("First", 3, True)], [unreadable, after])


def _unreadable_cases() -> list[tuple[_FakeFolder, str, list[str]]]:
    """Each way the walk can fail to read an item: the path the error must name (literal text) and
    the tasks of that same folder the lenient walk must still list."""
    return [
        (_DeniedTasksFolder("\\Locked", [_FakeTask("Hidden", 3, True)], []), "'\\Locked\\'", []),
        (_DeniedSubfoldersFolder("\\NoKids", [_FakeTask("Kept", 3, True)], []), "'\\NoKids\\'", ["Kept"]),
        (_FakeFolder("\\Bad", [_DeniedTask("Broken", 3, True)], []), "'\\Bad\\Broken'", []),
        (_DeniedChildFolder("\\Parent", [], []), "'\\Parent\\'", []),
    ]


_CASE_IDS = ["GetTasks", "GetFolders", "task", "subfolder"]


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("unreadable", "path", "kept"), _unreadable_cases(), ids=_CASE_IDS)
def test_strict_raises_naming_what_it_could_not_read(
    monkeypatch: pytest.MonkeyPatch, unreadable: _FakeFolder, path: str, kept: list[str]
) -> None:
    """[54] strict=True: an unreadable folder, subfolder list, subfolder or task raises, naming its path.

    The source is lazy, so the records before the failure are still yielded; the raise comes
    when the walk reaches the unreadable item, and nothing after it is listed.
    """
    del kept  # what the lenient walk keeps; strict stops at the failure
    monkeypatch.setattr(st_mod, "_connect_scheduler", lambda: _FakeService(_tree_with(unreadable)))
    walk = iter_scheduled_tasks(strict=True)
    assert next(walk).task_name == "First"
    with pytest.raises(NativeCallError) as caught:
        list(walk)
    assert path in str(caught.value)
    assert "access denied" in str(caught.value)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("unreadable", "path", "kept"), _unreadable_cases(), ids=_CASE_IDS)
def test_lenient_skips_what_it_could_not_read_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    unreadable: _FakeFolder,
    path: str,
    kept: list[str],
) -> None:
    """[54] The default lists on past an unreadable item, but never silently: one WARNING names it."""
    monkeypatch.setattr(st_mod, "_connect_scheduler", lambda: _FakeService(_tree_with(unreadable)))
    with caplog.at_level(logging.WARNING, logger=st_mod.__name__):
        names = [t.task_name for t in iter_scheduled_tasks()]
    assert names[0] == "First"
    assert names[-1] == "Later"  # the walk went on past the unreadable item
    assert "Hidden" not in names
    assert "Broken" not in names
    assert all(name in names for name in kept)  # the folder's own readable tasks are still listed
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert path in warnings[0].getMessage()
    assert "access denied" in warnings[0].getMessage()


@pytest.mark.os_agnostic
def test_strict_on_a_readable_tree_yields_what_lenient_yields(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[54] strict only changes what happens on a failure: a readable tree lists identically, warning-free."""
    sub = _FakeFolder("\\Sub", [_FakeTask("Alpha", 3, True, "me", "d"), _FakeTask("Beta", 1, False)], [])
    root = _FakeFolder("\\", [_FakeTask("Top", 3, True)], [sub])
    monkeypatch.setattr(st_mod, "_connect_scheduler", lambda: _FakeService(root))
    with caplog.at_level(logging.WARNING, logger=st_mod.__name__):
        lenient = list(iter_scheduled_tasks())
        strict = list(iter_scheduled_tasks(strict=True))
    assert [t.to_dict() for t in strict] == [t.to_dict() for t in lenient]
    assert [t.task_name for t in strict] == ["Top", "Alpha", "Beta"]
    assert caplog.records == []


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler COM is Windows-only")
def test_iter_scheduled_tasks_reads_real_tasks() -> None:
    """Against the real Task Scheduler, the adapter streams typed records."""
    tasks = [task for _, task in zip(range(5), iter_scheduled_tasks(), strict=False)]
    assert tasks
    assert all(isinstance(t, ScheduledTaskInfo) for t in tasks)
    assert all(t.task_path.endswith("\\") for t in tasks)
