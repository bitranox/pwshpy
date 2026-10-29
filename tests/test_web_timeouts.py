"""Web verb timeouts raise OperationTimeoutError, like every other pwshpy timeout.

A hermetic stalling server on 127.0.0.1 accepts the connection and then sends nothing
(or only the headers), so the client's socket read runs into ``timeout``. The only
patched seam is ``urllib.request.urlopen`` (stdlib, an external edge) for the
connect-phase timeout urllib wraps in ``URLError``, which a local server cannot produce
hermetically.
"""

from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from pwshpy.adapters.cli.main import main
from pwshpy.adapters.native import web as web_mod
from pwshpy.adapters.native.web import download_file, invoke_rest_method, invoke_web_request
from pwshpy.composition import build_testing
from pwshpy.domain.errors import NativeCallError, OperationTimeoutError

_TIMEOUT = 0.3


class _StallingServer:
    """Accept each connection, read the request, optionally send headers, then stall."""

    def __init__(self, *, send_headers: bool) -> None:
        self._send_headers = send_headers
        self._release = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(5.0)
        self.port: int = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            conn.recv(65536)
            if self._send_headers:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 100\r\n\r\npartial")
            self._release.wait(10.0)

    def close(self) -> None:
        self._release.set()
        self._listener.close()
        self._thread.join(5.0)


@pytest.fixture(params=[False, True], ids=["no-response", "headers-then-stall"])
def stalling_url(request: pytest.FixtureRequest) -> Iterator[str]:
    server = _StallingServer(send_headers=bool(request.param))
    try:
        yield f"http://127.0.0.1:{server.port}/"
    finally:
        server.close()


@pytest.mark.os_agnostic
def test_invoke_web_request_timeout_is_operation_timeout(stalling_url: str) -> None:
    """A read that runs into ``timeout`` raises OperationTimeoutError carrying the limit."""
    with pytest.raises(OperationTimeoutError) as caught:
        invoke_web_request(stalling_url, timeout=_TIMEOUT)
    assert caught.value.timeout == _TIMEOUT
    assert "timed out" in str(caught.value)


@pytest.mark.os_agnostic
def test_invoke_rest_method_timeout_is_operation_timeout(stalling_url: str) -> None:
    """invoke_rest_method shares the request path, so its timeout is an OperationTimeoutError too."""
    with pytest.raises(OperationTimeoutError) as caught:
        invoke_rest_method(stalling_url, timeout=_TIMEOUT)
    assert caught.value.timeout == _TIMEOUT


@pytest.mark.os_agnostic
def test_download_file_timeout_is_operation_timeout_and_leaves_no_file(stalling_url: str, tmp_path: Path) -> None:
    """A stalled download raises OperationTimeoutError and deletes the partial destination."""
    dest = tmp_path / "out.bin"
    with pytest.raises(OperationTimeoutError) as caught:
        download_file(stalling_url, dest, timeout=_TIMEOUT)
    assert caught.value.timeout == _TIMEOUT
    assert not dest.exists()


def _raise_wrapped_timeout(*_args: object, **_kwargs: object) -> object:
    # What urllib's do_open raises when the CONNECT runs into the timeout: the socket
    # timeout wrapped as URLError.reason.
    raise urllib.error.URLError(TimeoutError("timed out"))


@pytest.mark.os_agnostic
def test_connect_timeout_wrapped_in_urlerror_is_operation_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """urllib wraps a connect-phase timeout in URLError; it still maps to OperationTimeoutError."""
    monkeypatch.setattr(urllib.request, "urlopen", _raise_wrapped_timeout)
    with pytest.raises(OperationTimeoutError) as caught:
        invoke_web_request("http://192.0.2.1/", timeout=_TIMEOUT)
    assert caught.value.timeout == _TIMEOUT


@pytest.mark.os_agnostic
def test_connect_timeout_wrapped_in_urlerror_is_operation_timeout_for_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The download path classifies the wrapped connect timeout the same way."""
    monkeypatch.setattr(urllib.request, "urlopen", _raise_wrapped_timeout)
    with pytest.raises(OperationTimeoutError):
        download_file("http://192.0.2.1/", tmp_path / "x", timeout=_TIMEOUT)


@pytest.mark.os_agnostic
def test_refused_connection_stays_a_plain_native_call_error() -> None:
    """A transport failure that is not a timeout keeps raising plain NativeCallError (negative control)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    # A generous timeout: Windows retries a refused connect for about two seconds before it reports
    # WSAECONNREFUSED (measured on a Windows 11 VM: at 0.3 s the connect timed out first).
    with pytest.raises(NativeCallError) as caught:
        invoke_web_request(f"http://127.0.0.1:{closed_port}/", timeout=15.0)
    assert not isinstance(caught.value, OperationTimeoutError)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("verb", ["invoke_web_request", "invoke_rest_method", "download_file"])
def test_a_timed_out_web_verb_exits_110_through_main(
    verb: str, stalling_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through the real main(), a web timeout exits 110 like every other OperationTimeoutError."""
    argv = [verb, "--timeout", str(_TIMEOUT), stalling_url]
    if verb == "download_file":
        argv.append(str(tmp_path / "out.bin"))

    assert main(argv, services_factory=build_testing) == 110
    assert "OperationTimeoutError" in capsys.readouterr().err


@pytest.mark.os_agnostic
def test_web_module_exports_unchanged() -> None:
    """The adapter's public surface is the three verbs."""
    assert sorted(web_mod.__all__) == ["download_file", "invoke_rest_method", "invoke_web_request"]
