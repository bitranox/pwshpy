"""Native services via systemd D-Bus: pure mapping (os_agnostic) + a live read (Linux).

The ActiveState/UnitFileState mappings are pure functions, so they run on every OS, and so do
the listing and read-back tests over the fake system bus (``systemd_fake_bus``). The live D-Bus
read needs Linux + jeepney; it self-skips otherwise (so a CI lane without jeepney skips rather
than errors).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import time
from typing import Any

import pytest
from systemd_fake_bus import FakeConn, use_bus

from pwshpy.adapters.native import systemd_dbus, systemd_services
from pwshpy.adapters.native.systemd_services import (
    SystemdServiceController,
    iter_services,
    to_service_state,
    to_start_type,
)
from pwshpy.domain.enums import ServiceStartType, ServiceState
from pwshpy.domain.errors import NativeCallError, OperationTimeoutError
from pwshpy.domain.records import ServiceInfo

_HAS_JEEPNEY = importlib.util.find_spec("jeepney") is not None


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("active_state", "state"),
    [
        ("active", ServiceState.RUNNING),
        ("inactive", ServiceState.STOPPED),
        ("failed", ServiceState.STOPPED),
        ("activating", ServiceState.START_PENDING),
        ("deactivating", ServiceState.STOP_PENDING),
        ("reloading", ServiceState.CONTINUE_PENDING),
        ("nonsense", ServiceState.STOPPED),
    ],
)
def test_active_state_maps(active_state: str, state: ServiceState) -> None:
    """systemd ActiveState maps to the canonical ServiceState (unknown -> Stopped)."""
    assert to_service_state(active_state) is state


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("unit_file_state", "start_type"),
    [
        ("enabled", ServiceStartType.AUTOMATIC),
        ("enabled-runtime", ServiceStartType.AUTOMATIC),
        ("disabled", ServiceStartType.DISABLED),
        ("masked", ServiceStartType.DISABLED),
        ("static", ServiceStartType.MANUAL),
        ("indirect", ServiceStartType.MANUAL),
    ],
)
def test_unit_file_state_maps(unit_file_state: str, start_type: ServiceStartType) -> None:
    """systemd UnitFileState maps to a ServiceStartType."""
    assert to_start_type(unit_file_state) is start_type


@pytest.mark.os_agnostic
def test_unknown_unit_file_state_is_none() -> None:
    """An unrecognized UnitFileState yields None (unknown), not a wrong guess."""
    assert to_start_type("something-new") is None


@pytest.mark.os_linux
@pytest.mark.skipif(not sys.platform.startswith("linux") or not _HAS_JEEPNEY, reason="needs Linux + jeepney")
def test_live_read_yields_services() -> None:
    """A live D-Bus read yields typed .service records (ListUnits is unprivileged)."""
    services = {s.name: s for s in iter_services()}
    assert services
    assert all(isinstance(s, ServiceInfo) for s in services.values())
    assert all(s.name.endswith(".service") for s in services.values())
    assert all(s.service_type is None for s in services.values())  # no Windows ServiceType on Linux


# --- Listing and read-back over the fake system bus ---------------------------------------------

_UNIT = systemd_dbus.UNIT_IFACE
_NOTHING_FAILS: frozenset[str] = frozenset()


def _listing_conn(*, failing: frozenset[str] = frozenset()) -> FakeConn:
    """A box's services as systemd 259 reports them.

    ListUnits: a running service, one ``not-found`` (referenced, no unit file) and one ``masked``.
    Unit files only (unloaded): a stopped disabled service and a static vendor service under
    /usr/lib, plus a masked one, an alias, a template and a stale file whose LoadUnit answers
    ``not-found`` - none of those last four is a service of its own.
    """
    props: dict[tuple[str, str, str], Any] = {
        ("sshd.service", _UNIT, "ActiveState"): "active",
        ("acpid.service", _UNIT, "ActiveState"): "inactive",
        ("acpid.service", _UNIT, "Description"): "ACPI event daemon",
        ("vendor.service", _UNIT, "ActiveState"): "inactive",
        ("vendor.service", _UNIT, "Description"): "Vendor daemon",
    }
    unit_files = frozenset(
        {
            "sshd.service",
            "acpid.service",
            "vendor.service",
            "veiled.service",
            "gone.service",
            "alias.service",
            "getty@.service",
            "stale.service",
            "job.timer",
        }
    )
    return FakeConn(
        [
            ("sshd.service", "OpenSSH server", "active"),
            ("ghost.service", "ghost.service", "inactive"),
            ("veiled.service", "veiled.service", "inactive"),
            ("job.timer", "A timer", "active"),
        ],
        props,
        unit_files,
        file_states={
            "acpid.service": "disabled",
            "vendor.service": "static",
            "veiled.service": "masked",
            "gone.service": "masked",
            "alias.service": "alias",
        },
        failing=failing,
        unloaded=frozenset({"acpid.service", "vendor.service"}),
        load_states={"ghost.service": "not-found", "veiled.service": "masked", "stale.service": "not-found"},
        file_dirs={"vendor.service": "/usr/lib/systemd/system"},
    )


@pytest.mark.os_agnostic
@pytest.mark.parametrize("failing", [_NOTHING_FAILS, frozenset({"ListUnitFilesByPatterns"})])
def test_iter_services_lists_a_stopped_disabled_service(
    monkeypatch: pytest.MonkeyPatch, failing: frozenset[str]
) -> None:
    """A stopped, disabled service is unloaded, so ListUnits misses it; its unit file lists it.

    Like Get-Service listing a stopped service. Each service once (no duplicate for the loaded
    one); a not-found or masked unit, an alias, a template and a stale file are no services. The
    second case is a systemd without ListUnitFilesByPatterns (before 230), where the full
    ListUnitFiles is filtered instead.
    """
    conn = _listing_conn(failing=failing)
    use_bus(monkeypatch, conn)
    sshd, acpid, vendor = list(iter_services())
    assert (sshd.name, sshd.display_name, sshd.status) == ("sshd.service", "OpenSSH server", ServiceState.RUNNING)
    assert (acpid.name, acpid.display_name) == ("acpid.service", "ACPI event daemon")
    assert (acpid.status, acpid.start_type) == (ServiceState.STOPPED, ServiceStartType.DISABLED)
    # A vendor unit under /usr/lib is a real service too; static has no Windows twin and reads Manual.
    assert (vendor.name, vendor.display_name, vendor.start_type) == (
        "vendor.service",
        "Vendor daemon",
        ServiceStartType.MANUAL,
    )
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_iter_services_skips_a_not_found_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ListUnits entry whose LoadState is not-found names a service with no unit file: not listed.

    Get-Service lists no service that does not exist; systemd keeps such a name loaded only
    because another unit references it, and its Description reads back as the bare name.
    """
    conn = FakeConn(
        [("ghost.service", "ghost.service", "inactive"), ("real.service", "Real", "active")],
        {("real.service", _UNIT, "ActiveState"): "active"},
        frozenset({"real.service"}),
        load_states={"ghost.service": "not-found"},
    )
    use_bus(monkeypatch, conn)
    assert [s.name for s in iter_services()] == ["real.service"]


