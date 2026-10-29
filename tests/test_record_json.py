"""The shared record serialiser: U+FFFD for each undecodable byte, and the paths it replaced.

``record_json_bytes`` is what ``--json`` / ``--jsonl`` and ``ps.write_records`` dump a record with;
``json_bytes`` is the same rule for a plain value (``emit_json``: ``get_known_folder --json``, ``run``).
A POSIX environment variable, file name, process name or account name holding a byte that is not
valid UTF-8 reaches Python as a lone surrogate (``os`` decodes with ``surrogateescape``), which
``orjson`` refuses; the helper writes each such code point as U+FFFD and names the field.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import orjson
import pytest

from pwshpy.adapters.native.record_json import (
    JsonBytes,
    describe_output_replacements,
    describe_replacements,
    json_bytes,
    record_json_bytes,
)
from pwshpy.domain.enums import ProcessStatus
from pwshpy.domain.records import (
    CimInstance,
    EnvVar,
    PSInvocationResult,
    PSObjectRecord,
    PSRecord,
    to_dict_keeping_surrogate_keys,
)

#: The code point os.environ / os.fsdecode hold for the undecodable byte 0xFF.
_BAD = chr(0xDCFF)
#: What each undecodable byte is written as.
_FFFD = chr(0xFFFD)


# --- the shared helper ---------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_a_clean_record_is_dumped_as_before_with_no_replaced_fields() -> None:
    """The fast path: the same bytes as orjson.dumps(to_dict()) and nothing replaced."""
    record = EnvVar(name="A", value="caf" + chr(0xE9))
    assert record_json_bytes(record) == JsonBytes(orjson.dumps(record.to_dict()), ())


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
    assert result.replaced == ("bag.k?", "bag.k?.x")


@pytest.mark.os_agnostic
def test_reported_paths_are_ascii_with_question_marks_for_non_ascii_key_characters() -> None:
    """A path is printed on consoles that cannot hold U+FFFD (cp1252): every non-ASCII key char is '?'."""
    result = record_json_bytes(_RawBag(bag={"caf" + chr(0xE9) + _BAD: "v", "x": chr(0x4E2D) + _BAD}))
    assert result.replaced == ("bag.caf??", "bag.x")
    assert all(path.isascii() for path in result.replaced)


@pytest.mark.os_agnostic
def test_reported_paths_hold_no_control_characters() -> None:
    """A key with a newline or an ESC sequence must not split or style the one-line warning."""
    result = record_json_bytes(_RawBag(bag={"a\nb" + _BAD: "v", "c\x1b[31m": "v" + _BAD, "d\x7f": _BAD}))
    assert result.replaced == ("bag.a?b?", "bag.c?[31m", "bag.d?")
    assert all(path.isprintable() for path in result.replaced)


@pytest.mark.os_agnostic
def test_two_keys_differing_only_in_bad_bytes_both_survive_with_a_suffix() -> None:
    """The later colliding key gets ~2 (then ~3, ...) and is reported; no entry is lost."""
    bag = {"a" + chr(0xDCFF): 1, "a" + chr(0xDCFE): 2, "a" + chr(0xDCFD): 3}
    result = record_json_bytes(_RawBag(bag=bag))
    assert json.loads(result.data)["bag"] == {"a" + _FFFD: 1, "a" + _FFFD + "~2": 2, "a" + _FFFD + "~3": 3}
    assert result.replaced == ("bag.a?", "bag.a?~2", "bag.a?~3")


@pytest.mark.os_agnostic
def test_a_clean_key_keeps_its_name_and_the_repaired_key_takes_the_suffix() -> None:
    """A key that was already valid is never renamed, even when it comes after the repaired one."""
    result = record_json_bytes(_RawBag(bag={"a" + _BAD: 1, "a" + _FFFD: 2, "a" + _FFFD + "~2": 3}))
    assert json.loads(result.data)["bag"] == {"a" + _FFFD + "~3": 1, "a" + _FFFD: 2, "a" + _FFFD + "~2": 3}
    assert result.replaced == ("bag.a?~3",)


@pytest.mark.os_agnostic
def test_a_surrogate_in_a_dict_fields_own_key_is_one_fffd_per_byte_and_named() -> None:
    """A dict field's own key: one U+FFFD per byte and a warning path, never three U+FFFD silently.

    pydantic-core alone rewrites such a key into three U+FFFD (the surrogate's UTF-8 bytes) and
    reports nothing; the output layer dumps with the key kept, so the repair here names it.
    """
    record = CimInstance(class_name="c", properties={"k" + _BAD: "v"})
    assert to_dict_keeping_surrogate_keys(record)["properties"] == {"k" + _BAD: "v"}
    result = record_json_bytes(record)
    assert json.loads(result.data)["properties"] == {"k" + _FFFD: "v"}
    assert result.replaced == ("properties.k?",)


@pytest.mark.os_agnostic
def test_a_surrogate_in_a_nested_key_is_repaired_instead_of_raising() -> None:
    """A NESTED key of an open bag made pydantic-core raise UnicodeEncodeError in to_dict itself."""
    record = CimInstance(class_name="c", properties={"o": {"k" + _BAD: 1}})
    result = record_json_bytes(record)
    assert json.loads(result.data)["properties"] == {"o": {"k" + _FFFD: 1}}
    assert result.replaced == ("properties.o.k?",)


@pytest.mark.os_agnostic
def test_a_surrogate_key_in_a_list_and_in_a_nested_record_is_repaired() -> None:
    """A dict inside ``list[Any]``, and a record inside it whose own dict field has the key."""
    record = PSInvocationResult(
        output=[{"k" + _BAD: 1}, PSObjectRecord(type_name="T", properties={"p" + _BAD: {"q" + _BAD: 2}})]
    )
    result = record_json_bytes(record)
    assert json.loads(result.data)["output"] == [
        {"k" + _FFFD: 1},
        {"type_name": "T", "properties": {"p" + _FFFD: {"q" + _FFFD: 2}}},
    ]
    assert result.replaced == ("output.0.k?", "output.1.properties.p?", "output.1.properties.p?.q?")


@pytest.mark.os_agnostic
def test_two_dict_field_keys_differing_only_in_bad_bytes_both_survive() -> None:
    """pydantic-core turned both into the same three U+FFFD, so one entry was lost silently."""
    record = CimInstance(class_name="c", properties={"a" + chr(0xDCFF): 1, "a" + chr(0xDCFE): 2})
    assert json.loads(record_json_bytes(record).data)["properties"] == {"a" + _FFFD: 1, "a" + _FFFD + "~2": 2}


@pytest.mark.os_agnostic
def test_to_dict_keeps_the_json_form_of_values_beside_a_surrogate_key() -> None:
    """The slow path still dumps in JSON mode: a datetime is ISO text, an enum its value."""
    when = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    record = CimInstance(class_name="c", properties={"k" + _BAD: {"t": when, "s": ProcessStatus.RUNNING}, "x": when})
    clean = CimInstance(class_name="c", properties={"k": {"t": when, "s": ProcessStatus.RUNNING}, "x": when})
    dumped = clean.to_dict()["properties"]
    assert dumped == {"k": {"t": "2026-10-29T12:00:00Z", "s": "running"}, "x": "2026-10-29T12:00:00Z"}
    assert to_dict_keeping_surrogate_keys(record)["properties"] == {"k" + _BAD: dumped["k"], "x": dumped["x"]}
    assert record.to_dict()["properties"] == {"k" + _FFFD: dumped["k"], "x": dumped["x"]}


@pytest.mark.os_agnostic
def test_a_real_key_that_looks_like_an_internal_placeholder_is_kept() -> None:
    """Whatever stands in for a surrogate key while pydantic dumps must never capture a real key."""
    bag = {chr(0) + str(n): n for n in range(4)} | {"k" + _BAD: "v"}
    record = CimInstance(class_name="c", properties=bag)
    assert to_dict_keeping_surrogate_keys(record)["properties"] == bag


@pytest.mark.os_agnostic
def test_a_clean_record_with_open_bags_dumps_exactly_as_pydantic_does() -> None:
    """The fast path is model_dump(mode='json') itself."""
    when = datetime(2026, 10, 29, tzinfo=timezone.utc)
    record = PSInvocationResult(output=[{"a": when}, PSObjectRecord(type_name="T", properties={"b": [1]})])
    assert record.to_dict() == record.model_dump(mode="json")


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


# --- plain values (emit_json) ---------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_a_clean_value_is_dumped_as_before() -> None:
    """The fast path for a plain value is orjson.dumps(value, default=str)."""
    value = {"p": PurePosixPath("/opt/data"), "n": 1}
    assert json_bytes(value, default=str) == JsonBytes(orjson.dumps(value, default=str), ())


@pytest.mark.os_agnostic
def test_a_bare_string_is_repaired_with_an_empty_path() -> None:
    """A top-level string has no field: its path is the empty string."""
    result = json_bytes("/opt/a" + _BAD + "b", default=str)
    assert json.loads(result.data) == "/opt/a" + _FFFD + "b"
    assert result.replaced == ("",)


@pytest.mark.os_agnostic
def test_a_value_converted_by_default_is_repaired_at_its_path() -> None:
    """A leaf orjson hands to ``default`` (a path) is repaired when its text holds a surrogate."""
    result = json_bytes({"o": [{"p": PurePosixPath("/opt/x" + _BAD), "q": PurePosixPath("/opt/ok")}]}, default=str)
    assert json.loads(result.data) == {"o": [{"p": "/opt/x" + _FFFD, "q": "/opt/ok"}]}
    assert result.replaced == ("o.0.p",)


@pytest.mark.os_agnostic
def test_a_plain_value_with_an_oversized_integer_still_raises() -> None:
    """``default=str`` does not turn an integer orjson cannot write into a string."""
    with pytest.raises(orjson.JSONEncodeError, match="Integer exceeds 64-bit range"):
        json_bytes({"s": _BAD, "v": 2**64}, default=str)


@pytest.mark.os_agnostic
def test_describe_output_replacements_wording() -> None:
    """``output field <path>``, ``output fields a, b``, and plain ``output`` for a bare string."""
    tail = ": not valid UTF-8; each undecodable byte written as U+FFFD"
    assert describe_output_replacements(("a.b",)) == "output field a.b" + tail
    assert describe_output_replacements(("a", "b")) == "output fields a, b" + tail
    assert describe_output_replacements(("",)) == "output" + tail
