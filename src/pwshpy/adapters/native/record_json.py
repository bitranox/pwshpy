"""JSON bytes for CLI and file output - the one serialiser behind every ``--json`` / ``--jsonl``.

A record is dumped with ``orjson.dumps(record.to_dict())`` (``--json`` / ``--jsonl`` and
``write_records``), a plain value with ``orjson.dumps(value, default=str)`` (``emit_json``:
``get_known_folder --json``, ``run``, ``cmdlet`` and its wrappers, ``invoke_rest_method``). Text
holding a byte that is not valid UTF-8 (a POSIX environment variable, file name, process or account
name) reaches Python as a lone
surrogate, because ``os`` decodes with ``surrogateescape``, and orjson refuses a lone surrogate. Only
then is the value walked again: each code point in U+D800-U+DFFF is written as U+FFFD, one per
undecodable byte (for a single undecodable byte this is what pwsh 7 shows), and the paths that held
one are returned, so the caller can warn instead of aborting the stream. The output stays valid
UTF-8 JSON; the original bytes cannot be recovered from it.

A repaired mapping key that now equals another key keeps its entry under ``<key>~2`` (then ``~3``,
...): a key that was valid already keeps its name, and a later repaired key takes the next free
suffix. Reported paths are ASCII, each non-ASCII character of a key shown as ``?``, because the
warning is printed on consoles (cp1252) that cannot hold U+FFFD.

Lives in ``adapters`` because every caller does (``adapters.cli.output`` and
``adapters.native.fileio``) and the domain stays free of serialisation; ``native`` because the
library-side serialiser ``fileio`` is here and the CLI already imports sibling adapters.

Contents:
    * :class:`JsonBytes` - the JSON bytes plus the replaced paths.
    * :func:`json_bytes` - dump a plain value, repairing lone surrogates.
    * :func:`record_json_bytes` - dump one record, repairing lone surrogates.
    * :func:`describe_replacements` / :func:`describe_output_replacements` - the warning text.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import NamedTuple, cast

import orjson

from ...domain.records import PSRecord

#: Every UTF-16 surrogate code point; as a lone code point in a ``str`` it is an undecodable byte.
_SURROGATE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")

#: U+REPLACEMENT CHARACTER, written in place of each surrogate.
_REPLACEMENT = chr(0xFFFD)

#: What every warning says after naming the place.
_WARNING_TAIL = "not valid UTF-8; each undecodable byte written as U+FFFD"

#: orjson's ``default`` hook: called for a value orjson cannot write natively.
Default = Callable[[object], object]


class JsonBytes(NamedTuple):
    """JSON bytes and the dotted paths of the fields whose text was replaced.

    Example:
        >>> JsonBytes(b"{}", ()).replaced
        ()
    """

    data: bytes
    replaced: tuple[str, ...]


def json_bytes(value: object, *, default: Default | None = None) -> JsonBytes:
    """Dump ``value`` as JSON bytes, writing each lone surrogate as U+FFFD.

    The fast path is ``orjson.dumps(value, default=default)`` unchanged. Only when orjson refuses
    the value and it holds a lone surrogate (in a string, a mapping key, or the text ``default``
    turns a leaf into, at any depth) is it repaired and dumped again.

    Args:
        value: The value to serialise (dicts, lists, tuples, strings, numbers, and anything
            orjson or ``default`` can write).
        default: orjson's ``default`` hook (``str`` for ``emit_json``).

    Returns:
        The JSON bytes and the dotted ASCII paths (``value``, ``properties.items.2``; ``""`` for a
        bare top-level string) of everything whose text was replaced, in order; an empty tuple
        when nothing was.

    Raises:
        orjson.JSONEncodeError: For anything but a lone surrogate (an integer outside orjson's
            64-bit range), exactly as ``orjson.dumps`` raises it.

    Example:
        >>> result = json_bytes({"p": "a" + chr(0xDCFF)}, default=str)
        >>> result.data == b'{"p":"a' + chr(0xFFFD).encode() + b'"}', result.replaced
        (True, ('p',))
    """
    try:
        return JsonBytes(orjson.dumps(value, default=default), ())
    except orjson.JSONEncodeError:
        repairer = _Repairer(default)
        repaired = repairer.value(value, "")
        if not repairer.paths:
            raise
    # dict.fromkeys: a key and its value can both be replaced and would name one path twice.
    return JsonBytes(orjson.dumps(repaired, default=default), tuple(dict.fromkeys(repairer.paths)))


def record_json_bytes(record: PSRecord) -> JsonBytes:
    """Dump ``record`` as JSON bytes (``orjson.dumps(record.to_dict())``), writing each lone surrogate as U+FFFD.

    Args:
        record: The record to serialise.

    Returns:
        As :func:`json_bytes`: the bytes and the replaced field paths (``value``).

    Raises:
        orjson.JSONEncodeError: For anything but a lone surrogate, as ``orjson.dumps`` raises it.

    Example:
        >>> from pwshpy.domain.records import EnvVar
        >>> result = record_json_bytes(EnvVar(name="X", value="a" + chr(0xDCFF) + "b"))
        >>> result.data == b'{"name":"X","value":"a' + chr(0xFFFD).encode() + b'b"}', result.replaced
        (True, ('value',))
    """
    return json_bytes(record.to_dict())


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
    return f"{type(record).__name__} record {position}, {noun} {', '.join(fields)}: {_WARNING_TAIL}"


def describe_output_replacements(fields: tuple[str, ...]) -> str:
    """The warning text for a plain value (``emit_json``) that was repaired, never quoting it.

    Args:
        fields: The replaced paths, from :func:`json_bytes`; ``("",)`` for a bare string.

    Returns:
        One line without a trailing newline.

    Example:
        >>> describe_output_replacements(("",))
        'output: not valid UTF-8; each undecodable byte written as U+FFFD'
        >>> describe_output_replacements(("items.0",))
        'output field items.0: not valid UTF-8; each undecodable byte written as U+FFFD'
    """
    named = [field for field in fields if field]
    if not named:
        return f"output: {_WARNING_TAIL}"
    noun = "field" if len(named) == 1 else "fields"
    return f"output {noun} {', '.join(named)}: {_WARNING_TAIL}"


class _Repairer:
    """One repair walk: the ``default`` hook it honours and the paths it replaced, in order."""

    def __init__(self, default: Default | None) -> None:
        self._default = default
        self.paths: list[str] = []

    def value(self, value: object, path: str) -> object:
        """``value`` with every lone surrogate written as U+FFFD."""
        if isinstance(value, str):
            return self._text(value, path)
        if isinstance(value, Mapping):
            return self._mapping(cast("Mapping[object, object]", value), path)
        if isinstance(value, (list, tuple)):
            items = cast("list[object] | tuple[object, ...]", value)
            return [self.value(item, _join(path, str(index))) for index, item in enumerate(items)]
        return self._leaf(value, path)

    def _mapping(self, mapping: Mapping[object, object], path: str) -> dict[object, object]:
        # Valid keys are reserved first, so a repaired key never takes a name that was already
        # someone's, whichever comes first; a repaired key that collides takes the next free suffix.
        taken = {key for key in mapping if isinstance(key, str) and not _SURROGATE.search(key)}
        repaired: dict[object, object] = {}
        for key, item in mapping.items():
            new_key = key
            if isinstance(key, str) and _SURROGATE.search(key):
                new_key = _free_name(_SURROGATE.sub(_REPLACEMENT, key), taken)
                taken.add(new_key)
                self.paths.append(_join(path, _ascii(new_key)))
            repaired[new_key] = self.value(item, _join(path, _ascii(str(new_key))))
        return repaired

    def _text(self, text: str, path: str) -> str:
        repaired = _SURROGATE.sub(_REPLACEMENT, text)
        if repaired != text:
            self.paths.append(path)
        return repaired

    def _leaf(self, leaf: object, path: str) -> object:
        # A leaf orjson writes natively stays as it is. One it hands to ``default`` (a Path for
        # emit_json) is converted here only when the converted text holds a surrogate, so an
        # integer orjson cannot write is never turned into a string and still raises.
        if self._default is None or leaf is None or isinstance(leaf, (bool, int, float)):
            return leaf
        converted = self._default(leaf)
        if isinstance(converted, str) and _SURROGATE.search(converted):
            return self._text(converted, path)
        return leaf


def _free_name(name: str, taken: set[str]) -> str:
    """``name``, or ``name~2``, ``name~3``, ... - the first not in ``taken``."""
    candidate, suffix = name, 2
    while candidate in taken:
        candidate, suffix = f"{name}~{suffix}", suffix + 1
    return candidate


def _ascii(segment: str) -> str:
    """``segment`` with each non-ASCII or non-printable character as ``?``.

    The warning reaches cp1252 consoles, and a key from an outside source (a REST body, a CIM
    property) can hold a newline or an ESC sequence that would split or style the one-line warning.
    """
    return "".join(char if char.isascii() and char.isprintable() else "?" for char in segment)


def _join(path: str, segment: str) -> str:
    return f"{path}.{segment}" if path else segment


__all__ = [
    "JsonBytes",
    "describe_output_replacements",
    "describe_replacements",
    "json_bytes",
    "record_json_bytes",
]
