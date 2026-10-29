"""An HTTP error status (>= 400) raises, like Invoke-WebRequest / Invoke-RestMethod, unless skipped.

``skip_http_error_check=True`` (CLI ``--skip-http-error-check``, pwsh's ``-SkipHttpErrorCheck``)
returns the answer instead. A hermetic ``http.server`` on 127.0.0.1 serves every case.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from pwshpy.adapters.cli.root import cli
from pwshpy.adapters.native.web import invoke_rest_method, invoke_web_request
from pwshpy.composition import build_ps
from pwshpy.domain.errors import NativeCallError, OperationTimeoutError

if TYPE_CHECKING:
    from pwshpy.composition import AppServices

#: The entity tag the /etag route answers; a request naming it in If-None-Match gets 304.
_ETAG = '"v1"'

#: A body far longer than any excerpt, so a test can see the message stays bounded.
_LONG_BODY = b'{"error": "' + b"x" * 5000 + b'", "tail": "SECRET-TAIL"}'


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - matches the base signature; silences logging
        pass

    def do_GET(self) -> None:
        if self.path == "/etag" and self.headers.get("If-None-Match") == _ETAG:
            self.send_response(304)  # Not Modified: no body
            self.send_header("ETag", _ETAG)
            self.end_headers()
            return
        routes: dict[str, tuple[int, bytes]] = {
            "/notfound": (404, b'{"error": "no such thing"}'),
            "/teapot": (418, b"short and stout"),
            "/boom": (500, _LONG_BODY),
            "/multiline": (400, b"line one\r\nline two\n\x1b[31mred"),
            "/ok": (200, b'{"ok": true}'),
            "/empty-error": (503, b""),
        }
        status, body = routes.get(self.path, (200, b"{}"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
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
@pytest.mark.parametrize(
    ("path", "status"), [("/notfound", 404), ("/teapot", 418), ("/boom", 500), ("/empty-error", 503)]
)
def test_invoke_web_request_raises_on_an_http_error_status(base_url: str, path: str, status: int) -> None:
    """A 4xx or 5xx answer raises NativeCallError naming the status, like Invoke-WebRequest."""
    with pytest.raises(NativeCallError) as caught:
        invoke_web_request(base_url + path)
    assert f"HTTP {status}" in str(caught.value)
    assert not isinstance(caught.value, OperationTimeoutError)


@pytest.mark.os_agnostic
def test_invoke_rest_method_raises_on_an_http_error_status_instead_of_parsing_the_body(base_url: str) -> None:
    """A 404 with a JSON body is raised, not parsed and returned (Invoke-RestMethod raises)."""
    with pytest.raises(NativeCallError, match="HTTP 404"):
        invoke_rest_method(base_url + "/notfound")


@pytest.mark.os_agnostic
def test_the_error_message_carries_a_short_body_excerpt(base_url: str) -> None:
    """The start of the body is in the message, so a caller sees why the server refused."""
    with pytest.raises(NativeCallError) as caught:
        invoke_web_request(base_url + "/notfound")
    assert "no such thing" in str(caught.value)


@pytest.mark.os_agnostic
def test_the_body_excerpt_is_bounded(base_url: str) -> None:
    """A long error body is cut: the message stays short and never reaches the body's tail."""
    with pytest.raises(NativeCallError) as caught:
        invoke_web_request(base_url + "/boom")
    message = str(caught.value)
    assert "SECRET-TAIL" not in message
    assert len(message) < 500
    assert message.endswith("...")


@pytest.mark.os_agnostic
def test_the_body_excerpt_is_one_printable_line(base_url: str) -> None:
    """Line breaks and control characters from the body do not reach the message."""
    with pytest.raises(NativeCallError) as caught:
        invoke_web_request(base_url + "/multiline")
    message = str(caught.value)
    assert "\n" not in message
    assert "\r" not in message
    assert "\x1b" not in message
    assert "line one line two" in message


@pytest.mark.os_agnostic
def test_skip_http_error_check_returns_the_error_response(base_url: str) -> None:
    """skip_http_error_check=True returns the 4xx answer with its status and full body."""
    response = invoke_web_request(base_url + "/boom", skip_http_error_check=True)
    assert response.status_code == 500
    assert "SECRET-TAIL" in response.text


