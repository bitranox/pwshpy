"""native service CONTROL over ``win32service`` (Windows-only, **mutating**).

Start / Stop / Restart a Windows service and change its startup type - the
mutating counterpart of the read-only :mod:`~pwshpy.adapters.native.services`
source.  Each verb issues the native control call, waits for the service to
reach the target state, then returns a fresh :class:`ServiceInfo`
(query-after-mutate) so the caller can confirm the outcome.  Verbs are
idempotent: starting a running service (or stopping a stopped one) is a no-op,
not an error.

**MUTATING** - see CLAUDE.md "Development Safety": exercise only on a disposable
Windows VM (the throwaway box), never a machine you care about.  ``pywin32`` is
imported lazily so a portable install stays clean.

Contents:
    * :class:`NativeServiceController` - start/stop/restart/set_startup over one SCM.
"""

from __future__ import annotations

import importlib
import time
from typing import Any

from ...domain.enums import ServiceStartType
from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import ServiceInfo
from .marshal import from_service_start_type, to_service_kind, to_service_start_type, to_service_state

_POLL_INTERVAL = 0.25
_DEFAULT_TIMEOUT = 30.0


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
    """Return the service's current win32 ``SERVICE_*`` state id."""
    return int(win32service.QueryServiceStatus(handle)[1])


def _wait_for_state(win32service: Any, handle: Any, target: int, timeout: float) -> None:
    """Poll until the service reaches ``target`` state or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while _current_state(win32service, handle) != target:
        if time.monotonic() >= deadline:
            raise NativeCallError(f"service did not reach state {target} within {timeout}s")
        time.sleep(_POLL_INTERVAL)


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
    """Mutating service control over ``win32service`` (Start/Stop/Restart/Set startup type)."""

    def start(self, name: str, *, timeout: float = _DEFAULT_TIMEOUT) -> ServiceInfo:
        """Start ``name`` (no-op if already running); return its post-start state."""
        win32service = _load_win32service()
        scm = _open_scm(win32service)
        try:
            handle = _open_service(
                win32service, scm, name, win32service.SERVICE_START | win32service.SERVICE_QUERY_STATUS
            )
            try:
                if _current_state(win32service, handle) != win32service.SERVICE_RUNNING:
                    try:
                        win32service.StartService(handle, [])
                    except Exception as exc:
                        raise NativeCallError(str(exc) or f"cannot start service {name!r}") from exc
                    _wait_for_state(win32service, handle, win32service.SERVICE_RUNNING, timeout)
            finally:
                win32service.CloseServiceHandle(handle)
            return _query_one(win32service, scm, name)
        finally:
            win32service.CloseServiceHandle(scm)

    def stop(self, name: str, *, timeout: float = _DEFAULT_TIMEOUT) -> ServiceInfo:
        """Stop ``name`` (no-op if already stopped); return its post-stop state."""
        win32service = _load_win32service()
        scm = _open_scm(win32service)
        try:
            handle = _open_service(
                win32service, scm, name, win32service.SERVICE_STOP | win32service.SERVICE_QUERY_STATUS
            )
            try:
                if _current_state(win32service, handle) != win32service.SERVICE_STOPPED:
                    try:
                        win32service.ControlService(handle, win32service.SERVICE_CONTROL_STOP)
                    except Exception as exc:
                        raise NativeCallError(str(exc) or f"cannot stop service {name!r}") from exc
                    _wait_for_state(win32service, handle, win32service.SERVICE_STOPPED, timeout)
            finally:
                win32service.CloseServiceHandle(handle)
            return _query_one(win32service, scm, name)
        finally:
            win32service.CloseServiceHandle(scm)

    def restart(self, name: str, *, timeout: float = _DEFAULT_TIMEOUT) -> ServiceInfo:
        """Stop then start ``name``; return its post-restart state."""
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


__all__ = ["NativeServiceController"]
