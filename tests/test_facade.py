"""The ps facade: pipeline wiring and Tier-B delegation."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from pwshpy import ps
from pwshpy.composition import Ps, build_ps
from pwshpy.domain.errors import FeatureUnavailableError
from pwshpy.domain.pipeline import Pipeline
from pwshpy.domain.records import (
    ConnectionTest,
    DiskUsage,
    DnsRecord,
    EnvVar,
    NetConnection,
    ProcessInfo,
    SystemUptime,
)


@pytest.mark.os_agnostic
def test_default_ps_is_a_facade() -> None:
    """The module-level ps is a wired Ps facade."""
    assert isinstance(ps, Ps)


@pytest.mark.os_agnostic
def test_processes_returns_a_pipeline_of_records() -> None:
    """ps.processes() returns a Pipeline yielding ProcessInfo records."""
    pipeline = build_ps().processes()
    assert isinstance(pipeline, Pipeline)
    assert isinstance(pipeline.first(), ProcessInfo)


@pytest.mark.os_agnostic
def test_connections_returns_a_pipeline() -> None:
    """ps.connections() returns a Pipeline (possibly empty in sandboxes)."""
    pipeline = build_ps().connections()
    assert isinstance(pipeline, Pipeline)
    for conn in pipeline.take(1):
        assert isinstance(conn, NetConnection)


@pytest.mark.os_agnostic
def test_disks_returns_a_pipeline_of_records() -> None:
    """ps.disks() returns a Pipeline yielding DiskUsage records."""
    pipeline = build_ps().disks()
    assert isinstance(pipeline, Pipeline)
    assert isinstance(pipeline.first(), DiskUsage)


@pytest.mark.os_agnostic
def test_processes_is_independently_consumable_per_call() -> None:
    """Each processes() call hands out a fresh, independently consumable pipeline."""
    facade = build_ps()
    assert facade.processes().take(1).to_list()
    assert facade.processes().take(1).to_list()


@pytest.mark.os_agnostic
def test_run_delegates_to_tier_b_guard() -> None:
    """ps.run without the [full] extra surfaces FeatureUnavailableError."""
    from pwshpy.adapters.powershell import is_available

    if is_available():
        pytest.skip("[full] extra is installed; guard path not exercised")
    with pytest.raises(FeatureUnavailableError):
        build_ps().run("Get-Process")


@pytest.mark.os_agnostic
def test_build_ps_accepts_injected_adapters() -> None:
    """Ps composes over injected adapters (Testing-API style)."""
    fake_records = [ProcessInfo(pid=1, name="a"), ProcessInfo(pid=2, name="b")]

    def fake_source() -> Iterator[ProcessInfo]:
        yield from fake_records

    def fake_connections() -> Iterator[NetConnection]:
        yield from ()

    def fake_disks() -> Iterator[DiskUsage]:
        yield from ()

    def fake_uptime() -> SystemUptime:
        return SystemUptime(boot_time=datetime(2020, 1, 1, tzinfo=UTC), uptime_seconds=42.0)

    def fake_resolver(name: str, *, timeout: float | None = None) -> Iterator[DnsRecord]:
        yield from ()

    def fake_tester(host: str, *, port: int = 443, timeout: float = 5.0) -> ConnectionTest:
        return ConnectionTest(host=host, port=port, reachable=True, latency_ms=1.0)

    def fake_env() -> Iterator[EnvVar]:
        yield EnvVar(name="FOO", value="bar")

    def fake_runner(script: str, *, timeout: float | None = None) -> list[object]:
        return ["ran", script, timeout]

    facade = Ps(
        process_source=fake_source,
        connection_source=fake_connections,
        disk_source=fake_disks,
        uptime_source=fake_uptime,
        dns_resolver=fake_resolver,
        connection_tester=fake_tester,
        environment_source=fake_env,
        ps_runner=fake_runner,
    )
    assert facade.processes().select(lambda p: p.pid).to_list() == [1, 2]
    assert facade.connections().to_list() == []
    assert facade.disks().to_list() == []
    assert facade.uptime().uptime_seconds == 42.0
    assert facade.resolve("host").to_list() == []
    assert facade.test_connection("h", port=22).reachable is True
    assert facade.environment().select(lambda e: e.name).to_list() == ["FOO"]
    assert facade.run("x", timeout=1.0) == ["ran", "x", 1.0]