@pytest.mark.os_agnostic
def test_iter_services_reads_each_unloaded_service_with_one_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unloaded service costs one LoadUnit and no GetUnitFileState: the file listing has its state."""
    conn = _listing_conn()
    use_bus(monkeypatch, conn)
    list(iter_services())
    assert "GetUnit" not in conn.methods
    # acpid, vendor and the stale file are read via LoadUnit; masked, alias and template files never are.
    assert conn.methods.count("LoadUnit") == 3
    assert conn.methods.count("GetUnitFileState") == 1  # only the loaded sshd


@pytest.mark.os_agnostic
def test_iter_services_still_lists_loaded_services_when_no_unit_file_listing_answers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail-soft: with both unit-file listings refused, the loaded services are still listed (and it is logged)."""
    conn = _listing_conn(failing=frozenset({"ListUnitFilesByPatterns", "ListUnitFiles"}))
    use_bus(monkeypatch, conn)
    with caplog.at_level(logging.DEBUG, logger=systemd_services.__name__):
        assert [s.name for s in iter_services()] == ["sshd.service"]
    assert any("unit files" in r.getMessage() for r in caplog.records)


@pytest.mark.os_agnostic
def test_iter_services_lists_unit_files_only_when_the_loaded_services_are_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lazy: taking the first (loaded) service costs no unit-file listing and no LoadUnit."""
    conn = _listing_conn()
    use_bus(monkeypatch, conn)
    first = next(iter_services())
    assert first.name == "sshd.service"
    assert "ListUnitFilesByPatterns" not in conn.methods
    assert "ListUnitFiles" not in conn.methods
    assert "LoadUnit" not in conn.methods


def _acpid_conn() -> FakeConn:
    """One stopped, disabled, unloaded service, as a real host holds it after Stop-Service or a disable."""
    props: dict[tuple[str, str, str], Any] = {
        ("acpid.service", _UNIT, "ActiveState"): "inactive",
        ("acpid.service", _UNIT, "Description"): "ACPI event daemon",
    }
    return FakeConn(
        [],
        props,
        frozenset({"acpid.service"}),
        file_states={"acpid.service": "disabled"},
        unloaded=frozenset({"acpid.service"}),
    )


@pytest.mark.os_agnostic
def test_set_startup_reads_back_an_unloaded_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling a stopped service leaves it unloaded; the read-back still names it (LoadUnit, not GetUnit)."""
    conn = _acpid_conn()
    use_bus(monkeypatch, conn)
    info = SystemdServiceController().set_startup("acpid.service", ServiceStartType.DISABLED)
    assert conn.verbs == ["DisableUnitFiles", "Reload"]
    assert (info.display_name, info.status, info.start_type) == (
        "ACPI event daemon",
        ServiceState.STOPPED,
        ServiceStartType.DISABLED,
    )
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_stop_reads_back_a_service_that_unloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stopped service unloads; the post-stop read-back still carries its description."""
    conn = _acpid_conn()
    use_bus(monkeypatch, conn)
    info = SystemdServiceController().stop("acpid.service", timeout=1.0)
    assert conn.verbs == ["StopUnit"]
    assert (info.name, info.display_name, info.status) == ("acpid.service", "ACPI event daemon", ServiceState.STOPPED)


@pytest.mark.os_agnostic
def test_read_back_of_a_not_found_name_reports_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """LoadUnit answers a name with no unit file (not-found); its Description, the bare name, is not reported."""
    conn = FakeConn([], {})
    use_bus(monkeypatch, conn)
    info = SystemdServiceController().stop("ghost.service", timeout=1.0)
    assert (info.display_name, info.status, info.start_type) == ("", ServiceState.STOPPED, None)


def _orphan_service_conn(load_state: str, active_state: str) -> FakeConn:
    """A loaded ``busy.service`` whose ListUnits LoadState is ``load_state`` while it is ``active_state``.

    not-found: its unit file was deleted under it (no file, GetUnitFileState refuses). masked: a
    ``systemctl mask`` without ``--now`` left it running (its file state reads masked).
    """
    masked = load_state == "masked"
    return FakeConn(
        [("busy.service", "Busy daemon", active_state)],
        {("busy.service", _UNIT, "ActiveState"): active_state},
        frozenset({"busy.service"}) if masked else frozenset(),
        file_states={"busy.service": "masked"} if masked else None,
        load_states={"busy.service": load_state},
    )


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("load_state", "start_type"), [("not-found", None), ("masked", ServiceStartType.DISABLED)])
@pytest.mark.parametrize(
    ("active_state", "status"),
    [
        ("active", ServiceState.RUNNING),
        ("activating", ServiceState.START_PENDING),
        ("deactivating", ServiceState.STOP_PENDING),
        ("reloading", ServiceState.CONTINUE_PENDING),
    ],
)
def test_iter_services_lists_a_running_service_whose_unit_is_gone_or_masked(
    monkeypatch: pytest.MonkeyPatch,
    load_state: str,
    start_type: ServiceStartType | None,
    active_state: str,
    status: ServiceState,
) -> None:
    """Get-Service keeps listing a running service marked for deletion, and a masked one as Disabled/Running."""
    conn = _orphan_service_conn(load_state, active_state)
    use_bus(monkeypatch, conn)
    services = list(iter_services())
    assert [(s.name, s.display_name, s.status, s.start_type) for s in services] == [
        ("busy.service", "Busy daemon", status, start_type)
    ]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("load_state", ["not-found", "masked"])
@pytest.mark.parametrize("active_state", ["inactive", "failed"])
def test_iter_services_skips_a_stopped_service_whose_unit_is_gone_or_masked(
    monkeypatch: pytest.MonkeyPatch, load_state: str, active_state: str
) -> None:
    """Stopped, a not-found or masked service is no service: only a reference or an unstartable name is left."""
    conn = _orphan_service_conn(load_state, active_state)
    use_bus(monkeypatch, conn)
    assert list(iter_services()) == []


@pytest.mark.os_agnostic
def test_iter_services_lists_a_cached_not_found_service_whose_file_appeared(monkeypatch: pytest.MonkeyPatch) -> None:
    """systemd cached the name as not-found, then its file was written without a daemon-reload.

    ListUnits still reports the cached not-found, so that pass skips it; the unit-file pass must
    still list it, read through LoadUnit, which reloads it from the new file.
    """
    props: dict[tuple[str, str, str], Any] = {
        ("late.service", _UNIT, "ActiveState"): "inactive",
        ("late.service", _UNIT, "Description"): "Late daemon",
    }
    conn = FakeConn(
        [("late.service", "late.service", "inactive")],
        props,
        frozenset({"late.service"}),
        file_states={"late.service": "disabled"},
        load_states={"late.service": "not-found"},
        appeared=frozenset({"late.service"}),
    )
    use_bus(monkeypatch, conn)
    assert [(s.name, s.display_name, s.start_type) for s in iter_services()] == [
        ("late.service", "Late daemon", ServiceStartType.DISABLED)
    ]


@pytest.mark.os_agnostic
def test_read_back_of_a_running_not_found_service_reports_its_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """LoadUnit reads a service whose file was deleted under it as not-found, yet it still runs.

    The read-back after a stop that has not settled yet must say so, not report Stopped.
    """
    conn = _orphan_service_conn("not-found", "deactivating")
    use_bus(monkeypatch, conn)
    info = SystemdServiceController().stop("busy.service", wait=False)
    assert conn.verbs == ["StopUnit"]
    assert (info.name, info.status) == ("busy.service", ServiceState.STOP_PENDING)


def _settling_conn(
    active_state: str, *, failing: frozenset[str] = frozenset(), walks: dict[str, list[str]] | None = None
) -> FakeConn:
    """A loaded service in ``active_state``; frozen there (like a hung stop) unless ``walks`` moves it."""
    props: dict[tuple[str, str, str], Any] = {
        ("wuauserv.service", _UNIT, "ActiveState"): active_state,
        ("wuauserv.service", _UNIT, "Description"): "Update daemon",
    }
    return FakeConn([], props, frozenset({"wuauserv.service"}), failing=failing, walks=walks)


@pytest.mark.os_agnostic
def test_stop_without_wait_returns_the_state_right_after_stop_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """wait=False sends StopUnit and reads the unit back once, never polling it to inactive."""
    conn = _settling_conn("deactivating")
    use_bus(monkeypatch, conn)
    started = time.monotonic()
    info = SystemdServiceController().stop("wuauserv.service", wait=False)
    # The default 30 s timeout would be spent polling a unit that never leaves deactivating.
    assert time.monotonic() - started < 5
    assert conn.verbs == ["StopUnit"]
    assert info.status is ServiceState.STOP_PENDING
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_a_waiting_stop_that_stays_deactivating_raises_operation_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deadline raises its own type, as on Windows, instead of returning the unsettled state."""
    conn = _settling_conn("deactivating")
    use_bus(monkeypatch, conn)
    with pytest.raises(OperationTimeoutError) as caught:
        SystemdServiceController().stop("wuauserv.service", timeout=0.3)
    assert caught.value.timeout == 0.3
    assert conn.closed is True


