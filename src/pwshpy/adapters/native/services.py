"""native service source over ``win32service`` (Windows-only).

Typed facade over the untyped ``win32service`` surface, mirroring ``Get-Service``.
``EnumServicesStatusEx`` gives name/display/state/pid/type/controls in one bulk
call; ``QueryServiceConfig`` (per service) adds the start type and the
``required_services`` dependency list.  ``pywin32`` is imported lazily so a
portable install stays clean and raises
:class:`~pwshpy.domain.errors.PlatformUnsupportedError` when it is absent.

The services that DEPEND on a given service are not fetched: they are the inverse
of every service's ``required_services`` and are cheaply derived in Python (see
``docs/powershell-switch-mapping.md``).  Mutating verbs (Start/Stop/Restart/...)
belong to the destructive-action design, not this read subsystem.

Contents:
    * :func:`iter_services` - yield one :class:`ServiceInfo` per Windows service.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

from ...domain.enums import ServiceStartType
from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import ServiceInfo
from .marshal import to_service_kind, to_service_start_type, to_service_state


def _load_win32service() -> Any:
    """Import ``win32service`` lazily; absent means a portable (non-Windows) install."""
    try:
        return importlib.import_module("win32service")
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            "The services subsystem requires pywin32 (Windows only); install pwshpy on Windows."
        ) from exc


def _service_config(win32service: Any, scm: Any, name: str) -> tuple[ServiceStartType | None, list[str]]:
    """Return ``(start_type, required_services)`` from QueryServiceConfig.

    Degrades to ``(None, [])`` when the service cannot be opened for config
    (access denied, or the service was removed mid-enumeration).
    """
    try:
        handle = win32service.OpenService(scm, name, win32service.SERVICE_QUERY_CONFIG)
    except Exception:
        return None, []
    try:
        config = win32service.QueryServiceConfig(handle)
    except Exception:
        return None, []
    finally:
        win32service.CloseServiceHandle(handle)
    start_type = to_service_start_type(int(config[1]))
    dependencies: Any = config[6]
    # A dependency prefixed with '+' is a load-order GROUP, not a service; Get-Service
    # omits it from ServicesDependedOn, so drop it for parity.
    required: list[str] = [str(dep) for dep in dependencies if not str(dep).startswith("+")] if dependencies else []
    return start_type, required


def iter_services() -> Iterator[ServiceInfo]:
    """Yield a :class:`ServiceInfo` for every Windows service (like ``Get-Service``).

    Example:
        >>> import sys
        >>> from pwshpy.domain.records import ServiceInfo
        >>> sys.platform != "win32" or isinstance(next(iter_services()), ServiceInfo)
        True
    """
    win32service: Any = _load_win32service()
    accept_stop = win32service.SERVICE_ACCEPT_STOP
    accept_pause = win32service.SERVICE_ACCEPT_PAUSE_CONTINUE
    try:
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ENUMERATE_SERVICE)
    except Exception as exc:
        raise NativeCallError(str(exc) or "cannot open the service control manager") from exc
    try:
        entries = win32service.EnumServicesStatusEx(scm)
    except Exception as exc:
        win32service.CloseServiceHandle(scm)
        raise NativeCallError(str(exc) or "cannot enumerate services") from exc
    try:
        for entry in entries:
            name = str(entry["ServiceName"])
            controls = int(entry["ControlsAccepted"])
            start_type, required = _service_config(win32service, scm, name)
            yield ServiceInfo.model_construct(
                name=name,
                display_name=str(entry["DisplayName"]),
                status=to_service_state(int(entry["CurrentState"])),
                service_type=to_service_kind(int(entry["ServiceType"])),
                pid=int(entry["ProcessId"]) or None,
                can_stop=bool(controls & accept_stop),
                can_pause_continue=bool(controls & accept_pause),
                start_type=start_type,
                required_services=required,
            )
    finally:
        win32service.CloseServiceHandle(scm)


__all__ = ["iter_services"]
