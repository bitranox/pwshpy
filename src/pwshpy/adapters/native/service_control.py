"""native service CONTROL over ``win32service`` (Windows-only, **mutating**).

Start / Stop / Restart a Windows service and change its startup type - the
mutating counterpart of the read-only :mod:`~pwshpy.adapters.native.services`
source.  Each verb issues the native control call, waits for the service to
reach the target state, then returns a fresh :class:`ServiceInfo`
(query-after-mutate) so the caller can confirm the outcome.  A wait that hits
its deadline raises :class:`OperationTimeoutError`, a control the service
refuses raises a plain :class:`NativeCallError`, so the two stay apart.
``stop(wait=False)`` issues the stop and returns the state right after it,
typically ``StopPending``.  Verbs are idempotent: starting a running service
(or stopping a stopped one) is a no-op, not an error.  A service already in
``StartPending`` (``StopPending``) has its start (stop) issued, so the control is
not sent again - the SCM would refuse it - and the verb waits for that pending
transition instead, as ``Start-Service`` / ``Stop-Service`` do.  A service in any
OTHER pending state (a start meeting ``StopPending``, a stop meeting
``StartPending``) cannot take the control yet - the SCM answers 1056 for the start
and 1052 or 1061 for the stop, which read as "already running" / "refused" - so the
verb first waits for it to settle, then decides again from the settled state
(:func:`next_control_step`).  One deadline, ``timeout`` seconds from the call,
bounds the settle wait and the wait for the target state together.

**MUTATING** - see CLAUDE.md "Development Safety": exercise only on a disposable
Windows VM (the throwaway box), never a machine you care about.  ``pywin32`` is
imported lazily so a portable install stays clean.

Contents:
    * :class:`ControlStep` / :func:`next_control_step` - the pure decision what a verb does in a state.
    * :class:`PollClock` - the clock the waits read and sleep on (injectable, so tests need no real time).
    * :class:`NativeServiceController` - start/stop/restart/set_startup over one SCM.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ...domain.enums import ServiceStartType
from ...domain.errors import NativeCallError, OperationTimeoutError, PlatformUnsupportedError
from ...domain.records import ServiceInfo
from .marshal import from_service_start_type, to_service_kind, to_service_start_type, to_service_state

_POLL_INTERVAL = 0.25
_DEFAULT_TIMEOUT = 30.0

# win32 SERVICE_* state ids (winsvc.h); fixed by the Win32 ABI, so the decision below needs no pywin32.
_SERVICE_STOPPED = 1
_SERVICE_START_PENDING = 2
_SERVICE_STOP_PENDING = 3
_SERVICE_RUNNING = 4
_SERVICE_CONTINUE_PENDING = 5
_SERVICE_PAUSE_PENDING = 6
_PENDING_STATES = frozenset(
    {_SERVICE_START_PENDING, _SERVICE_STOP_PENDING, _SERVICE_CONTINUE_PENDING, _SERVICE_PAUSE_PENDING}
)


class ControlStep(Enum):
    """What a start/stop verb does next, given the service's current state."""

    DONE = "done"
    """Already in the target state: send nothing (idempotent no-op)."""
    AWAIT_TARGET = "await_target"
    """The verb's own transition is already pending: send nothing, wait for the target."""
    SETTLE_FIRST = "settle_first"
    """Another transition is pending and the SCM would refuse the control: wait for a settled state."""
    ISSUE = "issue"
    """Settled in another state: send the control (then wait for the target, unless told not to)."""