@pytest.mark.os_agnostic
def test_a_waiting_start_that_stays_activating_raises_operation_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """start and restart share the wait, so their deadline is the same distinct error."""
    use_bus(monkeypatch, _settling_conn("activating"))
    with pytest.raises(OperationTimeoutError) as caught:
        SystemdServiceController().start("wuauserv.service", timeout=0.3)
    assert caught.value.timeout == 0.3


@pytest.mark.os_agnostic
@pytest.mark.parametrize("wait", [True, False])
def test_a_refused_stop_unit_is_native_call_error_not_a_timeout(monkeypatch: pytest.MonkeyPatch, *, wait: bool) -> None:
    """A StopUnit systemd refuses stays a plain NativeCallError in both forms."""
    use_bus(monkeypatch, _settling_conn("active", failing=frozenset({"StopUnit"})))
    with pytest.raises(NativeCallError) as caught:
        SystemdServiceController().stop("wuauserv.service", wait=wait)
    assert not isinstance(caught.value, OperationTimeoutError)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("method", "verb", "walk"),
    [
        ("start", "StartUnit", ["activating", "activating", "inactive"]),
        ("restart", "RestartUnit", ["deactivating", "activating", "inactive"]),
    ],
)
def test_a_start_whose_job_ends_inactive_returns_promptly(
    monkeypatch: pytest.MonkeyPatch, method: str, verb: str, walk: list[str]
) -> None:
    """A Type=oneshot unit without RemainAfterExit (or a simple one exiting 0) ends inactive.

    Its job is done once the unit leaves activating, so the start is settled there and reports
    the state it reached instead of waiting for an active that never comes.
    """
    conn = _settling_conn("inactive", walks={verb: walk})
    use_bus(monkeypatch, conn)
    started = time.monotonic()
    info = getattr(SystemdServiceController(), method)("wuauserv.service", timeout=3.0)
    assert time.monotonic() - started < 2
    assert info.status is ServiceState.STOPPED
    assert conn.verbs == [verb]


@pytest.mark.os_agnostic
def test_a_restart_waits_for_its_job_not_for_the_active_state_it_started_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """Right after RestartUnit an active unit still reads active; the restart is settled only when its job is."""
    conn = _settling_conn("active", walks={"RestartUnit": ["active", "deactivating", "activating", "active"]})
    use_bus(monkeypatch, conn)
    info = SystemdServiceController().restart("wuauserv.service", timeout=3.0)
    assert info.status is ServiceState.RUNNING
    # Every step of the walk was read: the wait did not stop at the leading 'active'.
    assert conn.steps_ahead("wuauserv.service") == ["active"]


@pytest.mark.os_agnostic
def test_a_stop_settles_when_its_job_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """A waiting stop of a deactivating unit returns Stopped once the stop job is done."""
    use_bus(monkeypatch, _settling_conn("active", walks={"StopUnit": ["deactivating", "deactivating", "inactive"]}))
    assert SystemdServiceController().stop("wuauserv.service", timeout=3.0).status is ServiceState.STOPPED
