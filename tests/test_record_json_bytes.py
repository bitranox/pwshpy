"""Record JSON bytes: ``model_dump_json()`` against the ``orjson.dumps(to_dict())`` chain.

``write_records`` and the CLI's ``--json`` / ``--jsonl`` serialise a record through
``orjson.dumps(record.to_dict())``. Those bytes are a public contract (JSONL files, piped CLI
output), so replacing the chain with Pydantic's own ``model_dump_json()`` is only safe if the
two agree for every record and every value a record can carry.

They agree on the bytes of every ordinary record: one representative instance per concrete
:class:`PSRecord` subclass below, with aware and naive datetimes, non-ASCII, emoji and control
characters, awkward floats (0.1, 1e20, inf, nan, -0.0), 64-bit boundary integers, empty
containers, nested records, enums and ``SecretStr``. They do NOT agree at the edges pinned by
the ``test_differs_*`` tests, which is why both call sites keep the chain:

* an integer outside orjson's range (below -2**63 or above 2**64-1), in an ``int`` field or in
  an open ``properties`` / ``output`` bag: the chain raises ``orjson.JSONEncodeError`` (a
  ``TypeError``), ``model_dump_json()`` writes the number;
* a lone surrogate in a string (a non-UTF-8 environment variable on POSIX decodes to one):
  both raise, but the chain raises ``orjson.JSONEncodeError`` (a ``TypeError``) and
  ``model_dump_json()`` raises ``PydanticSerializationError`` (a ``ValueError``), with a
  different message;
* non-UTF-8 ``bytes`` in an open bag: the chain raises ``UnicodeDecodeError`` from ``to_dict``,
  ``model_dump_json()`` raises ``PydanticSerializationError``.

Each is a change to what a caller sees (an error turning into output, or one exception type
and message turning into another), so the switch waits until those are decided on purpose.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import orjson
import pytest
from pydantic import SecretStr
from pydantic_core import PydanticSerializationError

from pwshpy.adapters.cli.output import RecordFormat, emit
from pwshpy.adapters.native.fileio import write_records
from pwshpy.domain import records as records_module
from pwshpy.domain.enums import (
    AceType,
    AclEntryKind,
    AddressFamily,
    ConnectionState,
    EventLevel,
    ImageArchitecture,
    IntegrityLevel,
    ProcessStatus,
    RegistryHive,
    RegistryValueType,
    ServiceKind,
    ServiceStartType,
    ServiceState,
    TaskRunLevel,
    TaskState,
    TransportProtocol,
)
from pwshpy.domain.records import (
    AclEntry,
    CimInstance,
    CommandInfo,
    CommandParameter,
    ComputerInfo,
    ConnectionTest,
    Credential,
    CurrentIdentity,
    DiskUsage,
    DnsRecord,
    EnvVar,
    EventLogEntry,
    FileSystemItem,
    Hotfix,
    LocalGroup,
    LocalUser,
    NetAdapter,
    NetConnection,
    NetIpAddress,
    PackedScript,
    ProcessInfo,
    ProcessResult,
    PSInvocationResult,
    PSObjectRecord,
    PSRecord,
    RegistryKey,
    RegistryValue,
    ScheduledTaskInfo,
    ServiceInfo,
    ShortcutInfo,
    SystemUptime,
    UpdateCategory,
    WebResponse,
    WindowsImage,
    WindowsUpdate,
)

# Built from code points so this file stays ASCII: e-acute, a grinning-face emoji (outside the
# BMP), two CJK ideographs, U+2028 (JSON-legal raw, JavaScript-illegal), a quote, a backslash,
# a slash, NUL, a unit separator and DEL - every character class an encoder may escape differently.
_TEXT = (
    "caf"
    + chr(0xE9)
    + " "
    + chr(0x1F600)
    + " "
    + chr(0x4E2D)
    + chr(0x6587)
    + chr(0x2028)
    + ' "q" \\ / '
    + chr(0)
    + chr(0x1F)
    + chr(0x7F)
)
_AWARE = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
_OFFSET = datetime(2026, 1, 2, 3, 4, 5, 123, tzinfo=timezone(timedelta(hours=2, minutes=30)))
_NAIVE = datetime(2026, 1, 2, 3, 4, 5)
_U64_MAX = 2**64 - 1
_I64_MIN = -(2**63)


def _open_bag() -> dict[str, Any]:
    """A heterogeneous property bag as CIM/WMI or a marshaled PSObject carries it."""
    return {
        "text": _TEXT,
        "float": 0.1,
        "big_float": 1e20,
        "inf": float("inf"),
        "nan": float("nan"),
        "neg_zero": -0.0,
        "u64_max": _U64_MAX,
        "i64_min": _I64_MIN,
        "none": None,
        "bool": True,
        "empty_list": [],
        "empty_dict": {},
        "tuple": (1, "a"),
        "set": {3},
        "nested": {"list": [1, {"deep": None}], "key": _TEXT},
        "aware": _AWARE,
        "naive": _NAIVE,
        "date": date(2026, 1, 2),
        "timedelta": timedelta(days=-1, seconds=5, microseconds=7),
        "decimal": Decimal("1.10"),
        "native_path": Path("rel") / "x.txt",
        "uuid": UUID(int=5),
        "bytes": b"ab",
        "enum": ServiceState.RUNNING,
        "record": EnvVar(name="N", value=_TEXT),
        "secret": SecretStr("hunter2"),
        "int_keys": {1: "one", None: "none"},
    }


_RECORDS: list[PSRecord] = [
    ProcessInfo(
        pid=1,
        ppid=0,
        name=_TEXT,
        status=ProcessStatus.SLEEPING,
        username="root",
        memory_rss=_U64_MAX,
        create_time=_AWARE,
    ),
    ProcessInfo(pid=2, name="naive", create_time=_NAIVE),
    ProcessResult(argv=["echo", _TEXT], exit_code=-1, stdout=_TEXT, stderr="", duration_s=0.1),
    Hotfix(hotfix_id="KB5001234", description=_TEXT, installed_on="1/2/2026", installed_by=""),
    ComputerInfo(hostname="pc", os_name=_TEXT, cpu_count=None, total_memory_bytes=17_000_000_000),
    WebResponse(url="http://x/", status_code=200, text=_TEXT, headers={}),
    WebResponse(url="http://x/", status_code=200, headers={"Content-Type": "text/plain", _TEXT: _TEXT}),
    NetAdapter(name="eth0", is_up=True, speed_mbps=None, mtu=1500, mac_address="00:11:22:33:44:55"),
    NetIpAddress(interface="eth0", address="fe80::1", family=AddressFamily.IPV6, netmask=""),
    FileSystemItem(path="/tmp/" + _TEXT, name=_TEXT, is_directory=False, size=0, modified=_OFFSET),
    FileSystemItem(path="/tmp", name="tmp", is_directory=True),
    Credential(username=_TEXT, secret=SecretStr("hunter2"), target="db"),
    Credential(username="u", secret=SecretStr("")),
    NetConnection(
        protocol=TransportProtocol.TCP,
        local_address="127.0.0.1",
        local_port=8080,
        remote_address="::1",
        remote_port=443,
        status=ConnectionState.ESTABLISHED,
        pid=None,
    ),
    DiskUsage(device="C:\\", mountpoint="C:\\", fstype="NTFS", total=_U64_MAX, used=0, free=_U64_MAX, percent=0.1),
    DiskUsage(device="d", mountpoint="/", fstype="x", total=1, used=1, free=0, percent=1e20),
    DiskUsage(device="d", mountpoint="/", fstype="x", total=1, used=1, free=0, percent=float("nan")),
    DiskUsage(device="d", mountpoint="/", fstype="x", total=1, used=1, free=0, percent=float("inf")),
    DiskUsage(device="d", mountpoint="/", fstype="x", total=1, used=1, free=0, percent=-0.0),
    SystemUptime(boot_time=_AWARE, uptime_seconds=1 / 3),
    SystemUptime(boot_time=_NAIVE, uptime_seconds=5e-324),
    DnsRecord(name=_TEXT, address="127.0.0.1", family=AddressFamily.IPV4),
    ConnectionTest(host="localhost", port=80, reachable=False, latency_ms=None, error=_TEXT),
    ConnectionTest(host="localhost", port=80, reachable=True, latency_ms=0.30000000000000004),
    EnvVar(name="PATH", value=_TEXT),
    EnvVar(name="", value=""),
    RegistryKey(hive=RegistryHive.HKLM, key="SOFTWARE\\" + _TEXT, name=_TEXT),
    RegistryValue(hive=RegistryHive.HKCU, key="k", name="", type=RegistryValueType.REG_SZ, data=_TEXT),
    RegistryValue(hive=RegistryHive.HKCU, key="k", name="d", type=RegistryValueType.REG_DWORD, data=_I64_MIN),
    RegistryValue(hive=RegistryHive.HKCU, key="k", name="m", type=RegistryValueType.REG_MULTI_SZ, data=[]),
    RegistryValue(hive=RegistryHive.HKCU, key="k", name="n", type=RegistryValueType.REG_NONE, data=None),
    ServiceInfo(
        name="sshd.service",
        display_name=_TEXT,
        status=ServiceState.RUNNING,
        service_type=ServiceKind.WIN32_SHARE_PROCESS,
        pid=1234,
        can_stop=True,
        start_type=ServiceStartType.AUTOMATIC,
        required_services=["a", _TEXT],
    ),
    ServiceInfo(name="x", display_name="", status=ServiceState.STOPPED),
    ShortcutInfo(
        path="C:\\Users\\me\\Desktop\\" + _TEXT + ".lnk",
        target="C:\\Tools\\tool.exe",
        arguments='--name "' + _TEXT + '"',
        working_directory="C:\\Tools",
        icon="C:\\Tools\\tool.ico",
        icon_index=-(2**31),
        description=_TEXT,
    ),
    ShortcutInfo(path="C:\\a.lnk", target="C:\\t.exe"),
    EventLogEntry(
        log_name="System",
        record_id=_U64_MAX,
        event_id=0,
        level=EventLevel.CRITICAL,
        provider_name=_TEXT,
        time_created=_OFFSET,
        machine_name="host",
        user_id="S-1-5-18",
        message=_TEXT,
    ),
    CimInstance(class_name="Win32_Thing", properties=_open_bag()),
    CimInstance(class_name="Empty"),
    PSObjectRecord(type_name="System.Object", properties=_open_bag()),
    PSInvocationResult(
        output=[42, _TEXT, None, PSObjectRecord(type_name="T", properties={"n": 1}), 0.1, [], {}],
        errors=[_TEXT],
        warnings=[],
        verbose=["v"],
        debug=[],
        information=["i"],
        had_errors=True,
    ),
    CommandParameter(name="Path", type="String", mandatory=True, aliases=["PSPath", _TEXT]),
    CommandInfo(
        name="Get-Item",
        command_type="Cmdlet",
        module="Microsoft.PowerShell.Management",
        parameters=[CommandParameter(name="Path"), CommandParameter(name=_TEXT, aliases=[])],
    ),
    CommandInfo(name="None"),
    ScheduledTaskInfo(
        task_name=_TEXT,
        task_path="\\",
        state=TaskState.READY,
        enabled=False,
        author=_TEXT,
        user_id="S-1-5-18",
        run_level=TaskRunLevel.HIGHEST,
        last_run_time=_AWARE,
        last_task_result=0x8004_1303,
        next_run_time=None,
    ),
    LocalUser(name=_TEXT, sid="S-1-5-21-1-2-3-501", enabled=False, full_name=_TEXT),
    CurrentIdentity(
        sid="S-1-5-18",
        account="NT AUTHORITY\\SYSTEM",
        name="SYSTEM",
        domain="NT AUTHORITY",
        is_system=True,
        elevated=True,
        integrity_level=IntegrityLevel.SYSTEM,
    ),
    CurrentIdentity(sid="0"),
    LocalGroup(name=_TEXT, sid="S-1-5-32-544"),
    AclEntry(
        path="/" + _TEXT,
        owner_sid="0",
        trustee_sid="1000",
        access_type=AceType.ALLOW,
        rights=7,
        trustee_name=_TEXT,
        inherited=True,
        kind=AclEntryKind.MASK,
        permissions="rwx",
    ),
    PackedScript(
        output_path="tool.ps1",
        entry=_TEXT + ".py",
        files=[],
        payload_sha256="ab" * 32,
        payload_bytes=0,
        uv_args=["--with", "x"],
        external_imports=["yaml"],
        has_script_metadata=True,
    ),
    WindowsImage(
        index=1,
        name=_TEXT,
        architecture=ImageArchitecture.ARM64,
        version="10.0.26200.1",
        build=26200,
        languages=("de-DE", "en-US"),
        size_bytes=_U64_MAX,
    ),
    WindowsImage(index=2, name="empty"),
    UpdateCategory(category_id="0fa1201d-4330-4fa8-8ae9-b877473b6441", type="UpdateClassification", name=_TEXT),
    WindowsUpdate(
        update_id="u",
        revision_number=200,
        title=_TEXT,
        kb_article_ids=("5034441",),
        is_installed=True,
        msrc_severity="Critical",
        max_download_size=_U64_MAX,
        categories=(_TEXT,),
        category_details=(UpdateCategory(category_id="c", type="Product", name=_TEXT),),
    ),
    WindowsUpdate(update_id="v", title="t"),
    # The trusted native hot path builds records with model_construct (no validation).
    ProcessInfo.model_construct(pid=3, ppid=None, name="constructed", status=ProcessStatus.RUNNING),
    NetConnection.model_construct(
        protocol=TransportProtocol.UDP, local_address="127.0.0.53", local_port=53, status=ConnectionState.NONE
    ),
]


def _chain(record: PSRecord) -> bytes:
    """The bytes the call sites write today."""
    return orjson.dumps(record.to_dict())


def _concrete_record_classes() -> set[type[PSRecord]]:
    """Every PSRecord subclass defined in the records module (the base itself excluded)."""
    return {
        member
        for _, member in inspect.getmembers(records_module, inspect.isclass)
        if issubclass(member, PSRecord) and member is not PSRecord and member.__module__ == records_module.__name__
    }


@pytest.mark.os_agnostic
def test_every_concrete_record_class_has_a_representative() -> None:
    """A record class added to the domain without a sample here would escape the byte check."""
    covered = {type(record) for record in _RECORDS}
    assert _concrete_record_classes() - covered == set()


@pytest.mark.os_agnostic
@pytest.mark.parametrize("record", _RECORDS, ids=lambda record: type(record).__name__)
def test_model_dump_json_bytes_equal_the_orjson_chain(record: PSRecord) -> None:
    """For every ordinary record the two serialisations produce the same bytes."""
    assert record.model_dump_json().encode() == _chain(record)


@pytest.mark.os_agnostic
def test_secret_is_masked_identically_in_both() -> None:
    """Neither path leaks a SecretStr: both write the same mask."""
    record = Credential(username="u", secret=SecretStr("hunter2"))
    assert b"hunter2" not in record.model_dump_json().encode()
    assert b"hunter2" not in _chain(record)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("value", [2**64, -(2**63) - 1, 10**30], ids=["u64_max_plus_1", "i64_min_minus_1", "1e30"])
def test_differs_on_an_integer_outside_orjson_range(value: int) -> None:
    """The chain refuses an integer orjson cannot hold; model_dump_json writes it."""
    for record in (
        CimInstance(class_name="c", properties={"v": value}),
        DiskUsage(device="d", mountpoint="/", fstype="x", total=value, used=0, free=0, percent=0.0),
    ):
        assert str(value).encode() in record.model_dump_json().encode()
        with pytest.raises(orjson.JSONEncodeError, match="Integer exceeds 64-bit range"):
            _chain(record)


@pytest.mark.os_agnostic
def test_differs_on_a_lone_surrogate_in_a_string() -> None:
    """Both refuse a lone surrogate, but with a different exception type and message."""
    record = EnvVar(name="X", value="a" + chr(0xDCFF))
    with pytest.raises(orjson.JSONEncodeError, match="surrogates not allowed"):
        _chain(record)
    with pytest.raises(PydanticSerializationError, match="surrogates not allowed"):
        record.model_dump_json()
    assert issubclass(orjson.JSONEncodeError, TypeError)
    assert issubclass(PydanticSerializationError, ValueError)
    assert not issubclass(PydanticSerializationError, TypeError)


@pytest.mark.os_agnostic
def test_both_refuse_an_unknown_type_in_an_open_bag_the_same_way() -> None:
    """A pure path (no Path subclass) is unknown to both: same exception, same message."""
    record = CimInstance(class_name="c", properties={"p": PurePosixPath("/a")})
    with pytest.raises(PydanticSerializationError) as via_chain:
        _chain(record)
    with pytest.raises(PydanticSerializationError) as via_pydantic:
        record.model_dump_json()
    assert str(via_chain.value) == str(via_pydantic.value)


@pytest.mark.os_agnostic
def test_differs_on_non_utf8_bytes_in_an_open_bag() -> None:
    """to_dict raises UnicodeDecodeError on non-UTF-8 bytes; model_dump_json raises its own error."""
    record = CimInstance(class_name="c", properties={"b": b"\xff\x00"})
    with pytest.raises(UnicodeDecodeError):
        _chain(record)
    with pytest.raises(PydanticSerializationError, match="invalid utf-8"):
        record.model_dump_json()


@pytest.mark.os_agnostic
def test_write_records_still_uses_the_chain(tmp_path: Path) -> None:
    """write_records raises what the chain raises, so a switch would show here first."""
    record = CimInstance(class_name="c", properties={"v": 2**64})
    with pytest.raises(orjson.JSONEncodeError, match="Integer exceeds 64-bit range"):
        write_records(tmp_path / "out.jsonl", [record])


@pytest.mark.os_agnostic
@pytest.mark.parametrize("fmt", [RecordFormat.JSONL, RecordFormat.JSON])
def test_cli_emit_still_uses_the_chain(fmt: RecordFormat) -> None:
    """--json / --jsonl raise what the chain raises, so a switch would show here first."""
    record = CimInstance(class_name="c", properties={"v": 2**64})
    with pytest.raises(orjson.JSONEncodeError, match="Integer exceeds 64-bit range"):
        emit([record], fmt)
