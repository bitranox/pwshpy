r"""``pwshpy`` known-folder and shortcut commands.

``get_known_folder`` prints a well-known folder the way pwsh's ``[Environment]::GetFolderPath``
reports it on the same OS (portable); ``get_shortcut`` reads a Windows shortcut (``.lnk``) and
``new_shortcut`` writes one (Windows only, mutating).  Thin front ends over the ``ps`` facade.

``new_shortcut`` takes the target's arguments as an argv after ``--`` (like ``exec``): each word
stays one argument, and Windows stores them as one command line joined the way
``subprocess.list2cmdline`` quotes it: ``pwshpy new_shortcut t.lnk --target C:\uv.exe --
--exclude-newer "7 days" lsdsk``.

Text output (the folder path, the ``new_shortcut`` status line) goes through
:func:`~pwshpy.adapters.cli.output.emit_lines` / :func:`~pwshpy.adapters.cli.output.emit_text`,
so a path outside the console code page prints as UTF-8 when piped.

Contents:
    * :func:`cli_get_known_folder`, :func:`cli_get_shortcut`, :func:`cli_new_shortcut`,
      exported as ``SHORTCUT_COMMANDS``.
"""

from __future__ import annotations

import rich_click as click

from ....domain.enums import KnownFolder
from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import RecordFormat, emit, emit_json, emit_lines, emit_text
from ..typed_click import argument, option
from ._common import enum_by_value, resolve_format


@click.command("get_known_folder", context_settings=CLICK_CONTEXT_SETTINGS)
@argument(
    "folder",
    # The values (the .NET SpecialFolder names), not the members: click matches an Enum choice
    # on the member NAME, which would refuse the documented spelling.
    type=click.Choice(sorted(member.value for member in KnownFolder), case_sensitive=False),
)
@option("--json", "as_json", is_flag=True, default=False, help="Emit the path as a JSON string.")
@click.pass_context
def cli_get_known_folder(ctx: click.Context, folder: str, as_json: bool) -> None:
    """Print a well-known folder's path (native, portable; like [Environment]::GetFolderPath).

    FOLDER is a .NET SpecialFolder name, in any case: ApplicationData, CommonApplicationData,
    Desktop, LocalApplicationData, MyDocuments or Programs (Windows only).  Exit 1 with a message
    on stderr when the folder is missing or not readable (where pwsh returns an empty string).

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_get_known_folder, ["--help"]).exit_code
        0
    """
    # The library's string arm is case-sensitive; the CLI promises any case, so it maps here.
    member = enum_by_value(KnownFolder, folder)
    found = get_cli_context(ctx).services.ps.get_known_folder(member)
    if found is None:
        emit_text(f"{member.value}: no such folder on this machine (missing or not readable)\n", err=True)
        ctx.exit(1)
    elif as_json:
        emit_json(str(found))
    else:
        emit_lines([f"{found}\n"])


@click.command("get_shortcut", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@option("--json", "as_json", is_flag=True, default=False, help="Emit the record as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Emit the record as one JSON line.")
@click.pass_context
def cli_get_shortcut(ctx: click.Context, path: str, as_json: bool, as_jsonl: bool) -> None:
    """Show a Windows shortcut (.lnk) as Windows reads it (native, Windows).

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_get_shortcut, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    emit([get_cli_context(ctx).services.ps.get_shortcut(path)], fmt)


@click.command("new_shortcut", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("path")
@argument("arguments", nargs=-1)
@option(
    "--target",
    required=True,
    help=r"What the shortcut runs or opens: an absolute Windows path, or one starting %NAME%\.",
)
@option("--working-directory", default=None, help="The directory the target starts in.")
@option("--icon", default=None, help="The file the icon is taken from.")
@option("--icon-index", type=int, default=0, show_default=True, help="Which icon in --icon (negative: a resource id).")
@option("--description", default=None, help="The tooltip Explorer shows.")
@option(
    "--run-as-administrator",
    is_flag=True,
    default=False,
    help="Start the target elevated when the shortcut is opened (one UAC prompt).",
)
@option("--force", "-f", is_flag=True, default=False, help="Overwrite an existing file at PATH.")
@option("--json", "as_json", is_flag=True, default=False, help="Emit the stored record as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Emit the stored record as one JSON line.")
@click.pass_context
def cli_new_shortcut(
    ctx: click.Context,
    *,
    path: str,
    arguments: tuple[str, ...],
    target: str,
    working_directory: str | None,
    icon: str | None,
    icon_index: int,
    description: str | None,
    run_as_administrator: bool,
    force: bool,
    as_json: bool,
    as_jsonl: bool,
) -> None:
    """Write a Windows shortcut (.lnk) at PATH; put the target's arguments after -- (native, Windows, mutating).

    Prints one status line naming what Windows stored, or with --json/--jsonl the stored record
    instead.  An existing PATH is refused unless --force is given.

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_new_shortcut, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)  # before writing: a usage error must not leave a file
    info = get_cli_context(ctx).services.ps.new_shortcut(
        path,
        target=target,
        arguments=arguments,
        working_directory=working_directory,
        icon=icon,
        icon_index=icon_index,
        description=description,
        run_as_administrator=run_as_administrator,
        force=force,
    )
    if fmt is RecordFormat.HUMAN:
        emit_text(f"created shortcut {info.path} -> {info.target}\n")
    else:
        emit([info], fmt)


SHORTCUT_COMMANDS = (cli_get_known_folder, cli_get_shortcut, cli_new_shortcut)

__all__ = ["SHORTCUT_COMMANDS"]
