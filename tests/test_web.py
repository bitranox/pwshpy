"""native web requests: os_agnostic tests against a hermetic local HTTP server.

Spins a throwaway ``http.server`` on 127.0.0.1 in a thread, so no real network is
touched - deterministic on every OS.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from pwshpy.adapters.native.web import download_file, invoke_rest_method, invoke_web_request
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import WebResponse


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - matches the base signature; silences logging
        pass

    def do_GET(self) -> None:
        if self.path == "/json":
            self._respond(200, b'{"ok": true, "path": "/json"}', "application/json")
        elif self.path == "/notfound":
            self._respond(404, b"missing", "text/plain")
        else:
            self._respond(200, b"hello", "text/plain")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self._respond(200, self.rfile.read(length), "application/json")  # echo the body back

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def base_url() -> Iterator[str]:
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.os_agnostic
def test_invoke_web_request_returns_typed_response(base_url: str) -> None:
    """A 200 returns a WebResponse with the body text and status."""
    result = invoke_web_request(f"{base_url}/hello")
    assert isinstance(result, WebResponse)
    assert result.status_code == 200
    assert result.text == "hello"


@pytest.mark.os_agnostic
def test_invoke_web_request_http_error_is_returned_not_raised(base_url: str) -> None:
    """A 404 comes back as a WebResponse (status + body), not an exception."""
    result = invoke_web_request(f"{base_url}/notfound")
    assert result.status_code == 404
    assert "missing" in result.text


@pytest.mark.os_agnostic
def test_invoke_rest_method_parses_json(base_url: str) -> None:
    """invoke_rest_method returns the parsed JSON body."""
    assert invoke_rest_method(f"{base_url}/json") == {"ok": True, "path": "/json"}


@pytest.mark.os_agnostic
def test_invoke_rest_method_posts_json_body(base_url: str) -> None:
    """A json_body is serialized, sent, and (here) echoed back parsed."""
    assert invoke_rest_method(f"{base_url}/", method="POST", json_body={"a": 1, "b": [2, 3]}) == {"a": 1, "b": [2, 3]}


@pytest.mark.os_agnostic
def test_download_file_streams_to_disk(base_url: str, tmp_path: Path) -> None:
    """download_file writes the response body to a file (streamed in chunks)."""
    dest = tmp_path / "out.bin"
    result = download_file(f"{base_url}/hello", dest, chunk_size=2)  # tiny chunks to exercise the loop
    assert result == dest
    assert dest.read_bytes() == b"hello"


@pytest.mark.os_agnostic
def test_download_file_404_raises_and_leaves_no_file(base_url: str, tmp_path: Path) -> None:
    """A 4xx download raises NativeCallError and does not leave a partial file behind."""
    dest = tmp_path / "missing.bin"
    with pytest.raises(NativeCallError):
        download_file(f"{base_url}/notfound", dest)
    assert not dest.exists()


@pytest.mark.os_agnostic
def test_download_file_bad_scheme_raises(tmp_path: Path) -> None:
    """A non-http(s) scheme is rejected before any file is opened."""
    with pytest.raises(NativeCallError):
        download_file("file:///etc/passwd", tmp_path / "x")


@pytest.mark.os_agnostic
def test_non_http_scheme_raises() -> None:
    """A non-http(s) scheme is rejected up front."""
    with pytest.raises(NativeCallError):
        invoke_web_request("file:///etc/passwd")


@pytest.mark.os_agnostic
def test_transport_failure_raises() -> None:
    """A connection failure surfaces as NativeCallError."""
    with pytest.raises(NativeCallError):
        invoke_web_request("http://127.0.0.1:1/", timeout=1.0)
