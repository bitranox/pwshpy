"""Oracle: ``invoke_web_request`` / ``invoke_rest_method`` vs the real cmdlets on bodiless answers.

pwshpy raises for every status urllib does not follow, 304 included, unless
``skip_http_error_check``; the claim is that pwsh 7's ``Invoke-WebRequest`` does the same (it fails
on 304 and returns it under ``-SkipHttpErrorCheck``). Both clients ask one local ``http.server``
that answers 304 to a matching ``If-None-Match``, each with and without the skip switch, and the
outcomes are compared as data: raised or not, and the numeric status. ``Invoke-RestMethod`` is
asked the same, plus an empty, a whitespace-only and a literal ``null`` 200, and compared on
raised-or-not and the kind of result. That arm pins a deliberate divergence: where pwsh returns the
blank body text (``""``), pwshpy returns None. No message text is read, because the error text is
localised (the dev box is German Windows).

``local_only`` + ``os_windows``: it needs pwsh 7 (``-SkipHttpErrorCheck`` does not exist in Windows
PowerShell 5.1). Read-only: nothing on the host changes.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest

from pwshpy.adapters.native.web import invoke_rest_method, invoke_web_request
from pwshpy.domain.errors import NativeCallError

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; guards a stray run
    pytest.skip("the web pwsh oracle is Windows-only", allow_module_level=True)

_PWSH7 = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
_ETAG = '"v1"'

#: Both arms for Invoke-WebRequest; the URL and the entity tag arrive through the environment, so
#: nothing is quoted into the script. Only numbers and booleans leave it.
_PWSH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$headers = @{ 'If-None-Match' = $env:PWSHPY_ORACLE_ETAG }
function Probe([bool] $skip) {
    try {
        $answer = Invoke-WebRequest -Uri $env:PWSHPY_ORACLE_URL -Headers $headers -NoProxy `
            -SkipHttpErrorCheck:$skip -TimeoutSec 15
        return [ordered]@{ raised = $false; status = [int]$answer.StatusCode }
    } catch {
        $status = $null
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        return [ordered]@{ raised = $true; status = $status }
    }
}
[ordered]@{ checked = (Probe $false); skipped = (Probe $true) } | ConvertTo-Json -Compress
"""

#: Invoke-RestMethod against one URL, with and without the skip switch; reports whether it raised
#: and the kind of result: null, blank text (an empty or whitespace-only string) or anything else.
_PWSH_REST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$headers = @{ 'If-None-Match' = $env:PWSHPY_ORACLE_ETAG }
function Probe([bool] $skip) {
    try {
        $parsed = Invoke-RestMethod -Uri $env:PWSHPY_ORACLE_URL -Headers $headers -NoProxy `
            -SkipHttpErrorCheck:$skip -TimeoutSec 15
        $kind = 'other'
        if ($null -eq $parsed) { $kind = 'null' }
        elseif ($parsed -is [string] -and $parsed.Trim() -eq '') { $kind = 'blank-text' }
        return [ordered]@{ raised = $false; kind = $kind }
    } catch {
        return [ordered]@{ raised = $true; kind = $null }
    }
}
[ordered]@{ checked = (Probe $false); skipped = (Probe $true) } | ConvertTo-Json -Compress
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - matches the base signature; silences logging
        pass

    def do_GET(self) -> None:
        blank = {"/empty": b"", "/blank": b" \r\n\t ", "/null": b"null"}.get(self.path)
        if blank is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blank)))
            self.end_headers()
            self.wfile.write(blank)
            return
        if self.headers.get("If-None-Match") == _ETAG:
            self.send_response(304)
            self.send_header("ETag", _ETAG)
            self.end_headers()
            return
        body = b"fresh"
        self.send_response(200)
        self.send_header("ETag", _ETAG)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def base_url() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _pwshpy_probe(url: str, *, skip: bool) -> dict[str, object]:
    try:
        response = invoke_web_request(url, headers={"If-None-Match": _ETAG}, timeout=15, skip_http_error_check=skip)
    except NativeCallError as exc:
        cause = exc.__cause__
        status = cause.code if isinstance(cause, urllib.error.HTTPError) else None
        return {"raised": True, "status": status}
    return {"raised": False, "status": response.status_code}


def _pwshpy_rest_probe(url: str, *, skip: bool) -> dict[str, object]:
    try:
        parsed = invoke_rest_method(url, headers={"If-None-Match": _ETAG}, timeout=15, skip_http_error_check=skip)
    except NativeCallError:
        return {"raised": True, "kind": None}
    return {"raised": False, "kind": "null" if parsed is None else "other"}


def _pwsh_probe(url: str, script: str = _PWSH_SCRIPT) -> dict[str, dict[str, object]]:
    env = {**os.environ, "PWSHPY_ORACLE_URL": url, "PWSHPY_ORACLE_ETAG": _ETAG}
    completed = subprocess.run(  # noqa: S603 - fixed pwsh argv with a constant script; test oracle only
        [str(_PWSH7), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=True,
        timeout=120,
    )
    return json.loads(completed.stdout)


@pytest.mark.skipif(not _PWSH7.exists(), reason="needs pwsh 7 (-SkipHttpErrorCheck)")
def test_a_304_behaves_as_invoke_web_request_does(base_url: str) -> None:
    """Without the skip switch both raise on 304; with it both return the 304 answer."""
    etag_url = base_url + "/etag"
    theirs = _pwsh_probe(etag_url)
    ours = {"checked": _pwshpy_probe(etag_url, skip=False), "skipped": _pwshpy_probe(etag_url, skip=True)}
    assert theirs == {"checked": {"raised": True, "status": 304}, "skipped": {"raised": False, "status": 304}}
    assert ours == theirs


@pytest.mark.skipif(not _PWSH7.exists(), reason="needs pwsh 7 (-SkipHttpErrorCheck)")
@pytest.mark.parametrize(
    ("path", "measured"),
    [
        ("/etag", {"checked": {"raised": True, "kind": None}, "skipped": {"raised": False, "kind": "blank-text"}}),
        (
            "/empty",
            {"checked": {"raised": False, "kind": "blank-text"}, "skipped": {"raised": False, "kind": "blank-text"}},
        ),
        (
            "/blank",
            {"checked": {"raised": False, "kind": "blank-text"}, "skipped": {"raised": False, "kind": "blank-text"}},
        ),
        ("/null", {"checked": {"raised": False, "kind": "null"}, "skipped": {"raised": False, "kind": "null"}}),
    ],
)
def test_a_bodiless_answer_behaves_as_invoke_rest_method_does_but_gives_none(
    base_url: str, path: str, measured: dict[str, dict[str, object]]
) -> None:
    """Raised-or-not matches the cmdlet; where it returns blank body text, pwshpy returns None.

    A literal ``null`` body is the control: both give null, so the mapping below is the only
    difference.
    """
    url = base_url + path
    theirs = _pwsh_probe(url, _PWSH_REST_SCRIPT)
    ours = {"checked": _pwshpy_rest_probe(url, skip=False), "skipped": _pwshpy_rest_probe(url, skip=True)}
    assert theirs == measured
    expected_ours = {
        arm: {"raised": outcome["raised"], "kind": "null" if outcome["kind"] == "blank-text" else outcome["kind"]}
        for arm, outcome in theirs.items()
    }
    assert ours == expected_ours
