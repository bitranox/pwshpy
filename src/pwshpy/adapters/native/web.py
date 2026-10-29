"""native web requests over the standard library (portable, no extra dependency).

Typed equivalents of ``Invoke-WebRequest`` (returns a :class:`WebResponse`) and
``Invoke-RestMethod`` (returns the parsed JSON).  Built on ``urllib.request`` +
``orjson``; the URL scheme is restricted to http/https.

Contents:
    * :func:`invoke_web_request` - an HTTP request returning a typed WebResponse.
    * :func:`invoke_rest_method` - an HTTP request returning parsed JSON.
"""

from __future__ import annotations

import http.client
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


class _RedirectRefusedError(urllib.error.URLError):
    """A redirect to a scheme other than http/https; mapped like any other transport failure."""


class _HttpOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to http/https.

    urllib's own handler follows a redirect to ``ftp://`` too (and refuses ``file:`` only as a 3xx
    answer, which ``skip_http_error_check`` would return), so the http/https rule
    :func:`_require_http_scheme` applies to the first URL would not bind the URL actually fetched.
    The target is checked before anything connects to it, whatever ``skip_http_error_check`` says.
    """

    def http_error_302(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any) -> Any:
        """Refuse a non-http(s) redirect target, else redirect as urllib does."""
        location = headers.get("location") or headers.get("uri")
        if location is not None:
            target = urllib.parse.urljoin(req.full_url, str(location))
            if urllib.parse.urlsplit(target).scheme.lower() not in ("http", "https"):
                fp.close()
                raise _RedirectRefusedError(f"redirect to {target!r} refused: only http/https URLs are allowed")
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _open(request: urllib.request.Request, timeout: float) -> Any:
    """Open ``request`` with an opener that follows redirects to http/https only.

    Built per call, not once at import: ``build_opener`` reads the proxy environment when it runs.
    """
    opener = urllib.request.build_opener(_HttpOnlyRedirectHandler())
    return opener.open(request, timeout=timeout)


def _is_timeout(exc: BaseException) -> bool:
    """Whether a urllib failure is the socket timeout running out.

    A read timeout arrives as a bare ``TimeoutError`` (``socket.timeout`` is its alias
    since Python 3.10); a connect timeout arrives wrapped, as ``URLError.reason``.
    """
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)


#: What a transport or protocol failure can raise: ``http.client.HTTPException`` (``BadStatusLine`` from
#: a port that does not speak HTTP, ``IncompleteRead`` from a body cut short) is not an ``OSError``.
_TRANSPORT_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)


def _transport_error(what: str, exc: BaseException, timeout: float) -> NativeCallError:
    """Map a transport failure: a timeout to OperationTimeoutError, anything else through native_error."""
    if _is_timeout(exc):
        return OperationTimeoutError(f"{what} timed out after {timeout}s", timeout=timeout)
    return native_error(f"{what} failed: {exc}", exc)


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
    """Return the answer urllib raised as HTTPError, or raise it as the cmdlets do.

    urllib raises ``HTTPError`` for any status it does not follow (a refused 3xx such as a
    redirect loop, 304, 4xx, 5xx): each raises unless ``skip_http_error_check``, which returns
    the answer itself. A redirect to a scheme other than http/https never gets here: it is
    refused before it is followed, whatever ``skip_http_error_check`` says.
    """
    is_error = not skip_http_error_check
    try:
        # A raised error needs only the excerpt, so a huge error page is never read in full.
        raw = exc.read(_EXCERPT_BYTES + 1) if is_error else exc.read()
    except _TRANSPORT_ERRORS as read_exc:  # the error body itself can stall or break off
        raise _transport_error(f"web request to {url!r}", read_exc, timeout) from read_exc
    finally:
        exc.close()
    if is_error:
        excerpt = _body_excerpt(raw)
        # urllib's reason for a refused redirect spans lines; the message stays one line.
        reason = " ".join(str(exc.reason).split())
        raise native_error(f"web request to {url!r} failed: HTTP {exc.code} {reason}{excerpt}", exc) from exc
    return WebResponse(
        url=url,
        status_code=int(exc.code),
        text=raw.decode("utf-8", errors="replace"),
        headers=dict(exc.headers),
    )


def invoke_web_request(  # noqa: PLR0913 - mirrors the WebRequester port: Invoke-WebRequest's independent options
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: str | None = None,
    timeout: float = 30.0,
    skip_http_error_check: bool = False,
) -> WebResponse:
    """Perform an HTTP request and return a typed :class:`WebResponse` (like Invoke-WebRequest).

    Any status urllib does not follow (a refused 3xx, 304, 4xx, 5xx) raises
    :class:`~pwshpy.domain.errors.NativeCallError` naming the status and a short excerpt of the
    body, as ``Invoke-WebRequest`` does; with
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
        with _open(request, timeout) as response:
            raw = response.read()
            return WebResponse(
                url=url,
                status_code=int(response.status),
                text=raw.decode("utf-8", errors="replace"),
                headers=dict(response.headers),
            )
    except urllib.error.HTTPError as exc:
        return _http_error_response(url, exc, skip_http_error_check=skip_http_error_check, timeout=timeout)
    except _TRANSPORT_ERRORS as exc:
        raise _transport_error(f"web request to {url!r}", exc, timeout) from exc


