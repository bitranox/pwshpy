"""Every piped or redirected CLI line is UTF-8 with LF line ends, whatever the console code page.

A German Windows console encodes a text-mode stdout in cp1252, so ``pwshpy env | more`` raised
``UnicodeEncodeError`` (exit 87 there, 22 on Linux) on a value cp1252 cannot hold, as did every
status line naming such a path and ``pwshpy config``. The process entry now switches a piped
stdout and stderr to UTF-8 with LF line ends; a terminal keeps its own encoding. These tests force
a cp1252 stdio with ``PYTHONIOENCODING`` (the text layer the Windows console gives) and read back
the BYTES.

Non-ASCII values are built with chr() so the source stays ASCII.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pwshpy.adapters.cli.output import RecordFormat, emit, use_utf8_when_piped
from pwshpy.domain.records import EnvVar

#: Not representable in cp1252: the old code raised UnicodeEncodeError on stdout.
_BEYOND_CP1252 = chr(0x4E2D) + chr(0x6587) + " " + chr(0x2603)


def _run_pwshpy(
    *args: str, stdin: bytes = b"", extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run ``python -m pwshpy ARGS`` with a cp1252 stdio, capturing raw bytes (a pipe, not a tty)."""
    # env= REPLACES the environment, so start from the full one (Windows needs SystemRoot).
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0", **(extra_env or {})}
    return subprocess.run(  # noqa: S603 - fixed argv: this interpreter running pwshpy itself
        [sys.executable, "-m", "pwshpy", *args],
        input=stdin,
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )


def _ok(completed: subprocess.CompletedProcess[bytes]) -> bytes:
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


@pytest.mark.os_agnostic
def test_piped_human_table_holds_a_value_beyond_cp1252() -> None:
    """``pwshpy env`` piped prints the table with the value as UTF-8 instead of exiting 22/87."""
    stdout = _ok(_run_pwshpy("env", extra_env={"PWSHPY_PROBE_124": _BEYOND_CP1252}))
    assert _BEYOND_CP1252.encode("utf-8") in stdout


@pytest.mark.os_agnostic
def test_piped_human_table_ends_lines_in_lf() -> None:
    """The piped table has the same bytes on every OS: LF line ends, never CRLF."""
    stdout = _ok(_run_pwshpy("env", extra_env={"PWSHPY_PROBE_124": "plain"}))
    assert b"PWSHPY_PROBE_124" in stdout
    assert b"\r\n" not in stdout


@pytest.mark.os_agnostic
def test_piped_filesystem_status_lines_name_a_path_beyond_cp1252(tmp_path: Path) -> None:
    """new_item / copy_item / move_item / remove_item each print their path as UTF-8 with LF."""
    first = str(tmp_path / (_BEYOND_CP1252 + " a.txt"))
    second = str(tmp_path / (_BEYOND_CP1252 + " b.txt"))
    third = str(tmp_path / (_BEYOND_CP1252 + " c.txt"))
    assert _ok(_run_pwshpy("new_item", first)) == f"created file: {first}\n".encode()
    assert _ok(_run_pwshpy("copy_item", first, second)) == f"copied {first} -> {second}\n".encode()
    assert _ok(_run_pwshpy("move_item", second, third)) == f"moved {second} -> {third}\n".encode()
    assert _ok(_run_pwshpy("remove_item", third)) == f"removed {third}\n".encode()


@pytest.mark.os_agnostic
def test_piped_write_text_status_line_names_a_path_beyond_cp1252(tmp_path: Path) -> None:
    """write_text's 'wrote' line names a non-cp1252 destination as UTF-8, and the file is written."""
    dest = tmp_path / (_BEYOND_CP1252 + ".txt")
    stdout = _ok(_run_pwshpy("write_text", str(dest), stdin=b"body\n"))
    assert stdout.startswith(b"wrote ")
    assert stdout.endswith(dest.name.encode("utf-8") + b"\n")
    assert dest.read_bytes() == b"body\n"


@pytest.mark.os_agnostic
def test_piped_config_display_holds_a_value_beyond_cp1252() -> None:
    """``pwshpy config`` piped prints an env-layer value beyond cp1252 instead of exiting 22."""
    stdout = _ok(_run_pwshpy("config", extra_env={"PWSHPY___LIB_LAYERED_CONFIG__PROBE_124": _BEYOND_CP1252}))
    assert _BEYOND_CP1252.encode("utf-8") in stdout


@pytest.mark.os_agnostic
def test_piped_stderr_error_names_a_value_beyond_cp1252_as_utf8() -> None:
    """An error line naming a non-cp1252 value reaches a piped stderr as UTF-8, not backslash escapes."""
    completed = _run_pwshpy("config", "--section", _BEYOND_CP1252)
    assert completed.returncode != 0
    assert _BEYOND_CP1252.encode("utf-8") in completed.stderr


@pytest.mark.os_agnostic
def test_piped_human_table_frame_is_ascii_on_every_os(monkeypatch: pytest.MonkeyPatch) -> None:
    """A piped table is drawn with an ASCII frame, byte for byte the same on every OS.

    rich draws a heavy box on Linux and swaps in a square one for a legacy Windows console, both
    outside every legacy code page, so a reader decoding the pipe as cp850/cp1252 saw a garbled
    frame and the bytes differed by OS (user decision 2026-10-26: ASCII when piped).
    """
    # The Claude Code harness exports FORCE_COLOR; colour codes would hide the frame comparison.
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    sink = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(sink, encoding="utf-8", newline="\n"))
    emit([EnvVar(name="A", value="b")], RecordFormat.HUMAN)
    sys.stdout.flush()
    assert sink.getvalue() == (
        b"+--------------+\n| name | value |\n|------+-------|\n| A    | b     |\n+--------------+\n"
    )


class _TerminalStream(io.TextIOWrapper):
    """A cp1252 text stream that claims to be a terminal, standing in for a console."""

    def isatty(self) -> bool:
        return True


@pytest.mark.os_agnostic
def test_a_terminal_keeps_its_own_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that is a terminal is left on its own encoding."""
    terminal = _TerminalStream(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", terminal)
    use_utf8_when_piped()
    assert terminal.encoding == "cp1252"


@pytest.mark.os_agnostic
def test_a_piped_stream_becomes_utf8_and_keeps_its_error_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """A piped stdout and stderr switch to UTF-8 with LF, each keeping the error handler it had."""
    out_sink, err_sink = io.BytesIO(), io.BytesIO()
    piped_out = io.TextIOWrapper(out_sink, encoding="cp1252", errors="surrogateescape")
    piped_err = io.TextIOWrapper(err_sink, encoding="cp1252", errors="backslashreplace")
    monkeypatch.setattr(sys, "stdout", piped_out)
    monkeypatch.setattr(sys, "stderr", piped_err)
    use_utf8_when_piped()
    piped_out.write(_BEYOND_CP1252 + "\n")
    piped_out.flush()
    assert (piped_out.encoding, piped_out.errors) == ("utf-8", "surrogateescape")
    assert (piped_err.encoding, piped_err.errors) == ("utf-8", "backslashreplace")
    assert out_sink.getvalue() == (_BEYOND_CP1252 + "\n").encode("utf-8")


@pytest.mark.os_agnostic
def test_a_missing_or_foreign_stream_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """No stream (pythonw, a detached service) or a non-wrapper stand-in is skipped without error."""
    stand_in = io.StringIO()
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", stand_in)
    use_utf8_when_piped()
    assert sys.stderr is stand_in
