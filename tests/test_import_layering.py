"""``import pwshpy`` stays light: the library never loads the CLI's command modules.

The configuration adapter once imported the CLI's JSON writer, so ``import pwshpy`` (through the
composition's ``display_config``) loaded ``adapters.cli`` and every command module, and closed an
import cycle ``adapters.config -> display -> adapters.cli -> commands/config -> adapters.config``
while ``adapters.config`` was only half initialised. Checked in a fresh interpreter, because this
test process has long since imported the CLI.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_PROBE = "import sys, pwshpy; print(sorted(m for m in sys.modules if m.startswith('pwshpy.adapters.cli')))"


@pytest.mark.os_agnostic
def test_import_pwshpy_loads_no_cli_module() -> None:
    """A library import loads nothing of ``pwshpy.adapters.cli``, its command modules least of all."""
    result = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a literal probe
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
