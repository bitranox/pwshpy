"""Display configuration: the human view from lib_layered_config, the JSON through record_json.

Drains pending log output before displaying, so no log line lands inside the configuration.

Contents:
    * :func:`display_config` - the ``DisplayConfig`` port: print the merged configuration.
"""

from __future__ import annotations

from typing import Final

from lib_layered_config import Config
from lib_layered_config import OutputFormat as LibOutputFormat
from lib_layered_config import display_config as _lib_display
from rich.console import Console

from pwshpy.domain.enums import OutputFormat

from ..cli.output import emit_json
from ..logging.setup import drain_log_queue

#: What ``Config.get`` returns for a missing section, distinct from a section whose value is None.
_ABSENT: Final = object()


def display_config(
    config: Config,
    *,
    output_format: OutputFormat = OutputFormat.HUMAN,
    section: str | None = None,
    console: Console | None = None,
    profile: str | None = None,
) -> None:
    """Display configuration: lib_layered_config's Rich view, or JSON written by ``emit_json``.

    Drains any pending log output before displaying to prevent mixing log messages with
    configuration output (a drain that times out or cannot run is skipped).

    Args:
        config: Already-loaded layered configuration object to display.
        output_format: Output format: OutputFormat.HUMAN for TOML-like display or
            OutputFormat.JSON for JSON. Defaults to OutputFormat.HUMAN.
        section: Optional section name to display only that section. When None,
            displays all configuration.
        console: Optional Rich Console for the human view. When None, uses the module-level
            default. Primarily useful for testing. The JSON goes to stdout.
        profile: Optional profile name to include in provenance comments.

    Side Effects:
        Drains pending log messages before display.
        Writes formatted configuration to stdout.

    Raises:
        ValueError: If a section was requested that doesn't exist.
    """
    drain_log_queue()
    if output_format is OutputFormat.JSON:
        _display_json(config, section)
        return
    lib_format = LibOutputFormat(output_format.value)
    _lib_display(config, output_format=lib_format, section=section, profile=profile, console=console)


def _display_json(config: Config, section: str | None) -> None:
    """Print the redacted configuration (or one section of it) as two-space indented JSON.

    Written through :func:`~pwshpy.adapters.cli.output.emit_json`, the serialiser behind every other
    JSON output, rather than lib_layered_config's own: a value holding a byte that is not valid
    UTF-8 (a POSIX ``--set`` argument or environment variable, decoded with ``surrogateescape``) made
    that one raise ``TypeError``, where this writes U+FFFD for each such byte and one
    ``warning: output field <path>`` line on stderr. Clean output is what the library printed: the
    same redacted data, the same indentation, ``{section: value}`` for ``section``.

    Args:
        config: The merged configuration.
        section: A dotted section to show alone, or None for everything.

    Raises:
        ValueError: ``section`` names nothing in the configuration.
    """
    if section:
        if config.get(section, default=_ABSENT) is _ABSENT:
            raise ValueError(f"Section '{section}' not found")
        data: object = {section: config.get_redacted(section)}
    else:
        data = config.as_dict(redact=True)
    emit_json(data, indent=True)


__all__ = ["display_config"]
