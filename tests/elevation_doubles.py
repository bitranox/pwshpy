"""Test doubles for the packed runners' elevation path, shared by the ``.sh`` and ``.ps1`` e2e tests.

A refused sudo password cannot be typed without a TTY, so a ``sudo`` double on ``PATH`` stands in
for it. Imported by bare module name (``import elevation_doubles``): ``tests/`` has no
``__init__.py``, so pytest puts this directory on ``sys.path`` for the test files beside it.

Contents:
    * :data:`RUNNING_AS_ROOT` - whether this process is root (the runners then never elevate).
    * :func:`write_fake_sudo` - write a ``sudo`` double into a directory.
    * :func:`link_tools` - a ``PATH`` directory holding only the named tools.
    * :data:`MANAGED_PYTHON_VARIABLES` - the variables that steer where uv's managed Pythons come from.
    * :data:`INTERPRETER_VARIABLES` - the variables an elevated run drops before uv picks a Python.
    * :func:`write_planted_python` - ``python`` / ``python3`` doubles that record being run.
    * :func:`write_planted_environment` - a virtual or conda environment whose python is such a double.
    * :func:`write_planted_venv` - a real virtual environment whose python records every start.
    * :func:`write_local_wheel` - a dependency-free wheel uv installs without the network.
    * :func:`write_recording_uv` - a ``uv`` double that records its argv and the variables it got.
    * :func:`managed_python_dir` - the host's uv-managed Python directory, when one holds an install.
    * :func:`interpreter_probe_env` - a uv search confined to one test (fresh cache, no downloads).
    * :func:`managed_python_env` - name uv's managed-Python directory so an elevated run finds it too.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import sysconfig
import zipfile
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


#: The variables that decide where uv looks for, or fetches, the Pythons it manages: another install
#: directory, another config file, another download mirror or download list ([59]).
MANAGED_PYTHON_VARIABLES = (
    "UV_PYTHON_INSTALL_DIR",
    "UV_CONFIG_FILE",
    "UV_PYTHON_INSTALL_MIRROR",
    "UV_PYTHON_DOWNLOADS_JSON_URL",
)

#: Every variable an elevated run drops: those through which a caller picks the interpreter past
#: ``--python-preference only-managed`` (an explicit UV_PYTHON path, an active virtual or conda
#: environment), those uv refuses beside that flag, and :data:`MANAGED_PYTHON_VARIABLES`. Mirrors the
#: runners' own lists, in their order.
INTERPRETER_VARIABLES = (
    "UV_PYTHON",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "UV_MANAGED_PYTHON",
    "UV_NO_MANAGED_PYTHON",
    *MANAGED_PYTHON_VARIABLES,
)


def _real_python() -> str:
    """A real interpreter for a double to hand over to: the one running the suite, links resolved.

    Resolved so a venv's ``python`` (which finds its packages through the ``pyvenv.cfg`` beside it)
    is not what runs: the double only has to answer uv's interpreter query.
    """
    return str(Path(sys.executable).resolve())


def write_planted_python(directory: Path, marker: Path) -> Path:
    """Write ``python`` and ``python3`` doubles (POSIX): each appends its argv to ``marker``, then runs a real one.

    uv EXECUTES every interpreter it considers (it asks it for its version), so a marker file that
    exists afterwards proves the planted interpreter ran, whether or not uv then chose it.

    Args:
        directory: Where to write the doubles (created when missing); put it first on ``PATH``.
        marker: The file each run appends a line to.

    Returns:
        The ``python3`` double.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     write_planted_python(Path(d) / "bin", Path(d) / "ran.txt").name
        'python3'
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("python", "python3"):
        double = directory / name
        double.write_text(
            f'#!/bin/sh\nprintf \'%s\\n\' "$0 $*" >> "{marker}"\nexec "{_real_python()}" "$@"\n',
            encoding="utf-8",
            newline="\n",
        )
        double.chmod(0o755)
    return directory / "python3"


