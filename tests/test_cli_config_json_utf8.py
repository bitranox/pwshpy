"""``config --format json`` writes a byte that is not valid UTF-8 as U+FFFD, like every other JSON output.

[160] A POSIX command-line argument or environment variable holding such a byte reaches Python as a
lone surrogate (``surrogateescape``). ``pwshpy --set 'lib_log_rich.service=a<0xFF>' config --format
json`` went through lib_layered_config's own serialiser, which refused it (``TypeError: str is not
valid UTF-8: surrogates not allowed``, exit 22). It now goes through record_json's ``json_bytes``:
one U+FFFD per undecodable byte, one ``warning: output field <path>`` line on stderr naming where,
never the value; clean output is unchanged (two-space indented JSON).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

import pytest
from click.testing import CliRunner
from lib_layered_config import Config

from pwshpy.adapters import cli as cli_mod

#: The code point a POSIX argv holds for the undecodable byte 0xFF.
_BAD = chr(0xDCFF)
#: What each undecodable byte is written as.
_FFFD = chr(0xFFFD)
_TAIL = ": not valid UTF-8; each undecodable byte written as U+FFFD"

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="a Windows command line cannot hold a byte that is not valid UTF-8"
)


def _warnings(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("warning:")]


def _config_json(factory: Callable[[], Any], *args: str) -> Any:
    return CliRunner().invoke(
        cli_mod.cli, ["--set", "section.key=a" + _BAD + "b", "config", "--format", "json", *args], obj=factory
    )


@pytest.mark.os_posix
@_POSIX_ONLY
def test_config_json_writes_fffd_for_a_bad_byte_and_warns(
    config_cli_context: Callable[[dict[str, Any]], Callable[[], Any]],
) -> None:
    """Exit 0, valid JSON with U+FFFD in place of the byte, and one warning naming the key path."""
    result = _config_json(config_cli_context({"section": {"key": "x", "password": "hunter2"}}))
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["section"]["key"] == "a" + _FFFD + "b"
    assert data["section"]["password"] != "hunter2"
    assert _warnings(result.stderr) == ["warning: output field section.key" + _TAIL]


@pytest.mark.os_posix
@_POSIX_ONLY
def test_config_json_section_writes_fffd_for_a_bad_byte_and_warns(
    config_cli_context: Callable[[dict[str, Any]], Callable[[], Any]],
) -> None:
    """``--section`` takes the same path: the section is the top-level key, as before."""
    result = _config_json(config_cli_context({"section": {"key": "x"}, "other": {"k": 1}}), "--section", "section")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"section": {"key": "a" + _FFFD + "b"}}
    assert _warnings(result.stderr) == ["warning: output field section.key" + _TAIL]


@pytest.mark.os_agnostic
def test_clean_config_json_is_indented_as_before_with_no_warning(
    config_cli_context: Callable[[dict[str, Any]], Callable[[], Any]],
) -> None:
    """A clean configuration prints a blank line then exactly what lib_layered_config printed, no warning."""
    config_data: dict[str, Any] = {"section": {"key": "v", "token": "s3cret", "n": [1, 2], "u": "\u00e4"}}
    result = CliRunner().invoke(cli_mod.cli, ["config", "--format", "json"], obj=config_cli_context(config_data))
    assert result.exit_code == 0, result.output
    assert result.stdout == "\n" + Config(config_data, {}).to_json(indent=2, redact=True) + "\n"
    data = json.loads(result.stdout)
    assert data["section"]["key"] == "v"
    assert data["section"]["token"] != "s3cret"
    assert _warnings(result.stderr) == []


@pytest.mark.os_agnostic
def test_config_json_unknown_section_still_fails_with_its_message(
    config_cli_context: Callable[[dict[str, Any]], Callable[[], Any]],
) -> None:
    """An unknown --section is refused as before: exit 22 and the 'not found' line, empty JSON."""
    result = CliRunner().invoke(
        cli_mod.cli,
        ["config", "--format", "json", "--section", "missing"],
        obj=config_cli_context({"section": {"key": "v"}}),
    )
    assert result.exit_code == 22
    assert "Section 'missing' not found" in result.stderr
    assert "{" not in result.stdout
