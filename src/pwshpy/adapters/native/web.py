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

from ...domain.errors import NativeCallError, OperationTimeoutError
from ...domain.records import WebResponse
from .marshal import native_error


def _require_http_scheme(url: str) -> None:
    """Reject any non-http(s) URL up front (blocks file://, ftp://, etc.)."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise NativeCallError(f"unsupported URL scheme {scheme!r}; only http/https are allowed")


def _is_timeout(exc: BaseException) -> bool:
    """Whether a urllib failure is the socket timeout running out.

    A read timeout arrives as a bare ``TimeoutError`` (``socket.timeout`` is its alias
    since Python 3.10); a connect timeout arrives wrapped, as ``URLError.reason``.
    """
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)


def _transport_error(what: str, exc: BaseException, timeout: float) -> NativeCallError:
    """Map a transport failure: a timeout to OperationTimeoutError, anything else through native_error."""
    if _is_timeout(exc):
        return OperationTimeoutError(f"{what} timed out after {timeout}s", timeout=timeout)
    return native_error(f"{what} failed: {exc}", exc)


#: The lowest status the cmdlets treat as an error (Invoke-WebRequest raises on 4xx and 5xx).
_HTTP_ERROR_FLOOR = 400
#: How much of an error body is read for the message excerpt; the rest is never read.
_EXCERPT_BYTES = 1024
#: How many characters of that body the error message carries at most.
_EXCERPT_CHARS = 200


def _body_excerpt(raw: bytes) -> str:
    """A short, single-line, printable start of an error body, or "" for an empty body.

    The message is read on a console and in logs, so line breaks and control characters
    (an escape sequence included) become spaces, and the length is bounded: an error page
    can be megabytes and can echo request data the caller would not want repeated in full.
    """
    text = raw.decode("utf-8", errors="replace")
    one_line = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    if not one_line:
        return ""
    truncated = len(one_line) > _EXCERPT_CHARS or len(raw) > _EXCERPT_BYTES
    return f": {one_line[:_EXCERPT_CHARS]}{'...' if truncated else ''}"


def _http_error_response(
    url: str, exc: urllib.error.HTTPError, *, skip_http_error_check: bool, timeout: float
) -> WebResponse:
    """Return the answer urllib raised as HTTPError, or raise it as the cmdlets do for >= 400."""
    is_error = exc.code >= _HTTP_ERROR_FLOOR and not skip_http_error_check
    try:
        # A raised error needs only the excerpt, so a huge error page is never read in full.
        raw = exc.read(_EXCERPT_BYTES + 1) if is_error else exc.read()
    except OSError as read_exc:  # the error body itself can stall or break off
        raise _transport_error(f"web request to {url!r}", read_exc, timeout) from read_exc
    finally:
        exc.close()
    if is_error:
        excerpt = _body_excerpt(raw)
        raise native_error(f"web request to {url!r} failed: HTTP {exc.code} {exc.reason}{excerpt}", exc) from exc
    return WebResponse(
        url=url,
        status_code=int(exc.code),
        text=raw.decode("utf-8", errors="replace"),
        headers=dict(exc.headers),
    )


def invoke_web_request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: str | None = None,
    timeout: float = 30.0,
    skip_http_error_check: bool = False,
) -> WebResponse:
    """Perform an HTTP request and return a typed :class:`WebResponse` (like Invoke-WebRequest).

    A status of 400 or above raises :class:`~pwshpy.domain.errors.NativeCallError` naming the
    status and a short excerpt of the body, as ``Invoke-WebRequest`` does; with
    ``skip_http_error_check=True`` (pwsh's ``-SkipHttpErrorCheck``) the answer is returned
    with its status and full body instead. A transport failure raises ``NativeCallError``, a
    timeout its subclass :class:`~pwshpy.domain.errors.OperationTimeoutError`.

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
        return _http_error_response(url, exc, skip_http_error_check=skip_http_error_check, timeout=timeout)
    except (urllib.error.URLError, OSError) as exc:
        raise _transport_error(f"web request to {url!r}", exc, timeout) from exc


def invoke_rest_method(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    timeout: float = 30.0,
    skip_http_error_check: bool = False,
) -> Any:
    """Perform an HTTP request and return the parsed JSON body (like Invoke-RestMethod).

    ``json_body`` is serialized and sent with a ``Content-Type: application/json``
    header.  A status of 400 or above raises
    :class:`~pwshpy.domain.errors.NativeCallError` unless ``skip_http_error_check`` is
    True, in which case the error body is parsed like any other.  Raises
    ``NativeCallError`` too if the response body is not valid JSON.

    Example:
        >>> callable(invoke_rest_method)
        True
    """
    outgoing = dict(headers or {})
    body: str | None = None
    if json_body is not None:
        body = orjson.dumps(json_body).decode("utf-8")
        outgoing.setdefault("Content-Type", "application/json")
    response = invoke_web_request(
        url,
        method=method,
        headers=outgoing,
        body=body,
        timeout=timeout,
        skip_http_error_check=skip_http_error_check,
    )
    try:
        return orjson.loads(response.text)
    except orjson.JSONDecodeError as exc:
        raise native_error(f"response from {url!r} is not JSON: {exc}", exc) from exc


def download_file(url: str, dest: str | Path, *, chunk_size: int = 65536, timeout: float = 30.0) -> Path:
    """Stream an HTTP download to ``dest``, **memory-bounded** (never buffers the whole body).

    Unlike :func:`invoke_web_request` (which returns the body as data), this copies the
    response to a file in fixed-size chunks via :func:`shutil.copyfileobj`, so a multi-
    gigabyte download stays flat in memory.  A 4xx/5xx or transport failure raises
    :class:`~pwshpy.domain.errors.NativeCallError`, a timeout its subclass
    :class:`~pwshpy.domain.errors.OperationTimeoutError` (no partial file is left as a "success").

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
        raise native_error(f"download of {url!r} failed: HTTP {exc.code}", exc) from exc
    except (urllib.error.URLError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise _transport_error(f"download of {url!r}", exc, timeout) from exc
    return destination


__all__ = ["download_file", "invoke_rest_method", "invoke_web_request"]
