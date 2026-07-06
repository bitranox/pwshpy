"""The ``ps`` facade — the public entry point wiring adapters to the pipeline.

``ps`` is where library and CLI meet: both call the same facade, so they never
diverge.  Tier-A sources return a :class:`~pwshpy.domain.pipeline.Pipeline`;
``ps.run`` reaches Tier B (hosted PowerShell) behind the ``[full]`` extra.

Contents:
    * :class:`Ps` — the facade binding a process source + PowerShell runner.
    * :data:`ps` — the default production facade (``from pwshpy import ps``).
    * :func:`build_ps` — construct a facade from explicit adapters (for tests).
"""

from __future__ import annotations

from typing import Any

from ..adapters.native import get_uptime as _get_uptime
from ..adapters.native import iter_connections as _iter_connections
from ..adapters.native import iter_disks as _iter_disks
from ..adapters.native import iter_env as _iter_env
from ..adapters.native import iter_processes as _iter_processes
from ..adapters.native import resolve as _resolve
from ..adapters.native import test_connection as _test_connection
from ..adapters.powershell import run as _ps_run
from ..application.ports import (
    ConnectionTester,
    DiskSource,
    DnsResolver,
    EnvironmentSource,
    NetConnectionSource,
    PowerShellRunner,
    ProcessSource,
    UptimeSource,
)
from ..domain.pipeline import Pipeline
from ..domain.records import (
    ConnectionTest,
    DiskUsage,
    DnsRecord,
    EnvVar,
    NetConnection,
    ProcessInfo,
    SystemUptime,
)


class Ps:
    """Fluent facade over both tiers, returning the same typed pipeline.

    Example:
        >>> facade = build_ps()
        >>> facade.processes().take(1).to_list()  # doctest: +ELLIPSIS
        [ProcessInfo(...)]
    """

    def __init__(
        self,
        *,
        process_source: ProcessSource,
        connection_source: NetConnectionSource,
        disk_source: DiskSource,
        uptime_source: UptimeSource,
        dns_resolver: DnsResolver,
        connection_tester: ConnectionTester,
        environment_source: EnvironmentSource,
        ps_runner: PowerShellRunner,
    ) -> None:
        self._process_source = process_source
        self._connection_source = connection_source
        self._disk_source = disk_source
        self._uptime_source = uptime_source
        self._dns_resolver = dns_resolver
        self._connection_tester = connection_tester
        self._environment_source = environment_source
        self._ps_runner = ps_runner

    def processes(self) -> Pipeline[ProcessInfo]:
        """Return a lazy pipeline over the running processes (Tier A).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().processes(), Pipeline)
            True
        """
        return Pipeline(self._process_source())

    def connections(self) -> Pipeline[NetConnection]:
        """Return a lazy pipeline over the open network connections (Tier A).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().connections(), Pipeline)
            True
        """
        return Pipeline(self._connection_source())

    def disks(self) -> Pipeline[DiskUsage]:
        """Return a lazy pipeline over the mounted filesystems (Tier A).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().disks(), Pipeline)
            True
        """
        return Pipeline(self._disk_source())

    def uptime(self) -> SystemUptime:
        """Return the system boot time and elapsed uptime (Tier A).

        Example:
            >>> from pwshpy.domain.records import SystemUptime
            >>> isinstance(build_ps().uptime(), SystemUptime)
            True
        """
        return self._uptime_source()

    def resolve(self, name: str, *, timeout: float | None = None) -> Pipeline[DnsRecord]:
        """Return a lazy pipeline over the addresses ``name`` resolves to (Tier A).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().resolve("localhost"), Pipeline)
            True
        """
        return Pipeline(self._dns_resolver(name, timeout=timeout))

    def test_connection(self, host: str, *, port: int = 443, timeout: float = 5.0) -> ConnectionTest:
        """Probe TCP reachability of ``host:port`` within ``timeout`` seconds (Tier A).

        Example:
            >>> from pwshpy.domain.records import ConnectionTest
            >>> isinstance(build_ps().test_connection("127.0.0.1", port=1), ConnectionTest)
            True
        """
        return self._connection_tester(host, port=port, timeout=timeout)

    def environment(self) -> Pipeline[EnvVar]:
        """Return a lazy pipeline over the process environment variables (Tier A).

        Example:
            >>> from pwshpy.domain.pipeline import Pipeline
            >>> isinstance(build_ps().environment(), Pipeline)
            True
        """
        return Pipeline(self._environment_source())

    def run(self, script: str, *, timeout: float | None = None) -> list[Any]:
        """Execute a PowerShell script in the hosted engine (Tier B).

        Raises :class:`~pwshpy.domain.errors.FeatureUnavailableError` when the
        ``[full]`` extra is not installed.

        Example:
            >>> build_ps().run("Get-Process")  # doctest: +IGNORE_EXCEPTION_DETAIL
            Traceback (most recent call last):
            ...
            FeatureUnavailableError: ...
        """
        return self._ps_runner(script, timeout=timeout)


def build_ps() -> Ps:
    """Construct a facade wired with the production adapters.

    Example:
        >>> isinstance(build_ps(), Ps)
        True
    """
    return Ps(
        process_source=_iter_processes,
        connection_source=_iter_connections,
        disk_source=_iter_disks,
        uptime_source=_get_uptime,
        dns_resolver=_resolve,
        connection_tester=_test_connection,
        environment_source=_iter_env,
        ps_runner=_ps_run,
    )


#: Default production facade for ergonomic ``from pwshpy import ps`` usage.
ps = build_ps()


__all__ = ["Ps", "build_ps", "ps"]