def next_control_step(state: int, *, target: int, target_pending: int) -> ControlStep:
    """Decide what a verb heading for ``target`` does in ``state``.

    Args:
        state: The service's current win32 ``SERVICE_*`` state id.
        target: The state the verb ends in (``SERVICE_RUNNING`` for start, ``SERVICE_STOPPED`` for stop).
        target_pending: The pending state on the way there (``SERVICE_START_PENDING`` / ``SERVICE_STOP_PENDING``).

    Returns:
        The :class:`ControlStep`. ``SETTLE_FIRST`` never follows a settled state, so deciding again
        after the settle wait yields ``DONE`` or ``ISSUE``.

    Examples:
        >>> next_control_step(3, target=4, target_pending=2)
        <ControlStep.SETTLE_FIRST: 'settle_first'>
        >>> next_control_step(1, target=4, target_pending=2)
        <ControlStep.ISSUE: 'issue'>
    """
    if state == target:
        return ControlStep.DONE
    if state == target_pending:
        return ControlStep.AWAIT_TARGET
    if state in _PENDING_STATES:
        return ControlStep.SETTLE_FIRST
    return ControlStep.ISSUE


def _load_win32service() -> Any:
    """Import ``win32service`` lazily; absent means a portable (non-Windows) install."""
    try:
        return importlib.import_module("win32service")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "Service control requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc


def _open_scm(win32service: Any) -> Any:
    """Open the service control manager for connecting to individual services."""
    try:
        return win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    except Exception as exc:
        raise NativeCallError(str(exc) or "cannot open the service control manager") from exc


def _open_service(win32service: Any, scm: Any, name: str, access: int) -> Any:
    """Open one service by name with the requested access mask."""
    try:
        return win32service.OpenService(scm, name, access)
    except Exception as exc:
        raise NativeCallError(str(exc) or f"cannot open service {name!r}") from exc


def _current_state(win32service: Any, handle: Any) -> int:
    """Return the service's current win32 ``SERVICE_*`` state id.

    Raises:
        NativeCallError: ``QueryServiceStatus`` failed (a handle closed under it, access revoked).
    """
    try:
        return int(win32service.QueryServiceStatus(handle)[1])
    except Exception as exc:
        raise NativeCallError(str(exc) or "cannot query the service status") from exc


@dataclass(frozen=True)
class PollClock:
    """The clock the service waits read (``monotonic``) and poll with (``sleep``).

    The default is the real one; a test passes a stepped clock so a deadline is reached
    deterministically, without sleeping.
    """

    monotonic: Callable[[], float] = time.monotonic
    """Returns the current time in seconds (monotonic)."""
    sleep: Callable[[float], None] = time.sleep
    """Waits the given seconds between two status queries."""


@dataclass(frozen=True)
class _Deadline:
    """One call's time budget: where it ends on ``clock``, and the ``timeout`` it came from."""

    clock: PollClock
    at: float
    timeout: float

    @classmethod
    def after(cls, timeout: float, clock: PollClock) -> _Deadline:
        """Start a budget of ``timeout`` seconds on ``clock`` now."""
        return cls(clock=clock, at=clock.monotonic() + timeout, timeout=timeout)

    def passed(self) -> bool:
        """True once the clock reached the end of the budget."""
        return self.clock.monotonic() >= self.at


def _wait_for_state(win32service: Any, handle: Any, target: int, *, deadline: _Deadline) -> None:
    """Poll until the service reaches ``target`` state; raise OperationTimeoutError at ``deadline``."""
    timeout = deadline.timeout
    while _current_state(win32service, handle) != target:
        if deadline.passed():
            # Its own type: the control was accepted and the service is still on its way (a
            # wuauserv in StopPending), which a caller handles differently from a refused control.
            raise OperationTimeoutError(f"service did not reach state {target} within {timeout}s", timeout=timeout)
        deadline.clock.sleep(_POLL_INTERVAL)


def _wait_until_settled(win32service: Any, handle: Any, *, deadline: _Deadline) -> int:
    """Poll until the service leaves every pending state; return the settled state id.

    Raises:
        OperationTimeoutError: still pending at ``deadline``; no control was sent.
        NativeCallError: the status query failed.
    """
    timeout = deadline.timeout
    state = _current_state(win32service, handle)
    while state in _PENDING_STATES:
        if deadline.passed():
            raise OperationTimeoutError(
                f"service still in pending state {state} after {timeout}s; no control sent", timeout=timeout
            )
        deadline.clock.sleep(_POLL_INTERVAL)
        state = _current_state(win32service, handle)
    return state


