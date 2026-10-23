"""Text bodies (``exec`` output, ``invoke_web_request`` body) are UTF-8 bytes when piped.

A German Windows console encodes a text-mode stdout in cp1252, so a piped ``pwshpy exec`` whose
child printed a character cp1252 cannot represent raised ``UnicodeEncodeError`` AFTER the child had
run, and the child's stderr came out as backslash escapes. These tests force a cp1252 stdio with
``PYTHONIOENCODING`` (the same text layer the Windows console gives) and read back the BYTES.

Non-ASCII values are built with chr() so the source stays ASCII.
"""

from __future__ import annotations

import http.server
import io
import os
import socketserver
import subprocess
import sys
import threading
from collections.abc import Iterator

import pytest

from pwshpy.adapters.cli.output import emit_text

#: Not representable in cp1252: the old code raised UnicodeEncodeError on stdout.
_BEYOND_CP1252 = chr(0x4E2D) + chr(0x6587) + " " + chr(0x2603)
#: Representable in cp1252, so cp1252 and UTF-8 bytes differ for it without an error.
_GERMAN = "Ger" + chr(0xE4) + "t " + chr(0xFC)

#: The web body carries a CRLF so the test also pins that the body's own bytes pass through.
_WEB_BODY = ("first " + _BEYOND_CP1252 + "\r\nsecond " + _GERMAN + "\n").encode("utf-8")


def _cp1252_env() -> dict[str, str]:
    # env= REPLACES the environment, so start from the full one (Windows needs SystemRoot).
    return {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}


def _run_pwshpy(*args: str) -> subprocess.CompletedProcess[bytes]:
    """Run ``python -m pwshpy ARGS`` with a cp1252 stdio, capturing raw bytes (a pipe, not a tty)."""
    return subprocess.run(  # noqa: S603 - fixed argv: this interpreter running pwshpy itself
        [sys.executable, "-m", "pwshpy", *args],
        capture_output=True,
        env=_cp1252_env(),
        timeout=120,
        check=False,
    )


def _child_writing(stream: str, text: str) -> list[str]:
    """Argv for a child that writes ``text`` as UTF-8 bytes to its ``stream`` ("stdout"/"stderr")."""
    codepoints = "+".join(f"chr({ord(char)})" for char in text)
    code = f"import sys; sys.{stream}.buffer.write(({codepoints}).encode('utf-8'))"
    return [sys.executable, "-c", code]


@pytest.mark.os_agnostic
def test_piped_exec_stdout_beyond_cp1252_is_utf8() -> None:
    """A child's non-cp1252 stdout passes through a piped exec as UTF-8 and the exit code is the child's."""
    completed = _run_pwshpy("exec", "--", *_child_writing("stdout", _BEYOND_CP1252 + "\n"))
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stdout == (_BEYOND_CP1252 + "\n").encode("utf-8")


@pytest.mark.os_agnostic
def test_piped_exec_stderr_beyond_cp1252_is_utf8() -> None:
    """A child's non-cp1252 stderr passes through as UTF-8, not as backslash escapes."""
    completed = _run_pwshpy("exec", "--", *_child_writing("stderr", _BEYOND_CP1252 + "\n"))
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stderr == (_BEYOND_CP1252 + "\n").encode("utf-8")


class _Utf8BodyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - matches base signature
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(_WEB_BODY)))
        self.end_headers()
        self.wfile.write(_WEB_BODY)


@pytest.fixture
def utf8_body_url() -> Iterator[str]:
    server = socketserver.TCPServer(("127.0.0.1", 0), _Utf8BodyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/body"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.os_agnostic
def test_piped_invoke_web_request_body_is_the_utf8_body(utf8_body_url: str) -> None:
    """A piped invoke_web_request writes the UTF-8 body byte for byte, its CRLF included."""
    completed = _run_pwshpy("invoke_web_request", utf8_body_url)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stdout == _WEB_BODY


class _TerminalStream(io.TextIOWrapper):
    """A cp1252 text stream that claims to be a terminal, standing in for a console."""

    def isatty(self) -> bool:
        return True


@pytest.mark.os_agnostic
def test_emit_text_at_a_terminal_stays_on_the_text_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """At a terminal the body goes through the stream's own encoding, as before."""
    sink = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", _TerminalStream(sink, encoding="cp1252", errors="strict", newline="\n"))
    emit_text(_GERMAN + "\n")
    sys.stdout.flush()
    assert sink.getvalue() == (_GERMAN + "\n").encode("cp1252")


@pytest.mark.os_agnostic
def test_emit_text_to_a_piped_stderr_is_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """err=True writes UTF-8 bytes to a non-terminal stderr, after anything it already held."""
    sink = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(sink, encoding="cp1252", errors="strict", newline="\n"))
    sys.stderr.write("before\n")
    emit_text(_BEYOND_CP1252, err=True)
    sys.stderr.flush()
    assert sink.getvalue() == b"before\n" + _BEYOND_CP1252.encode("utf-8")
