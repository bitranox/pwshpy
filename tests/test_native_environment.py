"""Tier-A environment-variable source over os.environ — portable, hermetic."""

from __future__ import annotations

import pytest

from pwshpy.adapters.native import iter_env
from pwshpy.domain.records import EnvVar


@pytest.mark.os_agnostic
def test_iter_env_yields_env_var_records() -> None:
    """Each yielded item is a typed EnvVar."""
    records = list(iter_env())
    assert records
    assert all(isinstance(rec, EnvVar) for rec in records)


@pytest.mark.os_agnostic
def test_iter_env_reflects_a_set_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A variable set in the environment is reflected in the enumeration."""
    monkeypatch.setenv("PWSHPY_TEST_VAR", "sentinel-value")
    matches = {rec.name: rec.value for rec in iter_env() if rec.name == "PWSHPY_TEST_VAR"}
    assert matches == {"PWSHPY_TEST_VAR": "sentinel-value"}
