"""Test doubles for the packed runners' elevation path, shared by the ``.sh`` and ``.ps1`` e2e tests.

A refused sudo password cannot be typed without a TTY, so a ``sudo`` double on ``PATH`` stands in
for it. Imported by bare module name (``import elevation_doubles``): ``tests/`` has no
``__init__.py``, so pytest puts this directory on ``sys.path`` for the test files beside it.

Contents:
    * :data:`RUNNING_AS_ROOT` - whether this process is root (the runners then never elevate).
    * :func:`write_fake_sudo` - write a ``sudo`` double into a directory.
    * :func:`link_tools` - a ``PATH`` directory holding only the named tools.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: The runners only elevate for a non-root uid, so as root no test reaches the sudo branch.
#: Guarded by ``sys.platform`` because ``os.geteuid`` does not exist on Windows.
RUNNING_AS_ROOT: bool = sys.platform != "win32" and os.geteuid() == 0


def write_fake_sudo(bin_dir: Path, *, grant: bool, calls: Path | None = None) -> Path:
    """Write a ``sudo`` double: ``-v`` grants or refuses, anything else runs as the same user.

    Args:
        bin_dir: Directory to write the double into (put it first on ``PATH``).
        grant: Whether ``sudo -v`` succeeds (exit 0) or refuses (exit 1).
        calls: When given, every invocation appends its arguments as one line to this file,
            so a test can prove sudo was never called.

    Returns:
        The path of the written double.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     write_fake_sudo(Path(d), grant=False).name
        'sudo'
    """
    record = f'printf \'%s\\n\' "$*" >> "{calls}"\n' if calls is not None else ""
    script = bin_dir / "sudo"
    script.write_text(
        "#!/bin/sh\n" + record + f'if [ "$1" = "-v" ]; then exit {0 if grant else 1}; fi\n' + 'exec "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)
    return script


def link_tools(bin_dir: Path, names: tuple[str, ...]) -> Path:
    """Fill ``bin_dir`` with symlinks to the named tools found on ``PATH``; missing ones are skipped.

    Used as the WHOLE ``PATH`` to hide a tool (``sudo``) that lives in the same system directory as
    the tools a runner still needs.

    Args:
        bin_dir: Directory to create and fill.
        names: Tool names to link.

    Returns:
        ``bin_dir``.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     link_tools(Path(d) / "bin", ()).name
        'bin'
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        found = shutil.which(name)
        if found is not None:
            (bin_dir / name).symlink_to(found)
    return bin_dir


__all__ = ["RUNNING_AS_ROOT", "link_tools", "write_fake_sudo"]
