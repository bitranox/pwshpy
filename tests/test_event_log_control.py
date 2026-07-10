"""native event-log control (mutating): fake-backend unit tests + real integration.

The unit tests drive :class:`NativeEventLogController` against a fake
``win32evtlog`` (``os_agnostic``): the right ``EvtClearLog`` call is issued and a
native failure is wrapped.  The ``local_only`` + ``mutating`` test clears a REAL
event log and runs only on the disposable throwaway VM.
"""

# The fake below deliberately mirrors win32evtlog's PascalCase API.
# ruff: noqa: N802

from __future__ import annotations

from typing import Any

import pytest

from pwshpy.adapters.native import event_log_control as mod
from pwshpy.adapters.native.event_log_control import NativeEventLogController
from pwshpy.domain.errors import NativeCallError


class _FakeEvtLog:
    """Minimal ``win32evtlog`` double recording EvtClearLog calls."""

    def __init__(self) -> None:
        self.cleared: list[tuple[Any, ...]] = []
        self.fail = False

    def EvtClearLog(self, channel_path: str, target: str | None = None, flags: int = 0, session: Any = None) -> None:
        if self.fail:
            raise OSError("access denied")
        self.cleared.append((channel_path, target))


def _use(monkeypatch: pytest.MonkeyPatch, fake: _FakeEvtLog) -> None:
    monkeypatch.setattr(mod, "_load_win32evtlog", lambda: fake)


@pytest.mark.os_agnostic
def test_clear_calls_evtclearlog(monkeypatch: pytest.MonkeyPatch) -> None:
    """clear issues EvtClearLog(None, log, None, 0)."""
    fake = _FakeEvtLog()
    _use(monkeypatch, fake)
    NativeEventLogController().clear("Application")
    assert fake.cleared == [("Application", None)]


@pytest.mark.os_agnostic
def test_clear_passes_backup_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backup path is forwarded as the EvtClearLog target file."""
    fake = _FakeEvtLog()
    _use(monkeypatch, fake)
    NativeEventLogController().clear("Application", backup_path="C:\\bak.evtx")
    assert fake.cleared == [("Application", "C:\\bak.evtx")]


@pytest.mark.os_agnostic
def test_clear_failure_wrapped_in_native_call_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw win32 failure surfaces as NativeCallError."""
    fake = _FakeEvtLog()
    fake.fail = True
    _use(monkeypatch, fake)
    with pytest.raises(NativeCallError):
        NativeEventLogController().clear("Application")


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
def test_clear_event_log_on_real_log() -> None:
    """Clear a real event log and confirm it drops to near-empty (throwaway VM only)."""
    from pwshpy.composition import build_ps

    ps = build_ps()
    ps.clear_event_log("Application")  # must not raise
    remaining = ps.get_win_event("Application").take(25).to_list()
    assert len(remaining) <= 10  # only the post-clear audit entry + any immediate writes
