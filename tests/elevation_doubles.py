"""Test doubles for the packed runners' elevation path, shared by the ``.sh`` and ``.ps1`` e2e tests.

A refused sudo password cannot be typed without a TTY, so a ``sudo`` double on ``PATH`` stands in
for it. Imported by bare module name (``import elevation_doubles``): ``tests/`` has no
``__init__.py``, so pytest puts this directory on ``sys.path`` for the test files beside it.

Contents:
    * :data:`RUNNING_AS_ROOT` - whether this process is root (the runners then never elevate).
    * :func:`owned_by_another_account` - a file this suite made, owned by an account other than root.
    * :func:`write_fake_sudo` - write a ``sudo`` double into a directory.
    * :func:`link_tools` - a ``PATH`` directory holding only the named tools.
    * :data:`MANAGED_PYTHON_VARIABLES` - the variables that steer where uv's managed Pythons come from.
    * :data:`INTERPRETER_VARIABLES` - the variables an elevated run drops before uv picks a Python.
    * :data:`LIBRARY_VARIABLES` - the variables the interpreter reads itself, dropped too.
    * :func:`write_planted_python` - ``python`` / ``python3`` doubles that record being run.
    * :func:`write_planted_environment` - a virtual or conda environment whose python is such a double.
    * :func:`write_planted_venv` - a real virtual environment whose python records every start.
    * :func:`write_local_wheel` - a dependency-free wheel uv installs without the network.
    * :func:`write_recording_uv` - a ``uv`` double that records its argv and the variables it got.
    * :func:`managed_python_dir` - the host's uv-managed Python directory, when one holds an install.
    * :func:`trusted_managed_python` - that directory, or a copy of one install an elevated run may trust.
    * :func:`write_site_plant` - a ``.pth`` that records every start of an interpreter reading it.
    * :func:`planted_managed_python` - a managed-Python directory whose install carries such a plant.
    * :func:`interpreter_probe_env` - a uv search confined to one test (fresh cache, no downloads).
    * :func:`session_uv_cache` - a uv cache directory for the whole test session, never a real user's.
    * :func:`managed_python_env` - name uv's managed-Python directory so an elevated run finds it too.
    * :func:`data_home` - the data directory a test makes an elevated runner believe is its account's.
    * :func:`with_data_home` - a runner whose account data-home lookup answers :func:`data_home`.
    * :func:`without_python_dir_check` - a runner that skips judging uv's managed-Python directory.
"""

from __future__ import annotations

import atexit
import functools
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path

#: The runners only elevate for a non-root uid, so as root no test reaches the sudo branch.
#: Guarded by ``sys.platform`` because ``os.geteuid`` does not exist on Windows.
RUNNING_AS_ROOT: bool = sys.platform != "win32" and os.geteuid() == 0


#: The uid a root run of the suite gives a file it must see owned by another account (nobody).
OTHER_UID = 65534


def owned_by_another_account(path: Path) -> int:
    """Make ``path`` (a link itself, not its target) another account's; return the owner it has now.

    A file the suite creates is the suite's: a non-root uid, unless the suite runs as root, when the
    runners' root rule would trust it. So as root it goes to nobody; otherwise it is left as it is.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     owned = owned_by_another_account(Path(d))
        >>> RUNNING_AS_ROOT is False or owned == OTHER_UID
        True
    """
    # sys.platform first: os.lchown does not exist on Windows, where nothing runs as root anyway.
    if sys.platform != "win32" and RUNNING_AS_ROOT:
        os.lchown(path, OTHER_UID, OTHER_UID)
    return path.lstat().st_uid


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

#: The variables the interpreter reads itself, whichever one runs (another standard library, the
#: caller's directories ahead of it); an elevated run drops these too. Mirrors the runners' list.
LIBRARY_VARIABLES = ("PYTHONHOME", "PYTHONPATH")


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


