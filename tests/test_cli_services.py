"""CLI tests for ``get_service``'s ``--strict`` flag, against a fake ps facade.

``os_agnostic`` and hermetic: the facade is faked, so the tests pin the CLI's own contract -
the flag reaches ``ps.get_service``, and a strict failure leaves through ``main()`` as one stderr
line and exit 1 without leaving a bare ``[`` on a ``--json`` stdout.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fake_services import services_with_ps

from pwshpy.adapters.cli.main import main
from pwshpy.composition import AppServices
from pwshpy.domain.enums import ServiceState
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.pipeline import Pipeline
from pwshpy.domain.records import ServiceInfo

_UNREADABLE = "cannot load service unit 'denied.service': systemd LoadUnit failed: access denied"


def _service(name: str) -> ServiceInfo:
    return ServiceInfo(name=name, display_name=name, status=ServiceState.RUNNING)


class _FakePs:
    """Records each get_service call; under strict, raises after ``before`` records."""

    def __init__(self, *, before: int = 0) -> None:
        self.calls: list[bool] = []
        self._before = before

    def get_service(self, *, strict: bool = False) -> Pipeline[ServiceInfo]:
        self.calls.append(strict)
        return Pipeline(self._records(strict=strict))

    def _records(self, *, strict: bool) -> Iterator[ServiceInfo]:
        for index in range(self._before):
            yield _service(f"s{index}.service")
        if strict:
            raise NativeCallError(_UNREADABLE)


def _services(fake: _FakePs) -> AppServices:
    return services_with_ps(fake)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("flags", "strict"), [([], False), (["--strict"], True)])
def test_get_service_forwards_the_strict_choice(
    capsys: pytest.CaptureFixture[str], flags: list[str], *, strict: bool
) -> None:
    """[86] lenient by default; --strict asks the facade to raise on what it cannot read."""
    fake = _FakePs(before=1)
    services = _services(fake)
    rc = main(["get_service", *flags, "--jsonl"], services_factory=lambda: services)
    out = capsys.readouterr().out
    assert fake.calls == [strict]
    assert rc == (1 if strict else 0)
    assert '"s0.service"' in out  # the record before the failure still streamed


@pytest.mark.os_agnostic
def test_a_strict_failure_before_any_record_leaves_json_stdout_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """[86] An unreadable service under --strict exits 1 with one stderr line, and --json writes no bare ``[``."""
    services = _services(_FakePs(before=0))
    rc = main(["get_service", "--strict", "--json"], services_factory=lambda: services)
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.strip() == f"NativeCallError: {_UNREADABLE}"
    assert "Traceback" not in captured.err