@dataclass(frozen=True)
class _Control:
    """One verb's transition: the control that starts it and the states it passes through."""

    issue: Callable[[], None]
    """Sends the control (StartService / ControlService STOP), wrapping its failure."""
    target: int
    """The state the verb ends in."""
    target_pending: int
    """The pending state on the way to ``target``."""

    def step_in(self, state: int) -> ControlStep:
        """Return :func:`next_control_step` for this transition in ``state``."""
        return next_control_step(state, target=self.target, target_pending=self.target_pending)


def _drive(win32service: Any, handle: Any, control: _Control, *, deadline: _Deadline, wait: bool = True) -> None:
    """Bring one open service to ``control.target``: settle a foreign pending state, issue, then wait.

    Args:
        win32service: The (lazily imported) ``win32service`` module.
        handle: The open service handle (query rights plus the right the control needs).
        control: The verb's transition.
        deadline: The call's one budget, shared by the settle wait and the target wait.
        wait: False skips only the wait for the target after the control is sent.
    """
    step = control.step_in(_current_state(win32service, handle))
    if step is ControlStep.SETTLE_FIRST:
        step = control.step_in(_wait_until_settled(win32service, handle, deadline=deadline))
    if step is ControlStep.ISSUE:
        control.issue()
    if wait and step is not ControlStep.DONE:
        _wait_for_state(win32service, handle, control.target, deadline=deadline)


def _query_one(win32service: Any, scm: Any, name: str) -> ServiceInfo:
    """Build a :class:`ServiceInfo` snapshot for a single service after a mutation."""
    accept_stop = win32service.SERVICE_ACCEPT_STOP
    accept_pause = win32service.SERVICE_ACCEPT_PAUSE_CONTINUE
    handle = _open_service(
        win32service, scm, name, win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_QUERY_CONFIG
    )
    try:
        status = win32service.QueryServiceStatusEx(handle)
        config = win32service.QueryServiceConfig(handle)
    except Exception as exc:
        raise NativeCallError(str(exc) or f"cannot query service {name!r}") from exc
    finally:
        win32service.CloseServiceHandle(handle)
    controls = int(status["ControlsAccepted"])
    dependencies: Any = config[6]
    required = [str(dep) for dep in dependencies if not str(dep).startswith("+")] if dependencies else []
    return ServiceInfo.model_construct(
        name=name,
        display_name=str(config[8]),
        status=to_service_state(int(status["CurrentState"])),
        service_type=to_service_kind(int(status["ServiceType"])),
        pid=int(status["ProcessId"]) or None,
        can_stop=bool(controls & accept_stop),
        can_pause_continue=bool(controls & accept_pause),
        start_type=to_service_start_type(int(config[1])),
        required_services=required,
    )


