"""Console script entry point with production wiring.

This module provides the entry point for console scripts (pip-installed commands).
It wires production services from the composition layer before invoking the CLI.

System Role:
    Sits at package level (outside adapters) to properly wire composition into
    the adapters layer without violating clean architecture layer constraints.
"""

from __future__ import annotations

from .adapters.cli.main import main as cli_main
from .adapters.cli.output import use_utf8_when_piped
from .composition import build_production


def main() -> int:
    """Console script and ``python -m pwshpy`` entry point with production services wired.

    A piped stdout and stderr are switched to UTF-8 here, at the start of the process, rather than
    in :func:`pwshpy.adapters.cli.main.main`: that one is also called in-process, where the streams
    belong to the caller.

    Returns:
        Exit code from CLI execution.
    """
    use_utf8_when_piped()
    return cli_main(services_factory=build_production)


__all__ = ["main"]
