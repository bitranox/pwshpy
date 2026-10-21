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

import sys
import time
import warnings
from typing import Any

import pytest

from pwshpy.adapters.native import service_control as mod
from pwshpy.adapters.native.service_control import NativeServiceController
from pwshpy.composition.facade import Ps
from pwshpy.domain.enums import ServiceStartType, ServiceState
from pwshpy.domain.errors import NativeCallError, OperationTimeoutError


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
    SERVICE_START_PENDING = 2
    SERVICE_STOP_PENDING = 3
    SERVICE_NO_CHANGE = 0xFFFFFFFF

    def __init__(
        self,
        *,
        state: int = 1,
        start_type: int = 3,
        start_effect: int | None = None,
        stop_effect: int | None = None,
        settle_after: int | None = None,
    ) -> None:
        self.state = state
        self.start_type = start_type
        self._start_effect = self.SERVICE_RUNNING if start_effect is None else start_effect
        # A service that takes its time to stop (wuauserv) stays in STOP_PENDING after the control.
        self._stop_effect = self.SERVICE_STOPPED if stop_effect is None else stop_effect
        # A pending state reaches its target after this many status queries (None: never).
        self._settle_after = settle_after
        self._pending_queries = 0
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
        self._advance_pending()
        return (0x10, self.state, 0x3, 0, 0, 0, 0)

    def _advance_pending(self) -> None:
        settled = {self.SERVICE_STOP_PENDING: self.SERVICE_STOPPED, self.SERVICE_START_PENDING: self.SERVICE_RUNNING}
        if self.state not in settled or self._settle_after is None:
            return
        self._pending_queries += 1
        if self._pending_queries > self._settle_after:
            self.state = settled[self.state]

    def StartService(self, handle: Any, args: list[str]) -> None:
        if self.raise_on == "start":
            raise OSError("access denied")
        if self.state in {self.SERVICE_RUNNING, self.SERVICE_START_PENDING}:
            # StartService's contract: ERROR_SERVICE_ALREADY_RUNNING for a running or start-pending service.
            raise OSError(1056, "An instance of the service is already running.")
        self.actions.append("start")
        self.state = self._start_effect

    def ControlService(self, handle: Any, control: int) -> None:
        if self.raise_on == "stop":
            raise OSError("the service cannot accept control messages at this time")
        if self.state in {self.SERVICE_STOPPED, self.SERVICE_START_PENDING, self.SERVICE_STOP_PENDING}:
            # ControlService's contract: ERROR_SERVICE_CANNOT_ACCEPT_CTRL in a pending or stopped state.
            raise OSError(1061, "The service cannot accept control messages at this time.")
        self.actions.append("stop")
        self.state = self._stop_effect

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
    """A start that never reaches RUNNING times out as OperationTimeoutError carrying its timeout."""
    # StartService leaves the state STOPPED, so RUNNING is never reached.
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_STOPPED, start_effect=_FakeWin32Service.SERVICE_STOPPED)
    _use(monkeypatch, fake)
    with pytest.raises(OperationTimeoutError, match="did not reach state") as caught:
        controller.start("Svc", timeout=0.4)
    assert caught.value.timeout == 0.4


@pytest.mark.os_agnostic
def test_stop_without_wait_returns_while_stop_pending(
    monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController
) -> None:
    """wait=False issues the stop and returns the post-control state at once, without polling."""
    fake = _FakeWin32Service(
        state=_FakeWin32Service.SERVICE_RUNNING, stop_effect=_FakeWin32Service.SERVICE_STOP_PENDING
    )
    _use(monkeypatch, fake)
    started = time.monotonic()
    result = controller.stop("Svc", wait=False)
    # The default 30 s timeout would be spent polling a service that never leaves STOP_PENDING.
    assert time.monotonic() - started < 5
    assert result.status is ServiceState.STOP_PENDING
    assert fake.actions == ["stop"]


@pytest.mark.os_agnostic
def test_stop_without_wait_is_a_no_op_when_stopped(
    monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController
) -> None:
    """wait=False keeps the idempotency: an already-stopped service gets no control."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_STOPPED)
    _use(monkeypatch, fake)
    assert controller.stop("Svc", wait=False).status is ServiceState.STOPPED
    assert fake.actions == []


@pytest.mark.os_agnostic
def test_a_waiting_stop_that_stays_pending_raises_operation_timeout_error(
    monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController
) -> None:
    """The waiting stop's deadline is its own type, so 'stop issued, still pending' is not 'refused'."""
    fake = _FakeWin32Service(
        state=_FakeWin32Service.SERVICE_RUNNING, stop_effect=_FakeWin32Service.SERVICE_STOP_PENDING
    )
    _use(monkeypatch, fake)
    with pytest.raises(OperationTimeoutError) as caught:
        controller.stop("Svc", timeout=0.4)
    assert caught.value.timeout == 0.4
    assert fake.actions == ["stop"]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("wait", [True, False])
