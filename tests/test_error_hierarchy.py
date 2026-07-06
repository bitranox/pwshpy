"""The PwshPyError hierarchy: rooting, catchability, and messages."""

from __future__ import annotations

import pytest

from pwshpy.domain.errors import (
    ConfigurationError,
    FeatureUnavailableError,
    NativeCallError,
    PlatformUnsupportedError,
    PowerShellError,
    PwshPyError,
)

_SUBCLASSES = [
    NativeCallError,
    PowerShellError,
    FeatureUnavailableError,
    PlatformUnsupportedError,
    ConfigurationError,
]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("error_type", _SUBCLASSES)
def test_all_errors_root_at_pwshpy_error(error_type: type[PwshPyError]) -> None:
    """Every pwshpy error is catchable as PwshPyError."""
    assert issubclass(error_type, PwshPyError)
    with pytest.raises(PwshPyError):
        raise error_type("boom")


@pytest.mark.os_agnostic
def test_feature_unavailable_message_is_preserved() -> None:
    """FeatureUnavailableError preserves its actionable message."""
    exc = FeatureUnavailableError("pip install pwshpy[full]")
    assert "pwshpy[full]" in str(exc)


@pytest.mark.os_agnostic
def test_pwshpy_error_is_an_exception() -> None:
    """The root inherits from the builtin Exception."""
    assert issubclass(PwshPyError, Exception)
