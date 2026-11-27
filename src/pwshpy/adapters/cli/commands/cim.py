"""``pwshpy get_cim_instance`` / ``invoke_cim_method`` - CIM/WMI reads through the pipeline.

A thin front end over the ``ps`` facade (native / WBEM), mirroring ``Get-CimInstance`` and,
for the read-only methods pwshpy allows, ``Invoke-CimMethod``.  Streams
:class:`~pwshpy.domain.records.CimInstance` / :class:`~pwshpy.domain.records.CimMethodResult`
records lazily (forward-only), so querying an unbounded class and piping to ``head`` stays
cheap.  Both are open key/value bags - view them with ``--jsonl``.

Contents:
    * :func:`cli_cim` - the ``get_cim_instance`` subcommand.
    * :func:`cli_invoke_cim_method` - the ``invoke_cim_method`` subcommand.
"""

from __future__ import annotations

import logging

import rich_click as click

from ..constants import CLICK_CONTEXT_SETTINGS
from ..context import get_cli_context
from ..output import emit
from ..typed_click import argument, option
from ._common import bind_command, resolve_format

logger = logging.getLogger(__name__)


@click.command("get_cim_instance", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("class_name")
@option("--where", "where", default=None, help='Read-only WQL filter fragment, e.g. "DriveType = 3".')
@option("--namespace", "namespace", default="root/cimv2", help="WMI namespace (default: root/cimv2).")
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_cim(
    ctx: click.Context, class_name: str, where: str | None, namespace: str, as_json: bool, as_jsonl: bool
) -> None:
    """Query CIM/WMI class CLASS_NAME as typed records (native; like Get-CimInstance).

    CLASS_NAME is a WMI class, e.g. ``Win32_OperatingSystem``, ``Win32_LogicalDisk``.
    Streams lazily, so even an unbounded class (``CIM_DataFile``) stays cheap with
    ``--jsonl | head``.

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_cim, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    ps = get_cli_context(ctx).services.ps
    with bind_command(ctx):
        logger.info("Querying CIM/WMI")
        emit(ps.get_cim_instance(class_name, where=where, namespace=namespace), fmt)


@click.command("invoke_cim_method", context_settings=CLICK_CONTEXT_SETTINGS)
@argument("class_name")
@argument("method_name")
@option("--where", "where", default=None, help="Read-only WQL filter fragment selecting the instances.")
@option("--namespace", "namespace", default="root/cimv2", help="WMI namespace (default: root/cimv2).")
@option(
    "--arg",
    "-a",
    "args",
    multiple=True,
    help="An in-parameter as NAME=VALUE (repeatable); the value is typed from the method's schema.",
)
@option("--json", "as_json", is_flag=True, default=False, help="Emit records as a JSON array.")
@option("--jsonl", "as_jsonl", is_flag=True, default=False, help="Stream one JSON record per line.")
@click.pass_context
def cli_invoke_cim_method(
    ctx: click.Context,
    class_name: str,
    method_name: str,
    where: str | None,
    namespace: str,
    args: tuple[str, ...],
    as_json: bool,
    as_jsonl: bool,
) -> None:
    """Call read-only CIM/WMI method METHOD_NAME of CLASS_NAME on each instance (native; like Invoke-CimMethod).

    Only methods on pwshpy's read-only allowlist run (BitLocker's GetConversionStatus,
    Win32_Tpm's IsReady, ...); any other is refused before WMI is contacted. A non-zero
    return_value in a record is the method's own answer for that instance, not a failure.

    \f
    Example:
        >>> from click.testing import CliRunner
        >>> CliRunner().invoke(cli_invoke_cim_method, ["--help"]).exit_code
        0
    """
    fmt = resolve_format(as_json, as_jsonl)
    arguments = _arguments(args)
    ps = get_cli_context(ctx).services.ps
    with bind_command(ctx):
        logger.info("Invoking a read-only CIM/WMI method")
        emit(
            ps.invoke_cim_method(class_name, method_name, where=where, namespace=namespace, arguments=arguments),
            fmt,
        )


def _arguments(pairs: tuple[str, ...]) -> dict[str, str]:
    """``--arg NAME=VALUE`` pairs as a dict (last wins); a malformed pair is refused WITHOUT echoing it.

    A value can identify a secret (a key protector ID), so unlike other KEY=VALUE options the
    refusal does not repeat what was given.

    Example:
        >>> _arguments(("KeyProtectorType=3",))
        {'KeyProtectorType': '3'}
    """
    arguments: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator or not name:
            raise click.BadParameter("expected NAME=VALUE (the given text is not repeated)", param_hint="--arg")
        arguments[name] = value
    return arguments


__all__ = ["cli_cim", "cli_invoke_cim_method"]
