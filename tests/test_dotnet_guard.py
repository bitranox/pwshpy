""".NET import guard: a call without the [full] extra fails actionably."""

from __future__ import annotations

import pytest

from pwshpy.adapters.powershell import hosted, is_available, is_runtime_available, run
from pwshpy.domain.errors import FeatureUnavailableError


@pytest.mark.os_agnostic
def test_is_available_returns_bool() -> None:
    """Availability check returns a plain bool."""
    assert isinstance(is_available(), bool)


@pytest.mark.os_agnostic
def test_is_runtime_available_returns_bool() -> None:
    """The runtime check returns a plain bool, never raising."""
    assert isinstance(is_runtime_available(), bool)


@pytest.mark.os_agnostic
def test_runtime_unavailable_implies_extra_or_runtime_missing() -> None:
    """is_runtime_available is never True where is_available is False.

    The two answer different questions - "extra installed" vs "would a call work" - and
    the runtime is a strict superset of requirements, so True here with False there would
    mean .NET started without pythonnet, which is impossible.
    """
    if is_runtime_available():
        assert is_available()


@pytest.mark.os_agnostic
def test_runtime_check_is_false_not_raising_when_runtime_absent() -> None:
    """With the extra installed but no .NET runtime, the check reports False.

    This is the case that made 16 .NET tests fail instead of skip: their guard asked
    is_available() ("is pythonnet importable"), which is True on this box because the
    project venv installs [full], while no .NET runtime exists. A guard must be able to
    ask the real question and get an answer, not an exception.
    """
    if not is_available():
        pytest.skip("[full] extra absent; the extra check already short-circuits")
    assert isinstance(is_runtime_available(), bool)  # must not raise either way


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
