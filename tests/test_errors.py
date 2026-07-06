"""Domain error types: instantiation and message preservation."""

from __future__ import annotations

import pytest

from pwshpy.domain.errors import ConfigurationError


@pytest.mark.os_agnostic
def test_configuration_error_preserves_message() -> None:
    """Instantiation stores the message for display."""
    exc = ConfigurationError("output format not configured")
    assert str(exc) == "output format not configured"
