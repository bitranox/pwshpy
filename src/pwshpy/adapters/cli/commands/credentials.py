"""``pwshpy`` credential commands - save/load/delete OS-vault credentials (native).

``save_credential`` reads the secret from a **non-echoing prompt** (never an argv flag,
so it stays out of shell history / ``ps``).  ``load_credential`` prints the record with the
secret **masked**.  Backed by the OS credential vault (Windows Credential Manager).

Contents:
    * the ``cli_*`` credential commands, collected in ``CREDENTIAL_COMMANDS``.
"""

from __future__ import annotations

import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit
from ..typed_click import argument, option
from ._common import resolve_format


@click.command("save_credential", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("target")
@argument("username")
@click.pass_context
def cli_save_credential(ctx: click.Context, target: str, username: str) -> None:
    """Store a credential in the OS vault; the secret is read from a hidden prompt (mutating).

    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_save_credential, ["--help"]).exit_code
        0
    """
    secret = click.prompt("Secret", hide_input=True, confirmation_prompt=True)
    get_cli_context(ctx).services.ps.save_credential(target, username, secret)
    click.echo(f"saved credential for {target}")


@click.command("load_credential", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("target")
@option("--json", "as_json", is_flag=True, default=False, help="Emit the record as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Emit the record as one JSON line.")
@click.pass_context
def cli_load_credential(ctx: click.Context, target: str, as_json: bool, as_jsonl: bool) -> None:
    """Load a stored credential (the secret is shown masked); fails if none is stored."""
    credential = get_cli_context(ctx).services.ps.load_credential(target)
    if credential is None:
        raise click.ClickException(f"no credential stored for {target!r}")
    emit([credential], resolve_format(as_json, as_jsonl))


@click.command("delete_credential", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("target")
@click.pass_context
def cli_delete_credential(ctx: click.Context, target: str) -> None:
    """Remove a stored credential from the OS vault (mutating)."""
    get_cli_context(ctx).services.ps.delete_credential(target)
    click.echo(f"deleted credential for {target}")


CREDENTIAL_COMMANDS = (
    cli_save_credential,
    cli_load_credential,
    cli_delete_credential,
)

__all__ = ["CREDENTIAL_COMMANDS"]
