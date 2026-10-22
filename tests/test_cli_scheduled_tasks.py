"""CLI tests for ``get_scheduled_task``'s ``--strict`` flag, against a fake ps facade.

``os_agnostic`` and hermetic: the facade is faked, so the tests pin the CLI's own contract -
the flag reaches ``ps.get_scheduled_task``, and a strict failure leaves through ``main()`` as one
stderr line and exit 1 without leaving a bare ``[`` on a ``--json`` stdout.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fake_services import services_with_ps

from pwshpy.adapters.cli.main import main
from pwshpy.composition import AppServices
from pwshpy.domain.enums import TaskState
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.pipeline import Pipeline
from pwshpy.domain.records import ScheduledTaskInfo

_UNREADABLE = "cannot list the tasks in folder '\\Locked\\': access denied"


def _task(name: str) -> ScheduledTaskInfo:
    return ScheduledTaskInfo(task_name=name, task_path="\\", state=TaskState.READY, enabled=True)


class _FakePs:
    """Records each get_scheduled_task call; under strict, raises after ``before`` records."""

    def __init__(self, *, before: int = 0) -> None:
        self.calls: list[tuple[str, bool]] = []
        self._before = before

    def get_scheduled_task(self, folder_path: str = "\\", *, strict: bool = False) -> Pipeline[ScheduledTaskInfo]:
        self.calls.append((folder_path, strict))
        return Pipeline(self._records(strict=strict))

    def _records(self, *, strict: bool) -> Iterator[ScheduledTaskInfo]:
        for index in range(self._before):
            yield _task(f"T{index}")
        if strict:
            raise NativeCallError(_UNREADABLE)


def _services(fake: _FakePs) -> AppServices:
    return services_with_ps(fake)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("flags", "strict"), [([], False), (["--strict"], True)])
def test_get_scheduled_task_forwards_the_strict_choice(
    capsys: pytest.CaptureFixture[str], flags: list[str], *, strict: bool
) -> None:
    """[54] lenient by default; --strict asks the facade to raise on what it cannot read."""
    fake = _FakePs(before=1)
    services = _services(fake)
    rc = main(["get_scheduled_task", "--folder", "\\F\\", *flags, "--jsonl"], services_factory=lambda: services)
    capsys.readouterr()
    assert fake.calls == [("\\F\\", strict)]
    assert rc == (1 if strict else 0)


@pytest.mark.os_agnostic
def test_a_strict_failure_before_any_record_leaves_json_stdout_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """[54] An unreadable folder under --strict exits 1 with one stderr line, and --json writes no bare ``[``."""
    services = _services(_FakePs(before=0))
    rc = main(["get_scheduled_task", "--strict", "--json"], services_factory=lambda: services)
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.strip() == f"NativeCallError: {_UNREADABLE}"
    assert "Traceback" not in captured.err
