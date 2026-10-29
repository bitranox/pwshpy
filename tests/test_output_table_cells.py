"""The human table's cells: undecodable bytes as U+FFFD, and lists of records or mappings compacted.

[153] A POSIX environment variable (file, process or account name) holding a byte that is not valid
UTF-8 reaches Python as a lone surrogate. ``--json`` / ``--jsonl`` write U+FFFD for each such byte
and warn on stderr; the human table wrote the raw byte (through stdout's ``surrogateescape``
handler), so a redirected table was not valid UTF-8 and the two output modes disagreed. The table
now applies the same rule, with the same warning line.

[126] A field holding a list of records or mappings (``WindowsUpdate.category_details``) printed as
a Python list-of-dicts repr; it now reads ``k=v, k=v; k=v, k=v`` of the set fields, like a nested
record already did.

Non-ASCII values are built with chr() so the source stays ASCII.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pwshpy.adapters.cli.output import RecordFormat, emit
from pwshpy.domain.records import (
    CimInstance,
    EnvVar,
    PSInvocationResult,
    PSObjectRecord,
    PSRecord,
    UpdateCategory,
    WindowsUpdate,
)

#: The code point os.environ / os.fsdecode hold for the undecodable byte 0xFF.
_BAD = chr(0xDCFF)
#: What each undecodable byte is written as.
_FFFD = chr(0xFFFD)
#: The warning text after the place it names.
_TAIL = ": not valid UTF-8; each undecodable byte written as U+FFFD"

_SECURITY = UpdateCategory(
    category_id="0fa1201d-4330-4fa8-8ae9-b877473b6441", type="UpdateClassification", name="Security Updates"
)
_PRODUCT = UpdateCategory(category_id="72e7624a-5b00-45d2-b92f-e561c0a6a160", type="Product", name="Windows 11")


def _table(capsys: pytest.CaptureFixture[str], *records: PSRecord) -> tuple[str, str]:
    emit(records, RecordFormat.HUMAN)
    captured = capsys.readouterr()
    return captured.out, captured.err


def _warnings(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("warning:")]


@pytest.fixture(autouse=True)
def _no_explicit_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    # The piped table sizes itself to its content; an inherited COLUMNS would cut long cells.
    monkeypatch.delenv("COLUMNS", raising=False)


# --- [126] lists of records or mappings ------------------------------------------------------------


@pytest.mark.os_agnostic
def test_a_list_of_records_reads_as_key_value_groups(capsys: pytest.CaptureFixture[str]) -> None:
    """WindowsUpdate.category_details: one 'k=v, k=v' group per category, groups joined by '; '."""
    update = WindowsUpdate(update_id="u", title="t", category_details=(_SECURITY, _PRODUCT))
    out, _ = _table(capsys, update)
    assert (
        "category_id=0fa1201d-4330-4fa8-8ae9-b877473b6441, type=UpdateClassification, name=Security Updates; "
        "category_id=72e7624a-5b00-45d2-b92f-e561c0a6a160, type=Product, name=Windows 11"
    ) in out
    assert "{" not in out
    assert "'category_id'" not in out


@pytest.mark.os_agnostic
def test_a_list_of_mappings_keeps_only_the_set_fields(capsys: pytest.CaptureFixture[str]) -> None:
    """A mapping in a list (PSInvocationResult.output) drops its None entries, as a nested record does."""
    result = PSInvocationResult(output=[{"a": 1, "b": None}, PSObjectRecord(type_name="T", properties={})])
    out, _ = _table(capsys, result)
    assert "a=1; type_name=T, properties={}" in out
    assert "None" not in out


@pytest.mark.os_agnostic
def test_a_list_that_is_not_only_records_or_mappings_keeps_its_rendering(capsys: pytest.CaptureFixture[str]) -> None:
    """Scalars, a mixed list and an empty list are shown as before; so is a plain mapping field."""
    out, _ = _table(capsys, PSInvocationResult(output=[1, {"a": 1}], errors=["e1", "e2"]))
    assert "[1, {'a': 1}]" in out
    assert "['e1', 'e2']" in out
    assert "[]" in out
    out, _ = _table(capsys, CimInstance(class_name="c", properties={"k": 1}))
    assert "{'k': 1}" in out


# --- [153] undecodable bytes -----------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_a_table_cell_writes_fffd_per_undecodable_byte_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    """The cell holds U+FFFD where the byte was, and stderr names record and field, never the value."""
    out, err = _table(capsys, EnvVar(name="A", value="1"), EnvVar(name="SECRETNAME", value="hunter2" + _BAD + _BAD))
    assert "hunter2" + _FFFD + _FFFD in out
    assert _BAD not in out
    assert _warnings(err) == ["warning: EnvVar record 2, field value" + _TAIL]
    assert "hunter2" not in err


@pytest.mark.os_agnostic
def test_a_table_cell_repairs_keys_and_values_inside_compacted_and_plain_containers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bad byte in a compacted list, or in a plain mapping's key, is U+FFFD there too."""
    result = PSInvocationResult(output=[{"k" + _BAD: "v" + _BAD}])
    out, err = _table(capsys, result, CimInstance(class_name="c", properties={"p" + _BAD: 1}))
    assert "k" + _FFFD + "=v" + _FFFD in out
    assert "{'p" + _FFFD + "': 1}" in out
    assert _BAD not in out
    assert _warnings(err) == [
        "warning: PSInvocationResult record 1, field output.0.k?" + _TAIL,
        "warning: CimInstance record 2, field properties.p?" + _TAIL,
    ]


@pytest.mark.os_agnostic
def test_a_clean_table_prints_no_warning(capsys: pytest.CaptureFixture[str]) -> None:
    """No replacement, no warning."""
    _, err = _table(capsys, EnvVar(name="A", value="caf" + chr(0xE9)))
    assert err == ""


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="a Windows environment cannot hold a byte that is not valid UTF-8")
def test_env_redirected_to_a_file_writes_valid_utf8_with_fffd(tmp_path: Path) -> None:
    """`pwshpy env > file` with PWSHPY_PROBE_153=a<0xFF>b: the file is UTF-8 and holds a U+FFFD b."""
    target = tmp_path / "env.txt"
    # env= REPLACES the environment, so start from the full one. A str holding a lone surrogate
    # reaches the child as the raw byte 0xFF (subprocess encodes it with os.fsencode).
    env = {**os.environ, "PWSHPY_PROBE_153": "a" + _BAD + "b"}
    env.pop("COLUMNS", None)
    with target.open("wb") as handle:
        completed = subprocess.run(
            [sys.executable, "-m", "pwshpy", "env"],
            stdout=handle,
            stderr=subprocess.PIPE,
            env=env,
            timeout=120,
            check=False,
        )
    stderr = completed.stderr.decode("utf-8", errors="replace")
    assert completed.returncode == 0, stderr
    data = target.read_bytes()
    assert b"\xff" not in data
    text = data.decode("utf-8")
    assert "a" + _FFFD + "b" in text
    assert any(line.endswith("field value" + _TAIL) for line in _warnings(stderr)), stderr
