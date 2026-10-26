"""The shared record serialiser: U+FFFD for each undecodable byte, and the paths it replaced.

``record_json_bytes`` is what ``--json`` / ``--jsonl`` and ``ps.write_records`` dump a record with.
A POSIX environment variable, file name, process name or account name holding a byte that is not
valid UTF-8 reaches Python as a lone surrogate (``os`` decodes with ``surrogateescape``), which
``orjson`` refuses; the helper writes each such code point as U+FFFD and names the field.
"""

from __future__ import annotations

import json
from typing import Any

import orjson
import pytest

from pwshpy.adapters.native.record_json import RecordJson, describe_replacements, record_json_bytes
from pwshpy.domain.records import CimInstance, EnvVar, PSRecord

#: The code point os.environ / os.fsdecode hold for the undecodable byte 0xFF.
_BAD = chr(0xDCFF)
#: What each undecodable byte is written as.
_FFFD = chr(0xFFFD)


# --- the shared helper ---------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_a_clean_record_is_dumped_as_before_with_no_replaced_fields() -> None:
    """The fast path: the same bytes as orjson.dumps(to_dict()) and nothing replaced."""
    record = EnvVar(name="A", value="caf" + chr(0xE9))
    assert record_json_bytes(record) == RecordJson(orjson.dumps(record.to_dict()), ())


@pytest.mark.os_agnostic
def test_a_lone_surrogate_becomes_fffd_and_its_field_is_named() -> None:
    """a<0xFF>b decodes to a U+DCFF b; it is written as a U+FFFD b and the field is reported."""
    result = record_json_bytes(EnvVar(name="X", value="a" + _BAD + "b"))
    assert json.loads(result.data) == {"name": "X", "value": "a" + _FFFD + "b"}
    assert result.replaced == ("value",)


@pytest.mark.os_agnostic
def test_each_undecodable_byte_becomes_its_own_fffd() -> None:
    """Two bad bytes give two replacement characters, not one."""
    result = record_json_bytes(EnvVar(name="X", value="a" + chr(0xDCFF) + chr(0xDCFE) + "b"))
    assert json.loads(result.data)["value"] == "a" + _FFFD + _FFFD + "b"


@pytest.mark.os_agnostic
def test_every_surrogate_code_point_is_replaced() -> None:
    """The whole surrogate range is covered, both ends included."""
    result = record_json_bytes(EnvVar(name="X", value=chr(0xD800) + chr(0xDFFF)))
    assert json.loads(result.data)["value"] == _FFFD + _FFFD


@pytest.mark.os_agnostic
def test_nested_paths_are_dotted_with_list_indexes() -> None:
    """A surrogate deep in an open bag is reported by its dotted path, list items by index."""
    record = CimInstance(class_name="c", properties={"outer": {"items": ["ok", "x" + _BAD]}, "plain": 1})
    result = record_json_bytes(record)
    assert json.loads(result.data)["properties"] == {"outer": {"items": ["ok", "x" + _FFFD]}, "plain": 1}
    assert result.replaced == ("properties.outer.items.1",)


class _RawBag(PSRecord):
    """A record whose mapping keeps a surrogate in a nested key, as a non-pydantic to_dict could."""

    bag: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"bag": self.bag}


@pytest.mark.os_agnostic
def test_a_surrogate_in_a_dict_key_is_replaced_and_named() -> None:
    """A bad byte in a mapping key is replaced too; the path names the replaced key once."""
    result = record_json_bytes(_RawBag(bag={"k" + _BAD: {"x": "v" + _BAD}}))
    assert json.loads(result.data) == {"bag": {"k" + _FFFD: {"x": "v" + _FFFD}}}
    assert result.replaced == ("bag.k" + _FFFD, "bag.k" + _FFFD + ".x")


@pytest.mark.os_agnostic
def test_pydantic_rewrites_a_top_level_key_before_the_helper_sees_it() -> None:
    """Pins pydantic-core: to_dict turns a surrogate in a dict field's own key into three U+FFFD.

    The helper never sees that key, so it is not reported. If pydantic starts passing the key
    through, this fails and the helper's key repair takes over.
    """
    record = CimInstance(class_name="c", properties={"k" + _BAD: "v"})
    assert record.to_dict()["properties"] == {"k" + _FFFD * 3: "v"}
    assert record_json_bytes(record).replaced == ()


@pytest.mark.os_agnostic
def test_pydantic_refuses_a_nested_key_in_to_dict() -> None:
    """Pins pydantic-core: a surrogate in a NESTED key of an open bag fails in to_dict itself."""
    record = CimInstance(class_name="c", properties={"o": {"k" + _BAD: 1}})
    with pytest.raises(UnicodeEncodeError):
        record_json_bytes(record)


@pytest.mark.os_agnostic
def test_several_fields_are_all_named_in_order() -> None:
    """Every replaced field is reported once, in field order."""
    result = record_json_bytes(EnvVar(name="n" + _BAD, value="v" + _BAD))
    assert result.replaced == ("name", "value")


@pytest.mark.os_agnostic
def test_an_integer_outside_orjson_range_still_raises() -> None:
    """Only surrogates are repaired: any other encode error is raised exactly as before."""
    with pytest.raises(orjson.JSONEncodeError, match="Integer exceeds 64-bit range"):
        record_json_bytes(CimInstance(class_name="c", properties={"v": 2**64}))


@pytest.mark.os_agnostic
def test_an_integer_outside_orjson_range_beside_a_surrogate_still_raises() -> None:
    """Repairing the surrogate does not hide an integer orjson cannot write."""
    with pytest.raises(orjson.JSONEncodeError, match="Integer exceeds 64-bit range"):
        record_json_bytes(CimInstance(class_name="c", properties={"s": _BAD, "v": 2**64}))


@pytest.mark.os_agnostic
def test_describe_replacements_names_type_position_and_fields_never_the_value() -> None:
    """One line: record type, 1-based position, field path(s), and what was done."""
    record = EnvVar(name="SECRETNAME", value="hunter2" + _BAD)
    assert describe_replacements(record, 7, ("value",)) == (
        "EnvVar record 7, field value: not valid UTF-8; each undecodable byte written as U+FFFD"
    )
    assert describe_replacements(record, 2, ("name", "value")) == (
        "EnvVar record 2, fields name, value: not valid UTF-8; each undecodable byte written as U+FFFD"
    )
