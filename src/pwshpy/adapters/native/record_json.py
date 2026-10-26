"""Record JSON bytes - the one serialiser behind ``--json`` / ``--jsonl`` and ``write_records``.

A record is dumped with ``orjson.dumps(record.to_dict())``. Text holding a byte that is not valid
UTF-8 (a POSIX environment variable, file name, process or account name) reaches Python as a lone
surrogate, because ``os`` decodes with ``surrogateescape``, and orjson refuses a lone surrogate. Only
then is the record's mapping walked again: each code point in U+D800-U+DFFF is written as U+FFFD
(one per undecodable byte, which is what pwsh 7 shows for the same value) and the field paths that
held one are returned, so the caller can warn instead of aborting the stream. The output stays
valid UTF-8 JSON; the original bytes cannot be recovered from it.

Lives in ``adapters`` because both callers do (``adapters.cli.output`` and
``adapters.native.fileio``) and the domain stays free of serialisation; ``native`` because the
library-side serialiser ``fileio`` is here and the CLI already imports sibling adapters.

Contents:
    * :class:`RecordJson` - the JSON bytes plus the replaced field paths.
    * :func:`record_json_bytes` - dump one record, repairing lone surrogates.
    * :func:`describe_replacements` - the one-line warning text both callers print.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import NamedTuple, cast

import orjson

from ...domain.records import PSRecord

#: Every UTF-16 surrogate code point; as a lone code point in a ``str`` it is an undecodable byte.
_SURROGATE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")

#: U+REPLACEMENT CHARACTER, written in place of each surrogate.
_REPLACEMENT = chr(0xFFFD)


class RecordJson(NamedTuple):
    """One record's JSON bytes and the dotted paths of the fields whose text was replaced.

    Example:
        >>> RecordJson(b"{}", ()).replaced
        ()
    """

    data: bytes
    replaced: tuple[str, ...]


def record_json_bytes(record: PSRecord) -> RecordJson:
    """Dump ``record`` as JSON bytes, writing each lone surrogate as U+FFFD.

    The fast path is ``orjson.dumps(record.to_dict())`` unchanged. Only when orjson refuses the
    mapping and the mapping holds a lone surrogate (in a value or a key, at any depth) is it
    repaired and dumped again.

    Args:
        record: The record to serialise.

    Returns:
        The JSON bytes and the dotted paths (``value``, ``properties.items.2``) of every field
        whose text was replaced, in field order; an empty tuple when nothing was.

    Raises:
        orjson.JSONEncodeError: For anything but a lone surrogate (an integer outside orjson's
            64-bit range), exactly as ``orjson.dumps`` raises it.

    Example:
        >>> from pwshpy.domain.records import EnvVar
        >>> result = record_json_bytes(EnvVar(name="X", value="a" + chr(0xDCFF) + "b"))
        >>> result.data == b'{"name":"X","value":"a' + chr(0xFFFD).encode() + b'b"}', result.replaced
        (True, ('value',))
    """
    mapping = record.to_dict()
    try:
        return RecordJson(orjson.dumps(mapping), ())
    except orjson.JSONEncodeError:
        replaced: list[str] = []
        repaired = _repair(mapping, "", replaced)
        if not replaced:
            raise
    # dict.fromkeys: a key and its value can both be replaced and would name one path twice.
    return RecordJson(orjson.dumps(repaired), tuple(dict.fromkeys(replaced)))


def describe_replacements(record: PSRecord, position: int, fields: tuple[str, ...]) -> str:
    """The warning text for a record whose fields were repaired, never quoting a value.

    Args:
        record: The record that was repaired (only its type name is used).
        position: Its 1-based position in the stream or file.
        fields: The replaced field paths, from :func:`record_json_bytes`.

    Returns:
        One line without a trailing newline.

    Example:
        >>> from pwshpy.domain.records import EnvVar
        >>> describe_replacements(EnvVar(name="X", value=""), 7, ("value",))
        'EnvVar record 7, field value: not valid UTF-8; each undecodable byte written as U+FFFD'
    """
    noun = "field" if len(fields) == 1 else "fields"
    return (
        f"{type(record).__name__} record {position}, {noun} {', '.join(fields)}: "
        "not valid UTF-8; each undecodable byte written as U+FFFD"
    )


def _repair(value: object, path: str, replaced: list[str]) -> object:
    """``value`` with every lone surrogate written as U+FFFD; each repaired path appended to ``replaced``."""
    if isinstance(value, str):
        return _repair_text(value, path, replaced)
    if isinstance(value, Mapping):
        return _repair_mapping(cast("Mapping[object, object]", value), path, replaced)
    if isinstance(value, (list, tuple)):
        items = cast("list[object] | tuple[object, ...]", value)
        return [_repair(item, _join(path, str(index)), replaced) for index, item in enumerate(items)]
    return value


def _repair_mapping(mapping: Mapping[object, object], path: str, replaced: list[str]) -> dict[object, object]:
    repaired: dict[object, object] = {}
    for key, item in mapping.items():
        new_key = key
        if isinstance(key, str):
            new_key = _SURROGATE.sub(_REPLACEMENT, key)
            if new_key != key:
                replaced.append(_join(path, new_key))
        repaired[new_key] = _repair(item, _join(path, str(new_key)), replaced)
    return repaired


def _repair_text(text: str, path: str, replaced: list[str]) -> str:
    repaired = _SURROGATE.sub(_REPLACEMENT, text)
    if repaired != text:
        replaced.append(path)
    return repaired


def _join(path: str, segment: str) -> str:
    return f"{path}.{segment}" if path else segment


__all__ = ["RecordJson", "describe_replacements", "record_json_bytes"]
