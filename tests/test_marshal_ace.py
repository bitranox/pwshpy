"""ACE-type marshaling: Win32 ACE type ids -> AceType (pure, os-agnostic)."""

from __future__ import annotations

import pytest

from pwshpy.adapters.native.marshal import to_ace_type
from pwshpy.domain.enums import AceType


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("ace_type", "expected"),
    [
        (0, AceType.ALLOW),  # ACCESS_ALLOWED_ACE_TYPE
        (1, AceType.DENY),  # ACCESS_DENIED_ACE_TYPE
        (5, AceType.ALLOW),  # ACCESS_ALLOWED_OBJECT_ACE_TYPE
        (6, AceType.DENY),  # ACCESS_DENIED_OBJECT_ACE_TYPE
        (9, AceType.ALLOW),  # ACCESS_ALLOWED_CALLBACK_ACE_TYPE
        (10, AceType.DENY),  # ACCESS_DENIED_CALLBACK_ACE_TYPE
        (12, AceType.DENY),  # ACCESS_DENIED_CALLBACK_OBJECT_ACE_TYPE
        (99, AceType.ALLOW),  # unknown -> ALLOW
    ],
)
def test_ace_type(ace_type: int, expected: AceType) -> None:
    """Denied ACE type ids map to DENY; everything else grants (ALLOW)."""
    assert to_ace_type(ace_type) is expected
