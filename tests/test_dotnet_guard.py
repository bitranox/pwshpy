""".NET import guard: a call without the [full] extra fails actionably."""

from __future__ import annotations

import pytest

from pwshpy.adapters.powershell import hosted, is_available, run
from pwshpy.domain.errors import FeatureUnavailableError


@pytest.mark.os_agnostic
def test_is_available_returns_bool() -> None:
    """Availability check returns a plain bool."""
    assert isinstance(is_available(), bool)


@pytest.mark.os_agnostic
def test_run_raises_feature_unavailable_when_extra_missing() -> None:
    """Without the [full] extra, run raises FeatureUnavailableError naming the fix."""
    if is_available():
        pytest.skip("[full] extra is installed; guard path not exercised")
    with pytest.raises(FeatureUnavailableError) as excinfo:
        run("Get-Process")
    assert "pwshpy[full]" in str(excinfo.value)


@pytest.mark.os_agnostic
def test_importing_tier_b_loads_no_dotnet() -> None:
    """Importing the .NET adapter must not pull .NET into the process."""
    import sys

    # A base install carries no CLR; importing hosted must not have loaded it.
    assert hosted is not None
    assert "clr" not in sys.modules