def invoke_rest_method(  # noqa: PLR0913 - mirrors the RestInvoker port: Invoke-RestMethod's independent options
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
    header.  Any status urllib does not follow (a refused 3xx, 304, 4xx, 5xx) raises
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


def _require_complete_body(response: object) -> None:
    """Raise when the body ended before its ``Content-Length`` (http.client's count of bytes still due).

    ``http.client`` raises ``IncompleteRead`` for a short body only on a whole read; the chunked
    reads of ``shutil.copyfileobj`` just see the end of the stream, so a cut-off download would
    otherwise be kept as a complete file.

    Raises:
        ConnectionError: Bytes the server announced never arrived.
    """
    remaining = getattr(response, "length", None)
    if remaining:
        raise ConnectionError(f"the connection closed {remaining} bytes before the announced Content-Length")


def download_file(url: str, dest: str | Path, *, chunk_size: int = 65536, timeout: float = 30.0) -> Path:
    """Stream an HTTP download to ``dest``, **memory-bounded** (never buffers the whole body).

    Unlike :func:`invoke_web_request` (which returns the body as data), this copies the
    response to a file in fixed-size chunks via :func:`shutil.copyfileobj`, so a multi-
    gigabyte download stays flat in memory.  Any status urllib does not follow (a refused 3xx,
    304, 4xx, 5xx) or a transport failure raises
    :class:`~pwshpy.domain.errors.NativeCallError`, a timeout its subclass
    :class:`~pwshpy.domain.errors.OperationTimeoutError` (no partial file is left as a "success").
    Only a file this call opened is removed on failure: one that fails before it opens ``dest``
    (a refused connect, an HTTP error) leaves an existing file there untouched.

    Example:
        >>> callable(download_file)
        True
    """
    _require_http_scheme(url)
    destination = Path(dest)
    request = urllib.request.Request(url)  # noqa: S310 - scheme restricted to http/https above
    opened = False  # set once this call created or truncated ``dest``: only then is it ours to remove
    try:
        with (
            _open(request, timeout) as response,
            destination.open("wb") as handle,
        ):
            opened = True  # both entered in order: a failed connect or HTTP error never reaches open()
            shutil.copyfileobj(response, handle, chunk_size)
            _require_complete_body(response)
    except urllib.error.HTTPError as exc:
        raise native_error(f"download of {url!r} failed: HTTP {exc.code}", exc) from exc
    except _TRANSPORT_ERRORS as exc:
        if opened:
            destination.unlink(missing_ok=True)
        raise _transport_error(f"download of {url!r}", exc, timeout) from exc
    return destination


__all__ = ["download_file", "invoke_rest_method", "invoke_web_request"]
