"""CLI tests for invoke_web_request / invoke_rest_method against a hermetic local server."""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli

if TYPE_CHECKING:
    from pwshpy.composition import AppServices


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - matches base signature
        pass

    def do_GET(self) -> None:
        if self.path == "/json":
            self._respond(200, b'{"ok": true}', "application/json")
        else:
            self._respond(200, b"hello", "text/plain")

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
def test_invoke_web_request_prints_body(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], base_url: str
) -> None:
    """invoke_web_request prints the body by default and the record with --json."""
    body = cli_runner.invoke(cli, ["invoke_web_request", f"{base_url}/hello"], obj=production_factory)
    assert body.exit_code == 0, body.output
    assert "hello" in body.output

    as_json = cli_runner.invoke(cli, ["invoke_web_request", f"{base_url}/hello", "--json"], obj=production_factory)
    row = json.loads(next(line for line in as_json.stdout.splitlines() if line.strip().startswith("[")))
    assert row[0]["status_code"] == 200


@pytest.mark.os_agnostic
def test_invoke_rest_method_prints_json(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], base_url: str
) -> None:
    """invoke_rest_method prints the parsed JSON body."""
    result = cli_runner.invoke(cli, ["invoke_rest_method", f"{base_url}/json"], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout.strip()) == {"ok": True}


@pytest.mark.os_agnostic
def test_download_file_cli(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], base_url: str, tmp_path: Path
) -> None:
    """download_file streams a URL to DEST and reports the path."""
    dest = tmp_path / "got.bin"
    result = cli_runner.invoke(cli, ["download_file", f"{base_url}/hello", str(dest)], obj=production_factory)
    assert result.exit_code == 0, result.output
    assert dest.read_bytes() == b"hello"
    assert "downloaded" in result.output
