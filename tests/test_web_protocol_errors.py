"""A broken HTTP answer, a refused redirect and a failed download are pwshpy errors, and leave files alone.

Hermetic: a one-shot socket server on 127.0.0.1 answers each case with exactly the bytes it needs
(a non-HTTP greeting, a body shorter than its Content-Length, a redirect to itself), and a refused
connect uses a port that was just closed. Nothing reaches a public address.

* ``http.client.HTTPException`` (``BadStatusLine``, ``IncompleteRead``) is not an ``OSError``, so it
  escaped the web verbs raw.
* urllib refuses a redirect loop by raising ``HTTPError`` with the 3xx code; below 400 that came
  back as a successful ``WebResponse``.
* urllib's default opener follows a redirect to ``ftp://``; only http/https may be followed.
* ``download_file`` deleted ``dest`` on any failure, also one that came before it opened the file.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from pwshpy.adapters.native.web import download_file, invoke_rest_method, invoke_web_request
from pwshpy.domain.errors import NativeCallError, OperationTimeoutError

#: Generous: Windows retries a refused connect for about two seconds before it reports it.
_TIMEOUT = 15.0


class _OneShotServer:
    """Answer each connection with ``respond(port)`` and close it."""

    def __init__(self, respond: Callable[[int], bytes]) -> None:
        self._respond = respond
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        # Short, so the serving loop sees the stop event: closing a listener does not wake accept().
        self._listener.settimeout(0.1)
        self.port: int = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(5.0)
                conn.recv(65536)
                conn.sendall(self._respond(self.port))

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5.0)
        self._listener.close()


@pytest.fixture
def serve() -> Iterator[Callable[[Callable[[int], bytes]], str]]:
    servers: list[_OneShotServer] = []

    def start(respond: Callable[[int], bytes]) -> str:
        server = _OneShotServer(respond)
        servers.append(server)
        return f"http://127.0.0.1:{server.port}/x"

    yield start
    for server in servers:
        server.close()


def _not_http(_port: int) -> bytes:
    return b"SSH-2.0-OpenSSH_9.6\r\n"


def _short_body(_port: int) -> bytes:
    return b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 100000\r\n\r\npartial"


def _redirect_to_self(port: int) -> bytes:
    return f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{port}/x\r\nContent-Length: 0\r\n\r\n".encode()


def _assert_plain_native_error(error: BaseException) -> None:
    assert isinstance(error, NativeCallError)
    assert not isinstance(error, OperationTimeoutError)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("respond", [_not_http, _short_body], ids=["bad-status-line", "incomplete-read"])
@pytest.mark.parametrize("verb", [invoke_web_request, invoke_rest_method], ids=["web", "rest"])
def test_a_broken_http_answer_raises_native_call_error(
    serve: Callable[[Callable[[int], bytes]], str], respond: Callable[[int], bytes], verb: Callable[..., object]
) -> None:
    """``BadStatusLine`` / ``IncompleteRead`` are mapped through native_error, the cause kept."""
    with pytest.raises(NativeCallError) as caught:
        verb(serve(respond), timeout=_TIMEOUT)
    _assert_plain_native_error(caught.value)
    assert caught.value.__cause__ is not None


@pytest.mark.os_agnostic
@pytest.mark.parametrize("respond", [_not_http, _short_body], ids=["bad-status-line", "incomplete-read"])
def test_a_broken_download_raises_and_leaves_no_partial_file(
    serve: Callable[[Callable[[int], bytes]], str], respond: Callable[[int], bytes], tmp_path: Path
) -> None:
    """The download maps the error too, and removes the partial file it wrote."""
    dest = tmp_path / "out.bin"
    with pytest.raises(NativeCallError) as caught:
        download_file(serve(respond), dest, timeout=_TIMEOUT)
    _assert_plain_native_error(caught.value)
    assert not dest.exists()


@pytest.mark.os_agnostic
def test_a_redirect_urllib_refuses_raises(serve: Callable[[Callable[[int], bytes]], str]) -> None:
    """A 3xx urllib will not follow (a loop) is an error, not a successful WebResponse.

    A redirect to a non-http scheme is refused even earlier, by the http/https rule (below).
    """
    with pytest.raises(NativeCallError, match="HTTP 302") as caught:
        invoke_web_request(serve(_redirect_to_self), timeout=_TIMEOUT)
    _assert_plain_native_error(caught.value)


@pytest.mark.os_agnostic
def test_a_refused_redirect_is_returned_when_the_check_is_skipped(
    serve: Callable[[Callable[[int], bytes]], str],
) -> None:
    """``skip_http_error_check`` returns the 3xx answer itself, as it returns a 4xx."""
    response = invoke_web_request(serve(_redirect_to_self), timeout=_TIMEOUT, skip_http_error_check=True)
    assert response.status_code == 302


def _closed_port_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/important.bin"


@pytest.mark.os_agnostic
def test_a_download_that_fails_before_opening_dest_keeps_an_existing_file(tmp_path: Path) -> None:
    """A refused connect never touched ``dest``, so an existing file there is left as it was."""
    dest = tmp_path / "important.bin"
    dest.write_bytes(b"keep me")
    with pytest.raises(NativeCallError):
        download_file(_closed_port_url(), dest, timeout=_TIMEOUT)
    assert dest.read_bytes() == b"keep me"


@pytest.mark.os_agnostic
def test_a_download_refused_with_an_http_error_keeps_an_existing_file(
    serve: Callable[[Callable[[int], bytes]], str], tmp_path: Path
) -> None:
    """An HTTP error arrives before the body is copied: ``dest`` was never opened, so it stays."""
    dest = tmp_path / "important.bin"
    dest.write_bytes(b"keep me")
    with pytest.raises(NativeCallError, match="HTTP 302"):
        download_file(serve(_redirect_to_self), dest, timeout=_TIMEOUT)
    assert dest.read_bytes() == b"keep me"


# --- a redirect may only lead to http/https (N1) -------------------------------------------------


@pytest.fixture
def untouched_listener() -> Iterator[socket.socket]:
    """A local listener nothing may connect to: the refused redirect points at it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield listener