def write_planted_venv(root: Path, marker: Path, *, interpreter: str | None = None) -> Path:
    """A real virtual environment at ``root`` whose interpreter appends a line to ``marker`` on every start.

    Built with the suite's own interpreter (``venv --without-pip``), or ``interpreter`` when given, so
    it works on every OS, Windows included, where no test can write a ``python.exe`` double. A ``.pth``
    file in its site-packages records each start: uv queries an interpreter with ``python -I``, which
    still processes it. Planted as ``.venv`` in a working directory or above, it is what uv probes (and,
    measured with uv 0.13.0, even RUNS under ``--python-preference only-managed``) unless started
    elsewhere. Returns ``root``.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     (write_planted_venv(Path(d) / ".venv", Path(d) / "ran.txt") / "pyvenv.cfg").is_file()
        True
    """
    subprocess.run(  # noqa: S603 - fixed argv from the running interpreter
        [interpreter or sys.executable, "-m", "venv", "--without-pip", str(root)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    site_packages = _venv_site_packages(root)
    site_packages.mkdir(parents=True, exist_ok=True)
    # A .pth line that starts with "import" is executed by the site module at every start, isolated
    # (-I) ones included. Not a sitecustomize.py: Debian and Ubuntu ship one in the system Python's own
    # library directory, which is found first and shadows the venv's.
    (site_packages / "pwshpy_planted_marker.pth").write_text(
        f"import sys; open({str(marker)!r}, 'a', encoding='utf-8').write(sys.executable + '\\n')\n",
        encoding="utf-8",
    )
    return root


def _venv_site_packages(root: Path) -> Path:
    """The ``site-packages`` directory of the virtual environment at ``root``, whichever Python built it.

    Read from the layout on disk rather than from this interpreter's ``sysconfig``: a venv built from
    another Python (a Debian ``/usr/bin/python3.X``) has that version in its ``lib/pythonX.Y`` path.
    """
    windows = root / "Lib" / "site-packages"
    if windows.is_dir():
        return windows
    found = sorted((root / "lib").glob("python*/site-packages"))
    if found:
        return found[0]
    return Path(sysconfig.get_path("purelib", vars={"base": str(root), "platbase": str(root)}))


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
    and :data:`LIBRARY_VARIABLES` it was handed, so a test can read both what a runner passed and
    what it dropped. A ``uv python ...`` call (an elevated run resolving its interpreter) is recorded
    the same way into ``record`` with ``.python`` appended to its name, and ``uv python find`` prints
    the path of a stand-in ``python`` beside the double, so the run that follows has one to name.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     write_recording_uv(Path(d), Path(d) / "argv.txt").name
        'uv'
    """
    report = "".join(
        f'if [ -n "${{{name}+set}}" ]; then printf \'env:%s\\n\' "{name}=${name}" >> "$record"; fi\n'
        for name in (*INTERPRETER_VARIABLES, *LIBRARY_VARIABLES)
    )
    python = directory / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    python.chmod(0o755)
    double = directory / "uv"
    double.write_text(
        f'#!/bin/sh\nrecord="{record}"\nif [ "$1" = python ]; then record="{record}.python"; fi\n'
        f'for arg in "$@"; do printf \'%s\\n\' "$arg" >> "$record"; done\n{report}'
        f'if [ "$1 $2" = "python find" ]; then printf \'%s\\n\' "{python}"; fi\nexit 0\n',
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
    found = _ask_uv(uv, *_FIND_MANAGED)
    if found.returncode != 0:
        return None
    directory = _ask_uv(uv, "python", "dir")
    path = Path(directory.stdout.strip())
    return path if directory.returncode == 0 and path.is_dir() else None


@functools.lru_cache(maxsize=1)
def trusted_managed_python() -> Path | None:
    """A managed-Python directory an elevated run's trust walk accepts, or None when the host has none.

    An elevated run refuses an interpreter whose file or any directory above it another account can
    modify ([93]). On Windows the host's own directory (under the profile) passes. On POSIX uv creates
    it with the account's umask, which is group-writable on a host with per-user groups, so this
    copies the install uv would pick into a fresh directory with group and other write removed, once
    per test session (about 100 MB), and deletes it at exit. Read only for the host's own directory.

    Example:
        >>> found = trusted_managed_python()
        >>> found is None or found.is_dir()
        True
    """
    managed = managed_python_dir()
    uv = shutil.which("uv")
    if managed is None or uv is None or sys.platform == "win32":
        return managed
    found = _ask_uv(uv, *_FIND_MANAGED)
    install = Path(found.stdout.strip()).resolve().parents[1]
    scratch = Path(tempfile.mkdtemp(prefix="pwshpy-managed-python-"))
    atexit.register(shutil.rmtree, scratch, ignore_errors=True)
    # One level below the temp directory: an elevated run judges the managed-Python directory itself
    # and the one holding it, and /tmp is writable by every account (sticky or not).
    root = scratch / "managed"
    root.mkdir()
    shutil.copytree(install, root / install.name, symlinks=True)
    for directory, _subdirectories, files in os.walk(root):
        for path in (Path(directory), *(Path(directory) / name for name in files)):
            if not path.is_symlink():
                path.chmod(path.stat().st_mode & ~0o022)
    return root


def write_site_plant(site_packages: Path, marker: Path) -> Path:
    """Write a ``.pth`` into ``site_packages`` that appends a line to ``marker`` on every interpreter start.

    The site module executes a ``.pth`` line starting with ``import`` while the interpreter starts,
    before any script, so the marker shows that something ran the interpreter at all - uv asking it
    for its version included.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     write_site_plant(Path(d) / "site", Path(d) / "ran.txt").read_text(encoding="utf-8")[:6]
        'import'
    """
    site_packages.mkdir(parents=True, exist_ok=True)
    plant = site_packages / "pwshpy_planted.pth"
    line = f"import pathlib; pathlib.Path({str(marker)!r}).open('a', encoding='utf-8').write('ran\\n')\n"
    plant.write_text(line, encoding="utf-8")
    return plant


def planted_managed_python(destination: Path, marker: Path) -> Path | None:
    """A managed-Python directory holding the install an elevated run would pick, with a site plant in it.

    The install of :func:`trusted_managed_python` is linked into ``destination`` file by file (a hard
    link where the filesystem allows, a copy otherwise), so its directories are this test's own: a
    test may change their modes or plant into them without touching the shared copy or, on Windows,
    the host's own directory. Only directories are new, so change their modes and ACLs without
    inheritance, or the change reaches the shared files. None when the host has no managed Python.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     planted = planted_managed_python(Path(d) / "managed", Path(d) / "ran.txt")
        >>> planted is None or planted.name == "managed"
        True
    """
    managed = trusted_managed_python()
    uv = shutil.which("uv")
    if managed is None or uv is None:
        return None
    install = _install_holding(Path(_ask_uv(uv, *_FIND_MANAGED).stdout.strip()))
    if install is None:
        return None
    destination.mkdir(parents=True)
    # Whatever the umask: an elevated run refuses a managed-Python directory another account (the group,
    # on a host with per-user groups) can modify. The install's own directories keep the source's modes.
    destination.chmod(destination.stat().st_mode & ~0o022)
    shutil.copytree(managed / install.name, destination / install.name, symlinks=True, copy_function=_link_or_copy)
    for site_packages in (*destination.glob("*/lib/python3*/site-packages"), *destination.glob("*/Lib/site-packages")):
        write_site_plant(site_packages, marker)
    return destination


def _install_holding(executable: Path) -> Path | None:
    """The install directory (a child of the host's managed-Python directory) holding ``executable``."""
    root = managed_python_dir()
    if root is None:
        return None
    return next((parent for parent in executable.resolve().parents if parent.parent == root.resolve()), None)


def _link_or_copy(source: str, destination: str) -> str:
    """Hard-link ``source`` to ``destination`` (about 100 MB per install saved), else copy it."""
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


#: How the helpers ask uv for a managed Python: as an elevated run does (``--system``), so the answer is
#: an install uv manages, never a ``.venv`` near uv's directory or an active environment.
_FIND_MANAGED = ("python", "find", "--system", "--no-project", "--python-preference", "only-managed")


def _ask_uv(uv: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run ``uv arguments`` with downloads off, no interpreter variables, outside any project."""
    # UV_PYTHON_INSTALL_DIR stays: a run that isolates uv's managed Pythons (a CT, a CI job) names
    # them there, and the answer must be that directory, not the default the variable overrides.
    environment = {**os.environ, "UV_PYTHON_DOWNLOADS": "never"}
    for name in INTERPRETER_VARIABLES:
        if name != "UV_PYTHON_INSTALL_DIR":
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


@functools.lru_cache(maxsize=1)
def session_uv_cache() -> Path:
    """A uv cache directory shared by the whole test session and deleted at exit.

    A packed run that names no UV_CACHE_DIR would build its script environments in the cache of
    whoever runs the suite. One directory for the session, not one per test: the deliberate cowsay
    fetch and the local wheels then resolve once.

    Example:
        >>> session_uv_cache().is_dir()
        True
    """
    root = Path(tempfile.mkdtemp(prefix="pwshpy-uv-cache-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    return root


def interpreter_probe_env(scratch: Path, *, managed: Path | None) -> dict[str, str]:
    """Variables that confine uv's interpreter search to one test: a fresh cache, downloads off.

    A fresh cache because uv remembers what it learned about an interpreter and would not run a
    planted one again; ``managed`` is the managed-install directory to use (an empty one under
    ``scratch`` when None), named as :func:`managed_python_env` names it.

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


def data_home(scratch: Path) -> Path:
    """The data directory a test's elevated runner takes for its account's (see :func:`with_data_home`).

    Example:
        >>> data_home(Path("/s")).as_posix()
        '/s/data-home'
    """
    return scratch / "data-home"


def managed_python_env(scratch: Path, *, managed: Path | None) -> dict[str, str]:
    """Make ``<data home>/uv/python`` name ``managed`` and point UV_PYTHON_INSTALL_DIR at it too.

    An elevated run drops UV_PYTHON_INSTALL_DIR ([59]) and names ``<its account's data home>/uv/python``
    itself, from the passwd database or the RoamingAppData known folder ([93]), so a test hands it
    :func:`data_home` through :func:`with_data_home` and this links that directory's ``uv/python`` to
    ``managed`` (an empty directory when None, so uv finds no managed Python). An unelevated run reads
    UV_PYTHON_INSTALL_DIR, set here to the same directory; the data-home variable (XDG_DATA_HOME on
    POSIX, APPDATA on Windows) is set too, so neither run falls back to the real user's. A link or
    directory an earlier call left there (uv writes a ``.lock`` into an empty one) is replaced.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     first = managed_python_env(Path(d), managed=None)
        ...     again = managed_python_env(Path(d), managed=None)
        ...     Path(again["UV_PYTHON_INSTALL_DIR"]).is_dir(), len(again)
        (True, 2)
    """
    install = data_home(scratch) / "uv" / "python"
    install.parent.mkdir(parents=True, exist_ok=True)
    # Whatever the umask: an elevated run refuses an interpreter below a directory another account
    # (the group, on a host with per-user groups) can modify.
    for directory in (install.parent.parent, install.parent):
        directory.chmod(directory.stat().st_mode & ~0o022)
    if install.is_symlink():
        install.unlink()
    elif install.is_dir():
        shutil.rmtree(install)
    if managed is None:
        install.mkdir()
        install.chmod(install.stat().st_mode & ~0o022)
    else:
        install.symlink_to(managed, target_is_directory=True)
    home_variable = "APPDATA" if sys.platform == "win32" else "XDG_DATA_HOME"
    return {"UV_PYTHON_INSTALL_DIR": str(install), home_variable: str(data_home(scratch))}


#: The account data-home lookup each runner makes when elevated: the ``.sh`` runner's command
#: substitution, and the ``.ps1`` runner's function header.
SH_DATA_HOME_LOOKUP = "$(pwshpy_account_data_home)"
PS1_DATA_HOME_LOOKUP = "function Get-PwshPyAccountDataHome {"


def with_data_home(text: str, scratch: Path, *, powershell: bool) -> str:
    """``text`` (a rendered runner) with its account data-home lookup answering :func:`data_home`.

    A test cannot make the passwd database or the known-folder API name a directory of its own, and
    the real answer is the real user's data directory, which a test must not read uv's Pythons from
    or write to. One counted substitution; the rest is the shipped runner. The ``.ps1`` lookup is
    renamed rather than removed, so the runner keeps a definition of it.

    Example:
        >>> with_data_home("X=$(pwshpy_account_data_home) || exit", Path("/s"), powershell=False)
        "X='/s/data-home' || exit"
    """
    home = str(data_home(scratch))
    if powershell:
        assert text.count(PS1_DATA_HOME_LOOKUP) == 1
        answer = "'" + home.replace("'", "''") + "'"
        stand_in = f"{PS1_DATA_HOME_LOOKUP}\n    return {answer}\n}}\n\nfunction Get-PwshPyAccountDataHomeOnHost {{"
        return text.replace(PS1_DATA_HOME_LOOKUP, stand_in)
    assert text.count(SH_DATA_HOME_LOOKUP) == 1
    return text.replace(SH_DATA_HOME_LOOKUP, "'" + home.replace("'", "'\\''") + "'")


#: Where each runner judges uv's managed-Python directory before uv searches it, and what a test that
#: is about something else puts in its place (the directory is then taken as trusted).
SH_PYTHON_DIR_CHECK = 'PWSHPY_PYTHON_DIR_WHY=$(pwshpy_python_dir_distrust "$UV_PYTHON_INSTALL_DIR")'
PS1_PYTHON_DIR_CHECK = "$PwshPyPythonDirWhy = Get-PwshPyPythonDirDistrust -Path $env:UV_PYTHON_INSTALL_DIR"


def without_python_dir_check(text: str, *, powershell: bool) -> str:
    """``text`` (a rendered runner) with the check of uv's managed-Python directory answering "trusted".

    For a test of what an elevated run names or drops, run with the REAL account data home: that
    directory is the user's own, and on a host with per-user groups uv made it group-writable, so the
    check would stop the run before the part under test. One counted substitution.

    Example:
        >>> without_python_dir_check(SH_PYTHON_DIR_CHECK, powershell=False)
        'PWSHPY_PYTHON_DIR_WHY='
    """
    check, answer = (
        (PS1_PYTHON_DIR_CHECK, "$PwshPyPythonDirWhy = ''")
        if powershell
        else (SH_PYTHON_DIR_CHECK, "PWSHPY_PYTHON_DIR_WHY=")
    )
    assert text.count(check) == 1
    return text.replace(check, answer)


__all__ = [
    "INTERPRETER_VARIABLES",
    "LIBRARY_VARIABLES",
    "LOCAL_WHEEL_DISTRIBUTION",
    "MANAGED_PYTHON_VARIABLES",
    "OTHER_UID",
    "PS1_DATA_HOME_LOOKUP",
    "PS1_PYTHON_DIR_CHECK",
    "RUNNING_AS_ROOT",
    "SH_DATA_HOME_LOOKUP",
    "SH_PYTHON_DIR_CHECK",
    "data_home",
    "trusted_managed_python",
    "interpreter_probe_env",
    "link_tools",
    "managed_python_dir",
    "managed_python_env",
    "owned_by_another_account",
    "session_uv_cache",
    "planted_managed_python",
    "with_data_home",
    "without_python_dir_check",
    "write_fake_sudo",
    "write_planted_environment",
    "write_local_wheel",
    "write_planted_python",
    "write_planted_venv",
    "write_recording_uv",
    "write_site_plant",
]
