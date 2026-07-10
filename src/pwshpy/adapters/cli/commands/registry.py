"""``pwshpy get_item_property`` / ``registry_keys`` - read the Windows registry as records.

Thin front ends over the ``ps`` facade (native / ``lib_registry``): ``get_item_property KEY``
mirrors ``Get-ItemProperty`` (the values under a key) and ``registry_keys KEY`` mirrors
``Get-ChildItem`` on a registry path (the immediate subkeys).  Both stream typed records; a
native failure surfaces as a clean CLI error via the exit-code machinery.

Contents:
    * :func:`cli_get_item_property` - the ``get_item_property`` command (registry values).
    * :func:`cli_registry_keys` - the ``registry_keys`` command (registry subkeys).
"""

from __future__ import annotations

import logging

import lib_log_rich.runtime
import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit
from ..typed_click import argument, option
from ._common import resolve_format

logger = logging.getLogger(__name__)


@click.command("get_item_property", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("key")
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_get_item_property(ctx: click.Context, key: str, as_json: bool, as_jsonl: bool) -> None:
    """List the values under registry KEY as typed records (like Get-ItemProperty).

    KEY is a hive-prefixed path, e.g. ``HKLM/SOFTWARE/Microsoft/Windows NT/CurrentVersion``.

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_get_item_property, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-get-item-property", extra={"command": "get_item_property"}):
        logger.info("Reading registry values")
        emit(ps.get_item_property(key), fmt)


@click.command("registry_keys", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("key")
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_registry_keys(ctx: click.Context, key: str, as_json: bool, as_jsonl: bool) -> None:
    """List the immediate subkeys of registry KEY as typed records (like Get-ChildItem).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_registry_keys, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with lib_log_rich.runtime.bind(job_id="cli-registry-keys", extra={"command": "registry_keys"}):
        logger.info("Reading registry subkeys")
        emit(ps.registry_keys(key), fmt)


__all__ = ["cli_get_item_property", "cli_registry_keys"]
