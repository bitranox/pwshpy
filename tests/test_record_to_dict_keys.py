"""``PSRecord.to_dict()`` repairs a mapping key holding a lone surrogate, so its result always serialises.

[154] A key holding a byte that is not valid UTF-8 (a lone surrogate from ``surrogateescape``) is
written as one U+FFFD per undecodable byte, a repaired key that collides with another keeps its entry
as ``<key>~2`` (``~3``, ...), exactly as the output layer writes such a key. So a library caller's
``orjson.dumps(record.to_dict())`` never raises over a key. The warning that names the record and
field stays in the output layer (the domain prints nothing): ``--json``, ``--jsonl``, the human table
and ``write_records`` still report a repaired key.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
from typing import Annotated, Any

import orjson
import pytest
from click.testing import CliRunner
from fake_services import services_with_ps
from pydantic import Field

from pwshpy.adapters.cli.output import RecordFormat, emit
from pwshpy.adapters.cli.root import cli
from pwshpy.adapters.native.record_json import record_json_bytes
from pwshpy.domain.records import (
    CimInstance,
    ProcessInfo,
    PSInvocationResult,
    PSObjectRecord,
    PSRecord,
    _mapping_fields,  # pyright: ignore[reportPrivateUsage] - pins which fields the hot path walks
    to_dict_keeping_surrogate_keys,
)

_BAD = chr(0xDCFF)
_FFFD = chr(0xFFFD)


@pytest.mark.os_agnostic
def test_to_dict_of_a_bag_with_a_surrogate_key_serialises() -> None:
    """The reviewer's case: orjson.dumps(to_dict()) raised JSONEncodeError over the key."""
    record = CimInstance(class_name="c", properties={"k" + _BAD: 1})
    assert json.loads(orjson.dumps(record.to_dict())) == {"class_name": "c", "properties": {"k" + _FFFD: 1}}


@pytest.mark.os_agnostic
def test_to_dict_repairs_one_fffd_per_byte_at_any_depth() -> None:
    """Nested keys, keys in a list and a nested record's bag: each bad byte becomes one U+FFFD."""
    record = PSInvocationResult(
        output=[
            {"k" + _BAD + _BAD: {"n" + _BAD: 1}},
            PSObjectRecord(type_name="T", properties={"p" + _BAD: {"q" + _BAD: 2}}),
        ]
    )
    assert record.to_dict()["output"] == [
        {"k" + _FFFD + _FFFD: {"n" + _FFFD: 1}},
        {"type_name": "T", "properties": {"p" + _FFFD: {"q" + _FFFD: 2}}},
    ]
    orjson.dumps(record.to_dict())


@pytest.mark.os_agnostic
def test_to_dict_keeps_colliding_keys_with_the_tilde_rule() -> None:
    """A valid key keeps its name; repaired keys take the next free ``~N``; no entry is lost."""
    bag = {"a" + chr(0xDCFF): 1, "a" + _FFFD: 2, "a" + chr(0xDCFE): 3}
    record = CimInstance(class_name="c", properties=bag)
    assert record.to_dict()["properties"] == {"a" + _FFFD + "~2": 1, "a" + _FFFD: 2, "a" + _FFFD + "~3": 3}


@pytest.mark.os_agnostic
def test_to_dict_leaves_values_for_the_output_layer() -> None:
    """Only keys are repaired here; a surrogate in a value is the output layer's to repair and name."""
    record = CimInstance(class_name="c", properties={"k" + _BAD: "v" + _BAD})
    assert record.to_dict()["properties"] == {"k" + _FFFD: "v" + _BAD}


@pytest.mark.os_agnostic
def test_json_output_still_names_a_repaired_key() -> None:
    """record_json still warns for the key, with the same path as before."""
    result = record_json_bytes(CimInstance(class_name="c", properties={"k" + _BAD: "v"}))
    assert json.loads(result.data)["properties"] == {"k" + _FFFD: "v"}
    assert result.replaced == ("properties.k?",)