def _assert_never_connected(listener: socket.socket) -> None:
    listener.setblocking(False)
    with pytest.raises(BlockingIOError):
        listener.accept()


def _redirect_to(location: str) -> Callable[[int], bytes]:
    def respond(_port: int) -> bytes:
        return f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()

    return respond


@pytest.mark.os_agnostic
@pytest.mark.parametrize("skip", [False, True], ids=["checked", "skip-http-error-check"])
@pytest.mark.parametrize("verb", [invoke_web_request, invoke_rest_method], ids=["web", "rest"])
def test_a_redirect_to_ftp_is_refused_before_any_connect(
    serve: Callable[[Callable[[int], bytes]], str],
    untouched_listener: socket.socket,
    verb: Callable[..., object],
    skip: bool,
) -> None:
    """urllib's default opener follows a redirect to ftp://; the http/https-only rule refuses it."""
    target = f"ftp://127.0.0.1:{untouched_listener.getsockname()[1]}/x"
    with pytest.raises(NativeCallError, match="redirect") as caught:
        verb(serve(_redirect_to(target)), timeout=_TIMEOUT, skip_http_error_check=skip)
    _assert_plain_native_error(caught.value)
    assert "ftp" in str(caught.value)
    _assert_never_connected(untouched_listener)


@pytest.mark.os_agnostic
def test_a_download_redirected_to_ftp_is_refused_and_writes_nothing(
    serve: Callable[[Callable[[int], bytes]], str], untouched_listener: socket.socket, tmp_path: Path
) -> None:
    """The download path refuses it too, before ``dest`` is opened."""
    target = f"ftp://127.0.0.1:{untouched_listener.getsockname()[1]}/x"
    dest = tmp_path / "out.bin"
    with pytest.raises(NativeCallError, match="redirect"):
        download_file(serve(_redirect_to(target)), dest, timeout=_TIMEOUT)
    assert not dest.exists()
    _assert_never_connected(untouched_listener)


@pytest.mark.os_agnostic
def test_a_redirect_to_file_is_refused_even_when_the_check_is_skipped(
    serve: Callable[[Callable[[int], bytes]], str],
) -> None:
    """skip_http_error_check returns HTTP error answers; it never makes a non-http redirect acceptable."""
    with pytest.raises(NativeCallError, match="redirect"):
        invoke_web_request(serve(_redirect_to("file:///etc/hostname")), timeout=_TIMEOUT, skip_http_error_check=True)


@pytest.mark.os_agnostic
def test_a_redirect_to_http_is_still_followed(serve: Callable[[Callable[[int], bytes]], str]) -> None:
    """Control: an http redirect is followed as before (the target answers 200)."""

    def ok(_port: int) -> bytes:
        return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"

    target = serve(ok)
    response = invoke_web_request(serve(_redirect_to(target)), timeout=_TIMEOUT)
    assert (response.status_code, response.text) == (200, "ok")