@pytest.mark.os_agnostic
def test_skip_http_error_check_lets_invoke_rest_method_parse_the_error_body(base_url: str) -> None:
    """With the check skipped, invoke_rest_method parses the error body like any other."""
    assert invoke_rest_method(base_url + "/notfound", skip_http_error_check=True) == {"error": "no such thing"}


@pytest.mark.os_agnostic
def test_a_success_status_is_unaffected(base_url: str) -> None:
    """A 2xx answer is returned whatever the flag (negative control)."""
    assert invoke_rest_method(base_url + "/ok") == {"ok": True}
    assert invoke_web_request(base_url + "/ok").status_code == 200


@pytest.mark.os_agnostic
def test_the_facade_forwards_skip_http_error_check(base_url: str) -> None:
    """ps.invoke_web_request / ps.invoke_rest_method raise by default and forward the flag."""
    ps = build_ps()
    with pytest.raises(NativeCallError, match="HTTP 404"):
        ps.invoke_web_request(base_url + "/notfound")
    with pytest.raises(NativeCallError, match="HTTP 404"):
        ps.invoke_rest_method(base_url + "/notfound")
    assert ps.invoke_web_request(base_url + "/notfound", skip_http_error_check=True).status_code == 404
    assert ps.invoke_rest_method(base_url + "/notfound", skip_http_error_check=True) == {"error": "no such thing"}


@pytest.mark.os_agnostic
@pytest.mark.parametrize("verb", ["invoke_web_request", "invoke_rest_method"])
def test_cli_exits_nonzero_on_an_http_error_status(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], base_url: str, verb: str
) -> None:
    """Without the flag the CLI fails on a 404 and prints nothing to stdout."""
    result = cli_runner.invoke(cli, [verb, f"{base_url}/notfound"], obj=production_factory)
    assert result.exit_code != 0
    assert isinstance(result.exception, NativeCallError)
    assert "HTTP 404" in str(result.exception)


@pytest.mark.os_agnostic
def test_cli_skip_http_error_check_prints_the_error_body(
    cli_runner: CliRunner, production_factory: Callable[[], AppServices], base_url: str
) -> None:
    """--skip-http-error-check returns the error answer: the body, or the record with --json."""
    body = cli_runner.invoke(
        cli, ["invoke_web_request", "--skip-http-error-check", f"{base_url}/teapot"], obj=production_factory
    )
    assert body.exit_code == 0, body.output
    assert "short and stout" in body.output
    record = cli_runner.invoke(
        cli, ["invoke_web_request", "--skip-http-error-check", "--json", f"{base_url}/teapot"], obj=production_factory
    )
    assert record.exit_code == 0, record.output
    assert json.loads(record.output)[0]["status_code"] == 418
    rest = cli_runner.invoke(
        cli, ["invoke_rest_method", "--skip-http-error-check", f"{base_url}/notfound"], obj=production_factory
    )
    assert rest.exit_code == 0, rest.output
    assert json.loads(rest.output) == {"error": "no such thing"}


@pytest.mark.os_agnostic
def test_a_304_not_modified_raises_like_pwsh(base_url: str) -> None:
    """304 is an HTTPError urllib does not follow; pwsh's Invoke-WebRequest fails on it too."""
    with pytest.raises(NativeCallError, match="HTTP 304") as caught:
        invoke_web_request(base_url + "/etag", headers={"If-None-Match": _ETAG})
    assert not isinstance(caught.value, OperationTimeoutError)


@pytest.mark.os_agnostic
def test_a_304_is_returned_when_the_check_is_skipped(base_url: str) -> None:
    """With skip_http_error_check the 304 answer itself comes back, empty body included."""
    response = invoke_web_request(base_url + "/etag", headers={"If-None-Match": _ETAG}, skip_http_error_check=True)
    assert (response.status_code, response.text) == (304, "")


@pytest.mark.os_agnostic
def test_a_conditional_request_that_does_not_match_gets_the_body(base_url: str) -> None:
    """Control: an entity tag that does not match is answered 200 with the body."""
    response = invoke_web_request(base_url + "/etag", headers={"If-None-Match": '"other"'})
    assert response.status_code == 200
