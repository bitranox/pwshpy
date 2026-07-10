"""native service control (mutating): fake-injected unit tests + real integration tests.

The unit tests drive :class:`NativeServiceController` against a fake
``win32service`` (``os_agnostic``, run everywhere for coverage of the control
flow, idempotency, error wrapping, and the wait-for-state timeout).

The ``local_only`` + ``mutating`` tests actually start/stop/restart a real
Windows service (Print Spooler) and restore it - they run ONLY on the disposable
throwaway VM (see CLAUDE.md Development Safety), never in CI.
"""

# The fake below deliberately mirrors win32service's PascalCase method names.
# ruff: noqa: N802

from __future__ import annotations

from typing import Any

import pytest

from pwshpy.adapters.native import service_control as mod
from pwshpy.adapters.native.service_control import NativeServiceController
from pwshpy.domain.enums import ServiceStartType, ServiceState
from pwshpy.domain.errors import NativeCallError


class _FakeWin32Service:
    """Minimal in-memory ``win32service`` double: one service with a mutable state."""

    SC_MANAGER_CONNECT = 0x0001
    SERVICE_START = 0x0010
    SERVICE_STOP = 0x0020
    SERVICE_QUERY_STATUS = 0x0004
    SERVICE_QUERY_CONFIG = 0x0001
    SERVICE_CHANGE_CONFIG = 0x0002
    SERVICE_ACCEPT_STOP = 0x0001
    SERVICE_ACCEPT_PAUSE_CONTINUE = 0x0002
    SERVICE_CONTROL_STOP = 0x0001
    SERVICE_RUNNING = 4
    SERVICE_STOPPED = 1
    SERVICE_NO_CHANGE = 0xFFFFFFFF

    def __init__(self, *, state: int = 1, start_type: int = 3, start_effect: int | None = None) -> None:
        self.state = state
        self.start_type = start_type
        self._start_effect = self.SERVICE_RUNNING if start_effect is None else start_effect
        self.actions: list[str] = []
        self.closed = 0
        self.raise_on: str | None = None

    def OpenSCManager(self, machine: Any, db: Any, access: int) -> str:
        return "scm"

    def OpenService(self, scm: Any, name: str, access: int) -> str:
        return f"svc:{name}"

    def CloseServiceHandle(self, handle: Any) -> None:
        self.closed += 1

    def QueryServiceStatus(self, handle: Any) -> tuple[int, ...]:
        return (0x10, self.state, 0x3, 0, 0, 0, 0)

    def StartService(self, handle: Any, args: list[str]) -> None:
        if self.raise_on == "start":
            raise OSError("access denied")
        self.actions.append("start")
        self.state = self._start_effect

    def ControlService(self, handle: Any, control: int) -> None:
        self.actions.append("stop")
        self.state = self.SERVICE_STOPPED

    def ChangeServiceConfig(self, handle: Any, service_type: int, start_type: int, *rest: Any) -> None:
        if self.raise_on == "config":
            raise OSError("access denied")
        self.actions.append("config")
        self.start_type = start_type

    def QueryServiceStatusEx(self, handle: Any) -> dict[str, int]:
        return {
            "CurrentState": self.state,
            "ProcessId": 1234 if self.state == self.SERVICE_RUNNING else 0,
            "ServiceType": 0x10,
            "ControlsAccepted": 0x3,
        }

    def QueryServiceConfig(self, handle: Any) -> tuple[Any, ...]:
        return (0x10, self.start_type, 0, "", "", 0, [], "LocalSystem", "Fake Display")


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> NativeServiceController:
    """A controller whose ``win32service`` is swapped for a fake (set via ``_use``)."""
    return NativeServiceController()


def _use(monkeypatch: pytest.MonkeyPatch, fake: _FakeWin32Service) -> None:
    monkeypatch.setattr(mod, "_load_win32service", lambda: fake)


@pytest.mark.os_agnostic
def test_start_starts_a_stopped_service(monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController) -> None:
    """Starting a stopped service issues StartService and reports RUNNING."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_STOPPED)
    _use(monkeypatch, fake)
    result = controller.start("Svc")
    assert result.status is ServiceState.RUNNING
    assert fake.actions == ["start"]


@pytest.mark.os_agnostic
def test_start_is_idempotent_when_running(monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController) -> None:
    """Starting an already-running service is a no-op (no StartService call)."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_RUNNING)
    _use(monkeypatch, fake)
    result = controller.start("Svc")
    assert result.status is ServiceState.RUNNING
    assert fake.actions == []


