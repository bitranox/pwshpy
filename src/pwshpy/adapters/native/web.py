"""native web requests over the standard library (portable, no extra dependency).

Typed equivalents of ``Invoke-WebRequest`` (returns a :class:`WebResponse`) and
``Invoke-RestMethod`` (returns the parsed JSON).  Built on ``urllib.request`` +
``orjson``; the URL scheme is restricted to http/https.

Contents:
    * :func:`invoke_web_request` - an HTTP request returning a typed WebResponse.
    * :func:`invoke_rest_method` - an HTTP request returning parsed JSON.
"""

from __future__ import annotations

import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import orjson

from ...domain.errors import NativeCallError
from ...domain.records import WebResponse


def _require_http_scheme(url: str) -> None:
    """Reject any non-http(s) URL up front (blocks file://, ftp://, etc.)."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise NativeCallError(f"unsupported URL scheme {scheme!r}; only http/https are allowed")


def invoke_web_request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: str | None = None,
    timeout: float = 30.0,
) -> WebResponse:
    """Perform an HTTP request and return a typed :class:`WebResponse` (like Invoke-WebRequest).

    A 4xx/5xx response is returned (with its body/status), not raised - only a
    transport-level failure raises :class:`~pwshpy.domain.errors.NativeCallError`.

    Example:
        >>> callable(invoke_web_request)
        True
    """
    _require_http_scheme(url)
    request = urllib.request.Request(  # noqa: S310 - scheme restricted to http/https above
        url,
        data=body.encode("utf-8") if body is not None else None,
        method=method,
        headers=dict(headers or {}),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310 - http/https only
            raw = response.read()
            return WebResponse(
                url=url,
                status_code=int(response.status),
                text=raw.decode("utf-8", errors="replace"),
                headers=dict(response.headers),
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return WebResponse(
            url=url,
            status_code=int(exc.code),
            text=raw.decode("utf-8", errors="replace"),
            headers=dict(exc.headers),
        )
    except (urllib.error.URLError, OSError) as exc:
        raise NativeCallError(f"web request to {url!r} failed: {exc}") from exc


def invoke_rest_method(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    timeout: float = 30.0,
) -> Any:
    """Perform an HTTP request and return the parsed JSON body (like Invoke-RestMethod).

    ``json_body`` is serialized and sent with a ``Content-Type: application/json``
    header.  Raises :class:`~pwshpy.domain.errors.NativeCallError` if the response
    body is not valid JSON.

    Example:
        >>> callable(invoke_rest_method)
        True
    """
    outgoing = dict(headers or {})
    body: str | None = None
    if json_body is not None:
        body = orjson.dumps(json_body).decode("utf-8")
        outgoing.setdefault("Content-Type", "application/json")
    response = invoke_web_request(url, method=method, headers=outgoing, body=body, timeout=timeout)
    try:
        return orjson.loads(response.text)
    except orjson.JSONDecodeError as exc:
        raise NativeCallError(f"response from {url!r} is not JSON: {exc}") from exc


def download_file(url: str, dest: str | Path, *, chunk_size: int = 65536, timeout: float = 30.0) -> Path:
    """Stream an HTTP download to ``dest``, **memory-bounded** (never buffers the whole body).

    Unlike :func:`invoke_web_request` (which returns the body as data), this copies the
    response to a file in fixed-size chunks via :func:`shutil.copyfileobj`, so a multi-
    gigabyte download stays flat in memory.  A 4xx/5xx or transport failure raises
    :class:`~pwshpy.domain.errors.NativeCallError` (no partial file is left as a "success").

    Example:
        >>> callable(download_file)
        True
    """
    _require_http_scheme(url)
    destination = Path(dest)
    request = urllib.request.Request(url)  # noqa: S310 - scheme restricted to http/https above
    try:
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,  # noqa: S310  # nosec B310 - http/https only
            destination.open("wb") as handle,
        ):
            shutil.copyfileobj(response, handle, chunk_size)
    except urllib.error.HTTPError as exc:
        destination.unlink(missing_ok=True)
        raise NativeCallError(f"download of {url!r} failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise NativeCallError(f"download of {url!r} failed: {exc}") from exc
    return destination


__all__ = ["download_file", "invoke_rest_method", "invoke_web_request"]
