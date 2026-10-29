"""The one rule for text holding a byte that is not valid UTF-8 (a lone surrogate).

``os`` decodes such a byte with ``surrogateescape`` into a lone code point in U+D800-U+DFFF, which
no UTF-8 encoder accepts. Each one is written as U+FFFD, one per undecodable byte (for a single
undecodable byte this is what pwsh 7 shows). A mapping key repaired this way can then equal another
key; it keeps its entry under ``<key>~2`` (then ``~3``, ...): a key that was valid already keeps its
name, and a later repaired key takes the next free suffix.

Pure functions, shared by ``PSRecord.to_dict`` (keys) and the output layer's repair (keys and
values), so both always name a repaired key alike.

Contents:
    * :func:`has_surrogate` - whether a value is text holding a lone surrogate.
    * :func:`replace_surrogates` - text with each lone surrogate written as U+FFFD.
    * :func:`repaired_key_names` - the new name of each mapping key holding a lone surrogate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TypeGuard

#: Every UTF-16 surrogate code point; as a lone code point in a ``str`` it is an undecodable byte.
_SURROGATE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")

#: U+REPLACEMENT CHARACTER, written in place of each surrogate.
REPLACEMENT = chr(0xFFFD)


def has_surrogate(value: object) -> TypeGuard[str]:
    """Whether ``value`` is a string holding a lone surrogate (an ASCII string never does).

    Example:
        >>> has_surrogate("a" + chr(0xDCFF)), has_surrogate("a"), has_surrogate(1)
        (True, False, False)
    """
    return isinstance(value, str) and not value.isascii() and _SURROGATE.search(value) is not None


def replace_surrogates(text: str) -> str:
    """``text`` with each lone surrogate written as one U+FFFD.

    Example:
        >>> replace_surrogates("a" + chr(0xDCFF) + chr(0xDCFE)) == "a" + REPLACEMENT * 2
        True
    """
    return _SURROGATE.sub(REPLACEMENT, text)


def repaired_key_names(keys: Iterable[object]) -> dict[object, str]:
    """The new name of each key holding a lone surrogate, in order; other keys are not listed.

    Valid keys are reserved first, so a repaired key never takes a name that was already someone's,
    whichever comes first; a repaired key that collides takes the next free ``~N`` suffix.

    Args:
        keys: One mapping's keys, in its order.

    Returns:
        Original key to repaired name, for the repaired keys only.

    Example:
        >>> bad, ok = "a" + chr(0xDCFF), "a" + REPLACEMENT
        >>> repaired_key_names([bad, ok, 7]) == {bad: ok + "~2"}
        True
    """
    ordered = list(keys)
    taken = {key for key in ordered if isinstance(key, str) and not has_surrogate(key)}
    renamed: dict[object, str] = {}
    for key in ordered:
        if isinstance(key, str) and has_surrogate(key):
            renamed[key] = _free_name(replace_surrogates(key), taken)
            taken.add(renamed[key])
    return renamed


def _free_name(name: str, taken: set[str]) -> str:
    """``name``, or ``name~2``, ``name~3``, ... - the first not in ``taken``."""
    candidate, suffix = name, 2
    while candidate in taken:
        candidate, suffix = f"{name}~{suffix}", suffix + 1
    return candidate


__all__ = ["REPLACEMENT", "has_surrogate", "replace_surrogates", "repaired_key_names"]