@pytest.mark.os_agnostic
def test_stop_stops_a_running_service(monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController) -> None:
    """Stopping a running service issues ControlService(STOP) and reports STOPPED."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_RUNNING)
    _use(monkeypatch, fake)
    result = controller.stop("Svc")
    assert result.status is ServiceState.STOPPED
    assert fake.actions == ["stop"]


@pytest.mark.os_agnostic
def test_stop_is_idempotent_when_stopped(monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController) -> None:
    """Stopping an already-stopped service is a no-op."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_STOPPED)
    _use(monkeypatch, fake)
    assert controller.stop("Svc").status is ServiceState.STOPPED
    assert fake.actions == []


@pytest.mark.os_agnostic
def test_restart_stops_then_starts(monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController) -> None:
    """Restart stops then starts, ending RUNNING."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_RUNNING)
    _use(monkeypatch, fake)
    result = controller.restart("Svc")
    assert result.status is ServiceState.RUNNING
    assert fake.actions == ["stop", "start"]


@pytest.mark.os_agnostic
def test_set_startup_changes_config(monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController) -> None:
    """set_startup issues ChangeServiceConfig and reports the new start type."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_RUNNING, start_type=3)
    _use(monkeypatch, fake)
    result = controller.set_startup("Svc", ServiceStartType.DISABLED)
    assert result.start_type is ServiceStartType.DISABLED
    assert fake.actions == ["config"]


@pytest.mark.os_agnostic
def test_native_failure_wrapped_in_native_call_error(
    monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController
) -> None:
    """A raw win32 failure surfaces as NativeCallError, not the bare OSError."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_STOPPED)
    fake.raise_on = "start"
    _use(monkeypatch, fake)
    with pytest.raises(NativeCallError):
        controller.start("Svc")


@pytest.mark.os_agnostic
def test_wait_for_state_times_out(monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController) -> None:
    """If the service never reaches the target state, the wait times out as NativeCallError."""
    # StartService leaves the state STOPPED, so RUNNING is never reached.
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_STOPPED, start_effect=_FakeWin32Service.SERVICE_STOPPED)
    _use(monkeypatch, fake)
    with pytest.raises(NativeCallError, match="did not reach state"):
        controller.start("Svc", timeout=0.4)


# --- Real integration: run only on the disposable throwaway VM ---------------

_SCRATCH_SERVICE = "Spooler"  # Print Spooler: present on Windows, safe to stop/start on a throwaway box


def _service_state(name: str) -> ServiceState | None:
    from pwshpy.composition import build_ps

    svc = build_ps().get_service().where(lambda s: s.name == name).first()
    return svc.status if svc else None


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
def test_stop_start_restart_roundtrip_on_real_service() -> None:
    """Stop -> start -> restart a real service, then restore its original state."""
    from pwshpy.composition import build_ps

    ps = build_ps()
    if _service_state(_SCRATCH_SERVICE) is None:
        pytest.skip(f"{_SCRATCH_SERVICE} service is not present")
    original = _service_state(_SCRATCH_SERVICE)
    try:
        assert ps.stop_service(_SCRATCH_SERVICE).status is ServiceState.STOPPED
        assert ps.start_service(_SCRATCH_SERVICE).status is ServiceState.RUNNING
        # idempotent second start
        assert ps.start_service(_SCRATCH_SERVICE).status is ServiceState.RUNNING
        assert ps.restart_service(_SCRATCH_SERVICE).status is ServiceState.RUNNING
    finally:
        if original is ServiceState.STOPPED:
            ps.stop_service(_SCRATCH_SERVICE)
        else:
            ps.start_service(_SCRATCH_SERVICE)


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
def test_set_startup_roundtrip_on_real_service() -> None:
    """Change a real service's startup type, then restore it."""
    from pwshpy.composition import build_ps

    ps = build_ps()
    svc = ps.get_service().where(lambda s: s.name == _SCRATCH_SERVICE).first()
    if svc is None or svc.start_type is None:
        pytest.skip(f"{_SCRATCH_SERVICE} service or its start type is unavailable")
    original = svc.start_type
    try:
        changed = ps.set_service(_SCRATCH_SERVICE, ServiceStartType.DISABLED)
        assert changed.start_type is ServiceStartType.DISABLED
        remanual = ps.set_service(_SCRATCH_SERVICE, ServiceStartType.MANUAL)
        assert remanual.start_type is ServiceStartType.MANUAL
    finally:
        ps.set_service(_SCRATCH_SERVICE, original)