class NativeServiceController:
    """Mutating service control over ``win32service`` (Start/Stop/Restart/Set startup type).

    Args:
        clock: The clock the waits read and poll with; the real one when omitted.
    """

    def __init__(self, *, clock: PollClock | None = None) -> None:
        self._clock = clock if clock is not None else PollClock()

    def start(self, name: str, *, timeout: float = _DEFAULT_TIMEOUT) -> ServiceInfo:
        """Start ``name`` (no-op if already running); return its post-start state.

        A service already in ``StartPending`` gets no second StartService; the call waits for it.
        One in ``StopPending`` (or another foreign pending state) is first waited out, since
        StartService would fail on it with ERROR_SERVICE_ALREADY_RUNNING (1056).

        Raises:
            OperationTimeoutError: not settled, or not Running, within ``timeout`` seconds.
            NativeCallError: the SCM refused the start, or a status query failed.
        """
        win32service = _load_win32service()
        scm = _open_scm(win32service)
        try:
            handle = _open_service(
                win32service, scm, name, win32service.SERVICE_START | win32service.SERVICE_QUERY_STATUS
            )

            def issue() -> None:
                try:
                    win32service.StartService(handle, [])
                except Exception as exc:
                    raise NativeCallError(str(exc) or f"cannot start service {name!r}") from exc

            try:
                control = _Control(issue, target=_SERVICE_RUNNING, target_pending=_SERVICE_START_PENDING)
                _drive(win32service, handle, control, deadline=_Deadline.after(timeout, self._clock))
            finally:
                win32service.CloseServiceHandle(handle)
            return _query_one(win32service, scm, name)
        finally:
            win32service.CloseServiceHandle(scm)

    def stop(self, name: str, *, timeout: float = _DEFAULT_TIMEOUT, wait: bool = True) -> ServiceInfo:
        """Stop ``name`` (no-op if already stopped); return its post-stop state.

        With ``wait=False`` the stop is issued and the state right after the control is
        returned (typically ``StopPending``) without waiting for Stopped. A service already in
        ``StopPending`` gets no second control: ``wait=True`` waits for the pending stop,
        ``wait=False`` returns ``StopPending``. One in ``StartPending`` (or another foreign
        pending state) is first waited out, bounded by ``timeout``, in both forms: the SCM
        refuses a stop there: ERROR_INVALID_SERVICE_CONTROL (1052) when the starting service
        accepts no stop yet (measured on Windows 11), ERROR_SERVICE_CANNOT_ACCEPT_CTRL (1061)
        otherwise.

        Raises:
            OperationTimeoutError: not settled, or (``wait=True``) not Stopped, within ``timeout`` seconds.
            NativeCallError: the SCM refused the stop, or a status query failed.
        """
        win32service = _load_win32service()
        scm = _open_scm(win32service)
        try:
            handle = _open_service(
                win32service, scm, name, win32service.SERVICE_STOP | win32service.SERVICE_QUERY_STATUS
            )

            def issue() -> None:
                try:
                    win32service.ControlService(handle, win32service.SERVICE_CONTROL_STOP)
                except Exception as exc:
                    raise NativeCallError(str(exc) or f"cannot stop service {name!r}") from exc

            try:
                control = _Control(issue, target=_SERVICE_STOPPED, target_pending=_SERVICE_STOP_PENDING)
                _drive(win32service, handle, control, deadline=_Deadline.after(timeout, self._clock), wait=wait)
            finally:
                win32service.CloseServiceHandle(handle)
            return _query_one(win32service, scm, name)
        finally:
            win32service.CloseServiceHandle(scm)

    def restart(self, name: str, *, timeout: float = _DEFAULT_TIMEOUT) -> ServiceInfo:
        """Stop then start ``name``; return its post-restart state.

        Unlike a single start or stop, this is two calls, and each gets its own ``timeout``:
        the whole restart can take up to twice ``timeout``.
        """
        self.stop(name, timeout=timeout)
        return self.start(name, timeout=timeout)

    def set_startup(self, name: str, start_type: ServiceStartType) -> ServiceInfo:
        """Change ``name``'s startup type (Automatic/Manual/Disabled/...); return its new config."""
        win32service = _load_win32service()
        scm = _open_scm(win32service)
        try:
            handle = _open_service(win32service, scm, name, win32service.SERVICE_CHANGE_CONFIG)
            try:
                no_change = win32service.SERVICE_NO_CHANGE
                win32service.ChangeServiceConfig(
                    handle,
                    no_change,
                    from_service_start_type(start_type),
                    no_change,
                    None,
                    None,
                    0,
                    None,
                    None,
                    None,
                    None,
                )
            except Exception as exc:
                raise NativeCallError(str(exc) or f"cannot reconfigure service {name!r}") from exc
            finally:
                win32service.CloseServiceHandle(handle)
            return _query_one(win32service, scm, name)
        finally:
            win32service.CloseServiceHandle(scm)


__all__ = ["ControlStep", "NativeServiceController", "PollClock", "next_control_step"]