@pytest.mark.os_agnostic
def test_the_human_table_still_names_a_repaired_key() -> None:
    """The table repairs through to_dict's sibling that keeps the key, so its warning names it too."""
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        emit([CimInstance(class_name="c", properties={"k" + _BAD: "v"})], RecordFormat.HUMAN)
    warnings = [line for line in stderr.getvalue().splitlines() if line.startswith("warning:")]
    assert warnings == [
        "warning: CimInstance record 1, field properties.k?: not valid UTF-8; each undecodable byte written as U+FFFD"
    ]


class _FakeCmdletPs:
    """The one facade method ``cmdlet --streams`` calls, answering with a bag holding a bad key."""

    def cmdlet(self, name: str, *, timeout: float | None = None, **params: object) -> PSInvocationResult:
        return PSInvocationResult(output=[{"k" + _BAD: name}])


@pytest.mark.os_agnostic
def test_cmdlet_streams_still_names_a_repaired_key() -> None:
    """``cmdlet --streams`` dumps the whole result; the bad key is repaired and named, not hidden."""
    services = services_with_ps(_FakeCmdletPs())
    result = CliRunner().invoke(cli, ["cmdlet", "Get-Thing", "--streams"], obj=lambda: services)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["output"] == [{"k" + _FFFD: "Get-Thing"}]
    warnings = [line for line in result.stderr.splitlines() if line.startswith("warning:")]
    assert warnings == ["warning: output field output.0.k?: not valid UTF-8; each undecodable byte written as U+FFFD"]


# --- only fields that can hold a mapping are walked (F6), and none that can is skipped ------------


class _Inner(PSRecord):
    bag: dict[str, int]


class _Shapes(PSRecord):
    """One field per annotation shape that can carry a mapping, beside scalars that cannot."""

    name: str = ""
    count: int = 0
    tags: list[str] = Field(default_factory=list)
    nested: _Inner | None = None
    anything: list[Any] = Field(default_factory=list)
    maybe: dict[str, int] | None = None
    tagged: Annotated[dict[str, int], "meta"] = Field(default_factory=dict)
    inners: tuple[_Inner, ...] = ()


class _Node(PSRecord):
    """A self-referencing record: the walk must terminate and still look inside."""

    children: tuple[_Node, ...] = ()
    data: dict[str, int] = Field(default_factory=dict)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("record", "field"),
    [
        (_Shapes(nested=_Inner(bag={"k" + _BAD: 1})), "nested"),
        (_Shapes(anything=[{"k" + _BAD: 1}]), "anything"),
        (_Shapes(maybe={"k" + _BAD: 1}), "maybe"),
        (_Shapes(tagged={"k" + _BAD: 1}), "tagged"),
        (_Shapes(inners=(_Inner(bag={}), _Inner(bag={"k" + _BAD: 1}))), "inners"),
        (_Node(children=(_Node(children=(_Node(data={"k" + _BAD: 1}),)),)), "children"),
    ],
    ids=["nested-model", "list-of-any", "optional-dict", "annotated-dict", "tuple-of-models", "recursive"],
)
def test_a_surrogate_key_is_found_through_every_annotation_shape(record: PSRecord, field: str) -> None:
    """Restricting the walk to mapping-capable fields must never skip one: each key is repaired."""
    dumped = orjson.dumps(record.to_dict()).decode()
    assert "k" + _FFFD in dumped
    assert to_dict_keeping_surrogate_keys(record) != record.to_dict()
    assert field in record.to_dict()


@pytest.mark.os_agnostic
def test_a_record_of_scalars_has_no_field_to_walk() -> None:
    """The clean hot path: ProcessInfo has no field that can hold a mapping, CimInstance one."""
    assert _mapping_fields(ProcessInfo) == ()
    assert _mapping_fields(CimInstance) == ("properties",)
    assert _mapping_fields(_Shapes) == ("nested", "anything", "maybe", "tagged", "inners")
    assert _mapping_fields(_Node) == ("children", "data")