def write_planted_environment(root: Path, marker: Path) -> Path:
    """A virtual (or conda) environment at ``root`` whose ``bin/python`` is a planted double (POSIX).

    The ``pyvenv.cfg`` makes it a virtual environment for ``VIRTUAL_ENV``; ``bin/python`` is all
    ``CONDA_PREFIX`` needs. Returns ``root``.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     (write_planted_environment(Path(d) / "env", Path(d) / "ran.txt") / "pyvenv.cfg").is_file()
        True
    """
    write_planted_python(root / "bin", marker)
    (root / "pyvenv.cfg").write_text(f"home = {Path(_real_python()).parent}\n", encoding="utf-8")
    return root


def write_planted_venv(root: Path, marker: Path) -> Path:
    """A real virtual environment at ``root`` whose interpreter appends a line to ``marker`` on every start.

    Built with the suite's own interpreter (``venv --without-pip``), so it works on every OS, Windows
    included, where no test can write a ``python.exe`` double. A ``sitecustomize`` in its
    site-packages records each start: uv queries an interpreter with ``python -I``, which still
    imports it. Planted as ``.venv`` in a working directory or above, it is what uv probes (and,
    measured with uv 0.13.0, even RUNS under ``--python-preference only-managed``) unless started
    elsewhere. Returns ``root``.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     (write_planted_venv(Path(d) / ".venv", Path(d) / "ran.txt") / "pyvenv.cfg").is_file()
        True
    """
    subprocess.run(  # noqa: S603 - fixed argv from the running interpreter
        [sys.executable, "-m", "venv", "--without-pip", str(root)], check=True, capture_output=True, timeout=120
    )
    site_packages = Path(sysconfig.get_path("purelib", vars={"base": str(root), "platbase": str(root)}))
    site_packages.mkdir(parents=True, exist_ok=True)
    (site_packages / "sitecustomize.py").write_text(
        f"import sys\nwith open({str(marker)!r}, 'a', encoding='utf-8') as _f:\n    _f.write(sys.executable + '\\n')\n",
        encoding="utf-8",
    )
    return root


#: The distribution :func:`write_local_wheel` builds, and the module it carries.
LOCAL_WHEEL_DISTRIBUTION = "pwshpy_probe_wheel"