def test_a_refused_stop_is_native_call_error_not_a_timeout(
    monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController, wait: bool
) -> None:
    """A control the service refuses stays a plain NativeCallError in both forms."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_RUNNING)
    fake.raise_on = "stop"
    _use(monkeypatch, fake)
    with pytest.raises(NativeCallError, match="cannot accept control") as caught:
        controller.stop("Svc", wait=wait)
    assert not isinstance(caught.value, OperationTimeoutError)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("wait", "status"), [(True, ServiceState.STOPPED), (False, ServiceState.STOP_PENDING)])
def test_stop_of_a_stop_pending_service_does_not_resend_the_control(
    monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController, *, wait: bool, status: ServiceState
) -> None:
    """A service already in StopPending has its stop issued: no second control, which it would refuse.

    wait=True waits for the pending stop like Stop-Service does; wait=False returns StopPending.
    """
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_STOP_PENDING, settle_after=2)
    _use(monkeypatch, fake)
    assert controller.stop("Svc", wait=wait, timeout=5.0).status is status
    assert fake.actions == []


@pytest.mark.os_agnostic
def test_a_stop_pending_service_that_never_stops_times_out_rather_than_refusing(
    monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController
) -> None:
    """Waiting on a stop that was already pending ends in OperationTimeoutError, not 'cannot stop'."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_STOP_PENDING)
    _use(monkeypatch, fake)
    with pytest.raises(OperationTimeoutError) as caught:
        controller.stop("Svc", timeout=0.4)
    assert caught.value.timeout == 0.4


@pytest.mark.os_agnostic
def test_start_of_a_start_pending_service_waits_without_resending_the_start(
    monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController
) -> None:
    """A service already in StartPending has its start issued: wait for Running, send no StartService."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_START_PENDING, settle_after=2)
    _use(monkeypatch, fake)
    assert controller.start("Svc", timeout=5.0).status is ServiceState.RUNNING
    assert fake.actions == []


@pytest.mark.os_agnostic
def test_restart_of_a_stop_pending_service_waits_then_starts(
    monkeypatch: pytest.MonkeyPatch, controller: NativeServiceController
) -> None:
    """Restart's stop half waits for the pending stop instead of refusing it, then starts."""
    fake = _FakeWin32Service(state=_FakeWin32Service.SERVICE_STOP_PENDING, settle_after=2)
    _use(monkeypatch, fake)
    assert controller.restart("Svc", timeout=5.0).status is ServiceState.RUNNING
    assert fake.actions == ["start"]


# --- Real integration: run only on the disposable throwaway VM ---------------

_SCRATCH_SERVICE = "Spooler"  # Print Spooler: present on Windows, safe to stop/start on a throwaway box
_RESTORE_TIMEOUT = 120.0  # generous: the restore runs after a failure too, when the service may be slow


def _restore_service(ps: Ps, original: ServiceState | None) -> None:
    """Put the scratch service back in its original state, waiting generously."""
    if original is ServiceState.STOPPED:
        ps.stop_service(_SCRATCH_SERVICE, timeout=_RESTORE_TIMEOUT)
    else:
        ps.start_service(_SCRATCH_SERVICE, timeout=_RESTORE_TIMEOUT)


def _restore_after_failure(ps: Ps, original: ServiceState | None) -> None:
    """Restore after the test body failed: a restore error is warned, so it never hides the body's error."""
    try:
        _restore_service(ps, original)
    except Exception as exc:  # the body's failure is the one to report; this only must not replace it
        warnings.warn(f"could not restore {_SCRATCH_SERVICE}: {exc!r}", stacklevel=2)


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
    except BaseException:
        _restore_after_failure(ps, original)
        raise
    _restore_service(ps, original)


@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="stops a real Windows service")
def test_stop_without_wait_on_real_service_then_waiting_stop_settles() -> None:
    """wait=False returns once the stop is issued; a waiting stop afterwards reaches Stopped.

    Spooler usually stops within the query that follows the control, so the no-wait result may
    already read Stopped: the assertion accepts StopPending or Stopped, never Running. The waiting
    stop that follows may meet the service still in StopPending; it must wait for that pending
    stop (no second control, which the SCM refuses) and end Stopped either way.
    """
    from pwshpy.composition import build_ps

    ps = build_ps()
    original = _service_state(_SCRATCH_SERVICE)
    if original is None:
        pytest.skip(f"{_SCRATCH_SERVICE} service is not present")
    try:
        ps.start_service(_SCRATCH_SERVICE)
        issued = ps.stop_service(_SCRATCH_SERVICE, wait=False)
        assert issued.status in {ServiceState.STOP_PENDING, ServiceState.STOPPED}
        assert ps.stop_service(_SCRATCH_SERVICE, timeout=_RESTORE_TIMEOUT).status is ServiceState.STOPPED
    except BaseException:
        _restore_after_failure(ps, original)
        raise
    _restore_service(ps, original)


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
