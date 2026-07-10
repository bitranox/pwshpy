"""Service marshaling: win32 ``SERVICE_*`` ids / flags -> typed enums (pure, os-agnostic)."""

from __future__ import annotations

import pytest

from pwshpy.adapters.native.marshal import to_service_kind, to_service_start_type, to_service_state
from pwshpy.domain.enums import ServiceKind, ServiceStartType, ServiceState


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (1, ServiceState.STOPPED),
        (2, ServiceState.START_PENDING),
        (3, ServiceState.STOP_PENDING),
        (4, ServiceState.RUNNING),
        (5, ServiceState.CONTINUE_PENDING),
        (6, ServiceState.PAUSE_PENDING),
        (7, ServiceState.PAUSED),
        (999, ServiceState.STOPPED),  # unknown -> STOPPED
    ],
)
def test_service_state(state: int, expected: ServiceState) -> None:
    """Each win32 state id maps to its ServiceState; unknown degrades to STOPPED."""
    assert to_service_state(state) is expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("start_type", "expected"),
    [
        (0, ServiceStartType.BOOT),
        (1, ServiceStartType.SYSTEM),
        (2, ServiceStartType.AUTOMATIC),
        (3, ServiceStartType.MANUAL),
        (4, ServiceStartType.DISABLED),
        (99, ServiceStartType.MANUAL),  # unknown -> MANUAL
    ],
)
def test_service_start_type(start_type: int, expected: ServiceStartType) -> None:
    """Each win32 start id maps to its ServiceStartType; unknown degrades to MANUAL."""
    assert to_service_start_type(start_type) is expected


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("service_type", "expected"),
    [
        (0x01, ServiceKind.KERNEL_DRIVER),
        (0x02, ServiceKind.FILE_SYSTEM_DRIVER),
        (0x04, ServiceKind.ADAPTER),
        (0x08, ServiceKind.RECOGNIZER_DRIVER),
        (0x10, ServiceKind.WIN32_OWN_PROCESS),
        (0x20, ServiceKind.WIN32_SHARE_PROCESS),
        (0x10 | 0x100, ServiceKind.WIN32_OWN_PROCESS),  # interactive flag ignored
        (0x30, ServiceKind.WIN32_OWN_PROCESS),  # own+share -> primary (own)
        (0x100, ServiceKind.INTERACTIVE_PROCESS),  # interactive only
        (0x00, ServiceKind.WIN32_OWN_PROCESS),  # nothing set -> fallback
    ],
)
def test_service_kind(service_type: int, expected: ServiceKind) -> None:
    """Service-type flags map to the primary kind, drivers first, interactive last."""
    assert to_service_kind(service_type) is expected