def write_local_wheel(directory: Path) -> Path:
    """Write a minimal pure-Python wheel of :data:`LOCAL_WHEEL_DISTRIBUTION` into ``directory``.

    Its module holds ``VALUE = "from the wheel"``. It has no dependencies, so uv installs it from
    the file alone. Returns the wheel's path.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     write_local_wheel(Path(d)).name
        'pwshpy_probe_wheel-0.1-py3-none-any.whl'
    """
    name = LOCAL_WHEEL_DISTRIBUTION
    info = f"{name}-0.1.dist-info"
    members = {
        f"{name}/__init__.py": 'VALUE = "from the wheel"\n',
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: pwshpy-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(f"{member},,\n" for member in members) + f"{info}/RECORD,,\n"
    directory.mkdir(parents=True, exist_ok=True)
    wheel = directory / f"{name}-0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for member, text in {**members, f"{info}/RECORD": record}.items():
            archive.writestr(member, text)
    return wheel


def write_recording_uv(directory: Path, record: Path) -> Path:
    """Write a ``uv`` double (POSIX) that records its argv, one argument per line, then exits 0.

    After the arguments it records ``env:<NAME>=<value>`` for each of :data:`INTERPRETER_VARIABLES`
    it was handed, so a test can read both what a runner passed and what it dropped.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     write_recording_uv(Path(d), Path(d) / "argv.txt").name
        'uv'
    """
    report = "".join(
        f'if [ -n "${{{name}+set}}" ]; then printf \'env:%s\\n\' "{name}=${name}" >> "{record}"; fi\n'
        for name in INTERPRETER_VARIABLES
    )
    double = directory / "uv"
    double.write_text(
        f'#!/bin/sh\nfor arg in "$@"; do printf \'%s\\n\' "$arg" >> "{record}"; done\n{report}exit 0\n',
        encoding="utf-8",
        newline="\n",
    )
    double.chmod(0o755)
    return double


@functools.lru_cache(maxsize=1)
def managed_python_dir() -> Path | None:
    """The directory holding this host's uv-managed Pythons, or None when it holds none (or uv is absent).

    Asked of uv itself with downloads off and outside any project, so the answer never reaches the
    network and no ``.python-version`` near the suite steers it. A test points an elevated run at it
    (read only) so the run finds a managed Python without downloading one.

    Example:
        >>> found = managed_python_dir()
        >>> found is None or found.is_dir()
        True
    """
    uv = shutil.which("uv")
    if uv is None:
        return None
    found = _ask_uv(uv, "python", "find", "--no-project", "--python-preference", "only-managed")
    if found.returncode != 0:
        return None
    directory = _ask_uv(uv, "python", "dir")
    path = Path(directory.stdout.strip())
    return path if directory.returncode == 0 and path.is_dir() else None


def _ask_uv(uv: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run ``uv arguments`` with downloads off, no interpreter variables, outside any project."""
    environment = {**os.environ, "UV_PYTHON_DOWNLOADS": "never"}
    for name in INTERPRETER_VARIABLES:
        environment.pop(name, None)
    return subprocess.run(  # noqa: S603 - fixed argv from a resolved uv
        [uv, *arguments],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
        env=environment,
        cwd=Path(uv).parent,
    )


def interpreter_probe_env(scratch: Path, *, managed: Path | None) -> dict[str, str]:
    """Variables that confine uv's interpreter search to one test: a fresh cache, downloads off.

    A fresh cache because uv remembers what it learned about an interpreter and would not run a
    planted one again; ``managed`` is the managed-install directory to use (an empty one under
    ``scratch`` when None). It is named twice, because an elevated run drops UV_PYTHON_INSTALL_DIR
    ([59]) and then takes uv's default, ``<data home>/uv/python``: see :func:`managed_python_env`.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     interpreter_probe_env(Path(d), managed=None)["UV_PYTHON_DOWNLOADS"]
        'never'
    """
    return {
        "UV_CACHE_DIR": str(scratch / "uv-cache"),
        "UV_PYTHON_DOWNLOADS": "never",
        **managed_python_env(scratch, managed=managed),
    }


def managed_python_env(scratch: Path, *, managed: Path | None) -> dict[str, str]:
    """Point uv at ``managed`` as its managed-Python directory, both with and without UV_PYTHON_INSTALL_DIR.

    UV_PYTHON_INSTALL_DIR names it outright; an elevated run drops that variable, and uv then uses
    ``<data home>/uv/python``, where the data home is XDG_DATA_HOME on POSIX and APPDATA on Windows
    (measured with uv 0.13: XDG_DATA_HOME changes nothing on Windows). So the data home is set too:
    to the directory two above ``managed`` when that already ends in ``uv/python`` (uv's own default
    layout), else to a scratch directory whose ``uv/python`` links to it. With ``managed`` None both
    name an empty directory, so uv finds no managed Python either way.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     env = managed_python_env(Path(d), managed=None)
        ...     Path(env["UV_PYTHON_INSTALL_DIR"]).is_dir(), len(env)
        (True, 2)
    """
    data_home = scratch / "data-home"
    install = data_home / "uv" / "python"
    if managed is None:
        install.mkdir(parents=True, exist_ok=True)
    elif managed.name == "python" and managed.parent.name == "uv":
        data_home, install = managed.parent.parent, managed
    elif not install.is_symlink():
        install.parent.mkdir(parents=True, exist_ok=True)
        install.symlink_to(managed, target_is_directory=True)
    home_variable = "APPDATA" if sys.platform == "win32" else "XDG_DATA_HOME"
    return {"UV_PYTHON_INSTALL_DIR": str(install), home_variable: str(data_home)}


__all__ = [
    "INTERPRETER_VARIABLES",
    "LOCAL_WHEEL_DISTRIBUTION",
    "MANAGED_PYTHON_VARIABLES",
    "RUNNING_AS_ROOT",
    "interpreter_probe_env",
    "link_tools",
    "managed_python_dir",
    "managed_python_env",
    "write_fake_sudo",
    "write_planted_environment",
    "write_local_wheel",
    "write_planted_python",
    "write_planted_venv",
    "write_recording_uv",
]
