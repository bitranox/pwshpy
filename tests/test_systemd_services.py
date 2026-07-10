"""Native services via systemd D-Bus: pure mapping (os_agnostic) + a live read (Linux).

The ActiveState/UnitFileState mappings are pure functions, so they run on every OS. The
live D-Bus read needs Linux + jeepney; it self-skips otherwise (so a CI lane without the
[systemd] extra skips rather than errors).
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from pwshpy.adapters.native.systemd_services import iter_services, to_service_state, to_start_type
from pwshpy.domain.enums import ServiceStartType, ServiceState
from pwshpy.domain.records import ServiceInfo

_HAS_JEEPNEY = importlib.util.find_spec("jeepney") is not None


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("active_state", "state"),
    [
        ("active", ServiceState.RUNNING),
        ("inactive", ServiceState.STOPPED),
        ("failed", ServiceState.STOPPED),
        ("activating", ServiceState.START_PENDING),
        ("deactivating", ServiceState.STOP_PENDING),
        ("reloading", ServiceState.CONTINUE_PENDING),
        ("nonsense", ServiceState.STOPPED),
    ],
)
def test_active_state_maps(active_state: str, state: ServiceState) -> None:
    """systemd ActiveState maps to the canonical ServiceState (unknown -> Stopped)."""
    assert to_service_state(active_state) is state


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("unit_file_state", "start_type"),
    [
        ("enabled", ServiceStartType.AUTOMATIC),
        ("enabled-runtime", ServiceStartType.AUTOMATIC),
        ("disabled", ServiceStartType.DISABLED),
        ("masked", ServiceStartType.DISABLED),
        ("static", ServiceStartType.MANUAL),
        ("indirect", ServiceStartType.MANUAL),
    ],
)
def test_unit_file_state_maps(unit_file_state: str, start_type: ServiceStartType) -> None:
    """systemd UnitFileState maps to a ServiceStartType."""
    assert to_start_type(unit_file_state) is start_type


@pytest.mark.os_agnostic
def test_unknown_unit_file_state_is_none() -> None:
    """An unrecognized UnitFileState yields None (unknown), not a wrong guess."""
    assert to_start_type("something-new") is None


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux") or not _HAS_JEEPNEY, reason="needs Linux + jeepney")
def test_live_read_yields_services() -> None:
    """A live D-Bus read yields typed .service records (ListUnits is unprivileged)."""
    services = {s.name: s for s in iter_services()}
    assert services
    assert all(isinstance(s, ServiceInfo) for s in services.values())
    assert all(s.name.endswith(".service") for s in services.values())
    assert all(s.service_type is None for s in services.values())  # no Windows ServiceType on Linux
