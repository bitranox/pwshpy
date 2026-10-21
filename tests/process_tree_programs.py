"""Child programs for the ``wait_tree`` tests: a parent that starts a grandchild, then exits.

The grandchild leaves evidence on disk instead of being looked up by pid: ``<base>.beat``
grows by one byte every 50 ms while it runs (a monotone counter, so a stopped growth
proves it is gone) and ``<base>.done`` appears only when it finished its whole sleep.
The parent starts it in one of four shapes, chosen by the first argument:

* ``background`` - an ordinary child with its stdio on the null device; stays in the
  parent's process group and session.
* ``daemon`` - leaves the tree the way a daemon does: double fork plus ``setsid`` on
  POSIX, ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` on Windows.
* ``inherit`` - an ordinary child that keeps the parent's stdout and prints one line
  when it finishes.

The parent prints ``parent done`` and exits with ``$PARENT_EXIT`` (default 0).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

GRANDCHILD = """
import sys, time
base, seconds = sys.argv[1], float(sys.argv[2])
end = time.monotonic() + seconds
with open(base + '.beat', 'a') as beat:
    while time.monotonic() < end:
        beat.write('.')
        beat.flush()
        time.sleep(0.05)
with open(base + '.done', 'w') as done:
    done.write('done')
if len(sys.argv) > 3:
    print('grandchild out', flush=True)
"""

PARENT = f"""
import os, subprocess, sys
mode, base, seconds = sys.argv[1], sys.argv[2], sys.argv[3]
argv = [sys.executable, '-c', {GRANDCHILD!r}, base, seconds]
null = subprocess.DEVNULL
if mode == 'daemon' and os.name == 'posix':
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            os.execv(sys.executable, argv)
        os._exit(0)
    os.wait()
elif mode == 'daemon':
    subprocess.Popen(argv, creationflags=0x00000008 | 0x00000200, stdin=null, stdout=null, stderr=null)
elif mode == 'inherit':
    subprocess.Popen(argv + ['say'], close_fds=False)
else:
    subprocess.Popen(argv, stdin=null, stdout=null, stderr=null)
print('parent done', flush=True)
sys.exit(int(os.environ.get('PARENT_EXIT', '0')))
"""


def parent_argv(mode: str, base: Path, seconds: float) -> list[str]:
    """Return the argv that runs :data:`PARENT` in ``mode`` with evidence files under ``base``.

    Example:
        >>> parent_argv("background", Path("x"), 1.0)[3:]
        ['background', 'x', '1.0']
    """
    return [sys.executable, "-c", PARENT, mode, str(base), str(seconds)]


def beats(base: Path) -> int:
    """Return how many heartbeats the grandchild has written so far (0 before it started)."""
    try:
        return Path(f"{base}.beat").stat().st_size
    except FileNotFoundError:
        return 0


def finished(base: Path) -> bool:
    """Return ``True`` once the grandchild completed its whole sleep."""
    return Path(f"{base}.done").exists()


def wait_for_beats(base: Path, *, deadline_s: float = 20.0) -> None:
    """Block until the grandchild has written its first heartbeat (it is running)."""
    end = time.monotonic() + deadline_s
    while beats(base) == 0:
        if time.monotonic() > end:
            raise AssertionError(f"grandchild under {base} never started")
        time.sleep(0.05)


def is_stopped(base: Path, *, quiet_s: float = 1.0) -> bool:
    """Return ``True`` when the heartbeat does not grow over ``quiet_s`` (the grandchild is gone).

    20 beats fit into one quiet second of a live grandchild, so no growth is not a slow tick.
    """
    before = beats(base)
    time.sleep(quiet_s)
    return beats(base) == before
