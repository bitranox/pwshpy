""".NET RunspacePool mode: end-to-end in a fresh interpreter.

The host is a per-process singleton opened once, so pool mode (opt-in via
``PWSHPY_RUNSPACE_POOL_SIZE``) can only be exercised in a process that has not already
opened a single runspace.  Each test therefore runs a tiny driver in a subprocess with
the env var set, and asserts on its output.  ``local_only`` - needs the ``[full]`` extra
plus the .NET 10 / PowerShell 7.6 host.

Guarded on ``is_runtime_available()`` rather than ``is_available()``: the latter reports
only that the extra is importable, so it stays True on a box with pythonnet and no .NET
runtime and the driver subprocess then fails instead of skipping. The guard initializing
the host in THIS process is harmless here - every test spawns its own subprocess precisely
because the host is a per-process singleton.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pwshpy.adapters.powershell import is_runtime_available

pytestmark = [
    pytest.mark.local_only,
    pytest.mark.skipif(
        not is_runtime_available(),
        reason=".NET needs the [full] extra AND the .NET 10 runtime AND the PowerShell 7.6 SDK",
    ),
]

_CONCURRENT_DRIVER = """
import threading
from pwshpy import ps
from pwshpy.adapters.powershell import hosted

# Force host init, then confirm we are genuinely in pool mode.
ps.cmdlet("Write-Output", InputObject=0)
assert hosted._pool is not None, "expected a RunspacePool in pool mode"

results = []
lock = threading.Lock()


def work(n):
    out = ps.cmdlet("Write-Output", InputObject=n).output
    with lock:
        results.append(out[0])


threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

print(sorted(results))
"""


def _run_driver(source: str, pool_size: str) -> str:
    """Run a driver script in a subprocess with a pool size set; return its stdout."""
    env = {**os.environ, "PWSHPY_RUNSPACE_POOL_SIZE": pool_size}
    completed = subprocess.run(  # noqa: S603 - fixed argv (this interpreter + a literal driver), no shell
        [sys.executable, "-c", source],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, f"driver failed:\nSTDOUT:{completed.stdout}\nSTDERR:{completed.stderr}"
    return completed.stdout.strip()


def test_pool_mode_runs_concurrent_cmdlets() -> None:
    """A pool of 4 fulfils 8 concurrent cmdlet invocations, all returning correctly."""
    out = _run_driver(_CONCURRENT_DRIVER, pool_size="4")
    assert out == "[0, 1, 2, 3, 4, 5, 6, 7]"
