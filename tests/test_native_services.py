"""Services native adapter over win32service.

The exact live-Windows behaviour is pinned against real ``Get-Service`` in
``test_services_pwsh_oracle``.  Here a fake ``win32service`` (injected at the
adapter's load seam) exercises the marshaling and the graceful-degradation
branches deterministically on every OS, plus a real structural check on Windows.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from pwshpy.adapters.native import services as services_mod
from pwshpy.adapters.native.services import iter_services
from pwshpy.domain.enums import ServiceKind, ServiceStartType, ServiceState
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import ServiceInfo


class _FakeWin32Service:
    """A minimal stand-in for the ``win32service`` module (records, not mocks)."""

    SC_MANAGER_ENUMERATE_SERVICE = 4
    SERVICE_QUERY_CONFIG = 1
    SERVICE_ACCEPT_STOP = 1
    SERVICE_ACCEPT_PAUSE_CONTINUE = 2

    def OpenSCManager(self, machine: Any, database: Any, access: Any) -> str:  # noqa: N802 - win32 API name
        return "SCM"

    def CloseServiceHandle(self, handle: Any) -> None:  # noqa: N802 - win32 API name
        return None

    def EnumServicesStatusEx(self, scm: Any) -> list[dict[str, Any]]:  # noqa: N802 - win32 API name
        return [
            {
                "ServiceName": "Alpha",
                "DisplayName": "Alpha Svc",
                "ServiceType": 0x10,
                "CurrentState": 4,
                "ControlsAccepted": 3,
                "ProcessId": 100,
            },
            {
                "ServiceName": "Beta",
                "DisplayName": "Beta Svc",
                "ServiceType": 0x20,
                "CurrentState": 1,
                "ControlsAccepted": 0,
                "ProcessId": 0,
            },
            {
                "ServiceName": "Gamma",
                "DisplayName": "Gamma Svc",
                "ServiceType": 0x10,
                "CurrentState": 4,
                "ControlsAccepted": 1,
                "ProcessId": 7,
            },
        ]

    def OpenService(self, scm: Any, name: str, access: Any) -> str:  # noqa: N802 - win32 API name
        if name == "Gamma":
            raise OSError("access denied")  # -> config degrades to (None, [])
        return f"H-{name}"

    def QueryServiceConfig(self, handle: Any) -> tuple[Any, ...]:  # noqa: N802 - win32 API name
        if handle == "H-Alpha":
            return (0x10, 2, 1, r"C:\a.exe", "", 0, ["RpcSs", "Dhcp"], "LocalSystem", "Alpha Svc")
        raise OSError("config unavailable")  # Beta -> config degrades to (None, [])


@pytest.mark.os_agnostic
def test_iter_services_marshals_fake_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """iter_services marshals the win32 shapes and degrades when config is inaccessible."""
    monkeypatch.setattr(services_mod, "_load_win32service", _FakeWin32Service)
    by_name = {s.name: s for s in iter_services()}
    assert set(by_name) == {"Alpha", "Beta", "Gamma"}

    alpha = by_name["Alpha"]
    assert alpha.display_name == "Alpha Svc"
    assert alpha.status is ServiceState.RUNNING
    assert alpha.service_type is ServiceKind.WIN32_OWN_PROCESS
    assert alpha.pid == 100
    assert alpha.can_stop is True
    assert alpha.can_pause_continue is True
    assert alpha.start_type is ServiceStartType.AUTOMATIC
    assert alpha.required_services == ["RpcSs", "Dhcp"]

    beta = by_name["Beta"]  # QueryServiceConfig raised -> degraded config
    assert beta.status is ServiceState.STOPPED
    assert beta.service_type is ServiceKind.WIN32_SHARE_PROCESS
    assert beta.pid is None  # ProcessId 0 -> None
    assert beta.can_stop is False
    assert beta.can_pause_continue is False
    assert beta.start_type is None
    assert beta.required_services == []

    gamma = by_name["Gamma"]  # OpenService raised -> degraded config
    assert gamma.start_type is None
    assert gamma.required_services == []
    assert gamma.can_stop is True  # ControlsAccepted 1 & SERVICE_ACCEPT_STOP


@pytest.mark.os_agnostic
def test_iter_services_filters_group_deps_and_no_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A '+'-prefixed load-order group is dropped, and a no-dependency service yields []."""

    class _CfgWin32:
        SERVICE_ACCEPT_STOP = 1
        SERVICE_ACCEPT_PAUSE_CONTINUE = 2
        SERVICE_QUERY_CONFIG = 1
        SC_MANAGER_ENUMERATE_SERVICE = 4

        def OpenSCManager(self, machine: Any, database: Any, access: Any) -> str:  # noqa: N802 - win32 API name
            return "SCM"

        def CloseServiceHandle(self, handle: Any) -> None:  # noqa: N802 - win32 API name
            return None

        def EnumServicesStatusEx(self, scm: Any) -> list[dict[str, Any]]:  # noqa: N802 - win32 API name
            base = {"ServiceType": 0x10, "CurrentState": 4, "ControlsAccepted": 0}
            return [
                {"ServiceName": "Grouped", "DisplayName": "G", "ProcessId": 1, **base},
                {"ServiceName": "NoDeps", "DisplayName": "N", "ProcessId": 2, **base},
            ]

        def OpenService(self, scm: Any, name: str, access: Any) -> str:  # noqa: N802 - win32 API name
            return f"H-{name}"

        def QueryServiceConfig(self, handle: Any) -> tuple[Any, ...]:  # noqa: N802 - win32 API name
            if handle == "H-Grouped":
                return (0x10, 2, 1, "x", "", 0, ["RpcSs", "+GroupX", "Dhcp"], "LocalSystem", "G")
            return (0x10, 3, 1, "x", "", 0, None, "LocalSystem", "N")  # NoDeps: config[6] is None on success

    monkeypatch.setattr(services_mod, "_load_win32service", _CfgWin32)
    by_name = {s.name: s for s in iter_services()}
    assert by_name["Grouped"].required_services == ["RpcSs", "Dhcp"]  # '+' group filtered out
    assert by_name["NoDeps"].required_services == []  # no deps on a successful config read
    assert by_name["NoDeps"].start_type is ServiceStartType.MANUAL


@pytest.mark.os_agnostic
def test_iter_services_wraps_scm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure to open the service control manager surfaces as NativeCallError."""

    class _BadScm(_FakeWin32Service):
        def OpenSCManager(self, machine: Any, database: Any, access: Any) -> str:  # noqa: N802 - win32 API name
            raise OSError("access denied")

    monkeypatch.setattr(services_mod, "_load_win32service", _BadScm)
    with pytest.raises(NativeCallError):
        list(iter_services())


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="win32service is Windows-only")
def test_iter_services_reads_real_services() -> None:
    """Against the real SCM, iter_services yields typed records incl. a well-known service."""
    services = list(iter_services())
    assert services
    assert all(isinstance(s, ServiceInfo) for s in services)
    assert "eventlog" in {s.name.lower() for s in services}  # present on every Windows install
