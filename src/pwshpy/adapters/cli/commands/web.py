"""``pwshpy`` web commands - HTTP requests with typed / JSON output (native, portable).

``invoke_web_request`` prints the response body (or the whole ``WebResponse`` with
``--json``); ``invoke_rest_method`` sends/receives JSON.  A curl with typed output.

Contents:
    * the ``cli_*`` web commands, collected in ``WEB_COMMANDS``.
"""

from __future__ import annotations

import orjson
import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit, emit_json
from ..typed_click import argument, option
from ._common import parse_pairs, resolve_format


def _parse_headers(pairs: tuple[str, ...]) -> dict[str, str]:
    """Parse curl-style ``('Accept: application/json',)`` headers into a dict."""
    return parse_pairs(pairs, ":", "'Name: value'", strip=True)


@click.command("invoke_web_request", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("url")
@option("--method", "-X", default="GET", help="HTTP method (default GET).")
@option("--header", "-H", "headers", multiple=True, help="A request header as 'Name: value' (repeatable).")
@option("--body", default=None, help="Request body (string).")
@option("--timeout", type=float, default=30.0, help="Seconds before the request is abandoned.")
@option("--json", "as_json", is_flag=True, default=False, help="Emit the full WebResponse record as JSON.")
@click.pass_context
def cli_invoke_web_request(
    ctx: click.Context,
    url: str,
    method: str,
    headers: tuple[str, ...],
    body: str | None,
    timeout: float,
    as_json: bool,
) -> None:
    """Perform an HTTP request; print the body, or the full record with --json (like Invoke-WebRequest).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_invoke_web_request, ["--help"]).exit_code
        0
    """
    ps = get_cli_context(ctx).services.ps
    response = ps.invoke_web_request(url, method=method, headers=_parse_headers(headers), body=body, timeout=timeout)
    if as_json:
        emit([response], resolve_format(as_json=True, as_jsonl=False))
    else:
        click.echo(response.text, nl=False)


@click.command("invoke_rest_method", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("url")
@option("--method", "-X", default="GET", help="HTTP method (default GET).")
@option("--header", "-H", "headers", multiple=True, help="A request header as 'Name: value' (repeatable).")
@option("--body-json", default=None, help="Request body as a JSON string (sent as application/json).")
@option("--timeout", type=float, default=30.0, help="Seconds before the request is abandoned.")
@click.pass_context
def cli_invoke_rest_method(
    ctx: click.Context,
    url: str,
    method: str,
    headers: tuple[str, ...],
    body_json: str | None,
    timeout: float,
) -> None:
    """Perform an HTTP request and print the parsed JSON response (like Invoke-RestMethod)."""
    ps = get_cli_context(ctx).services.ps
    parsed_body = orjson.loads(body_json) if body_json is not None else None
    result = ps.invoke_rest_method(
        url, method=method, headers=_parse_headers(headers), json_body=parsed_body, timeout=timeout
    )
    emit_json(result)


@click.command("download_file", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("url")
@argument("dest")
@option("--timeout", type=float, default=30.0, help="Seconds before the download is abandoned.")
@click.pass_context
def cli_download_file(ctx: click.Context, url: str, dest: str, timeout: float) -> None:
    """Stream a URL to DEST, memory-bounded (never buffers the whole body; unlike Invoke-WebRequest)."""
    written = get_cli_context(ctx).services.ps.download_file(url, dest, timeout=timeout)
    click.echo(f"downloaded {url} -> {written}")


WEB_COMMANDS = (
    cli_invoke_web_request,
    cli_invoke_rest_method,
    cli_download_file,
)

__all__ = ["WEB_COMMANDS"]
