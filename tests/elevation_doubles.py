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
    * :data:`GIT_VARIABLES` - git and credential-helper variables an elevated run drops (GIT_, GCM_ by prefix).
    * :func:`write_git_source` - a one-commit git repository uv can fetch from a git+file URL.
    * :func:`write_planted_python` - ``python`` / ``python3`` doubles that record being run.
    * :func:`write_planted_environment` - a virtual or conda environment whose python is such a double.
    * :func:`write_planted_venv` - a real virtual environment whose python records every start.
    * :func:`write_local_wheel` - a dependency-free wheel uv installs without the network.
    * :func:`write_recording_uv` - a ``uv`` double that records its argv and the variables it got.
    * :func:`managed_python_dir` - the host's uv-managed Python directory, when one holds an install.
    * :func:`trusted_managed_python` - that directory, or a copy of one install an elevated run may trust.
    * :func:`write_site_plant` - a ``.pth`` that records every start of an interpreter reading it.
    * :func:`planted_managed_python` - a managed-Python directory whose install carries such a plant.
    * :func:`plant_library_path` - a caller-supplied library path (env file, user base, user site) planted.
    * :func:`default_uv_cache_env` - point uv's default cache (no UV_CACHE_DIR) into a test's directory.
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
#: directory, another config file, another download mirror or download list ([59]); [X22 E1] the
#: mirrors uv 0.13 also fetches CPython (UV_ASTRAL_MIRROR_URL), PyPy, GraalPy and Pyodide through.
MANAGED_PYTHON_VARIABLES = (
    "UV_PYTHON_INSTALL_DIR",
    "UV_CONFIG_FILE",
    "UV_PYTHON_INSTALL_MIRROR",
    "UV_PYTHON_DOWNLOADS_JSON_URL",
    "UV_ASTRAL_MIRROR_URL",
    "UV_PYPY_INSTALL_MIRROR",
    "UV_GRAALPY_INSTALL_MIRROR",
    "UV_PYODIDE_INSTALL_MIRROR",
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
#: caller's directories ahead of it, the caller's own site-packages); an elevated run drops these too.
#: Mirrors the runners' list.
LIBRARY_VARIABLES = ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE")

#: The variables that name where uv takes packages, its cache or an env file from (a package index,
#: a find-links location, an insecure host, a cache directory, an env file): an elevated run drops
#: them and uses uv's defaults (user decision, X21 P3). Mirrors the runners' list, in its order.
UV_SOURCE_VARIABLES = (
    "UV_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_INDEX",
    "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS",
    "UV_INSECURE_HOST",
    "UV_INSECURE_NO_ZIP_VALIDATION",
    "UV_CACHE_DIR",
    "UV_PYTHON_CACHE_DIR",
    "UV_ENV_FILE",
)


#: The variables that decide how uv connects ([X22 E1]): a proxy (each spelling uv 0.13 reads), the
#: certificate roots it trusts, the client certificate it presents, and a keyring helper it runs. An
#: elevated run drops them; mirrors the runners' list, in its order.
NETWORK_VARIABLES = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "SSL_CLIENT_CERT",
    "UV_NATIVE_TLS",
    "UV_SYSTEM_CERTS",
    "UV_KEYRING_PROVIDER",
)

#: [X23 G1] Variables git reads, which an elevated run drops because their name starts with GIT_ (one
#: no git release reads, so a fixed list would miss it), plus SSH_ASKPASS, which git runs for a
#: password. [113]: and the ones Git Credential Manager, the credential helper git runs, reads: every
#: GCM_ name (by prefix too; GCM_GPG_PATH names the gpg it runs, GCM_PLAINTEXT_STORE_PATH a credential
#: directory) and PASSWORD_STORE_DIR (where its gpg store reads credentials). The values are never
#: used: a recording uv runs no git.
GIT_VARIABLES = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.fsmonitor",
    "GIT_CONFIG_VALUE_0": "pwshpy-planted-hook",
    "GIT_SSH_COMMAND": "pwshpy-planted-ssh",
    "GIT_PWSHPY_NOT_READ_BY_ANY_GIT": "1",
    "SSH_ASKPASS": "pwshpy-planted-askpass",
    "GCM_CREDENTIAL_STORE": "plaintext",
    "GCM_PLAINTEXT_STORE_PATH": "pwshpy-planted-store",
    "GCM_GPG_PATH": "pwshpy-planted-gpg",
    "GCM_PWSHPY_NOT_READ_BY_ANY_GCM": "1",
    "PASSWORD_STORE_DIR": "pwshpy-planted-password-store",
}

#: A variable whose name holds GIT_ without starting with it: an elevated run keeps it, so the
#: tests see the drop is by prefix, not by substring.
NOT_A_GIT_VARIABLE = "PWSHPY_GIT_NOT_A_PREFIX"

#: The same for the GCM_ prefix ([113]).
NOT_A_GCM_VARIABLE = "PWSHPY_GCM_NOT_A_PREFIX"


def write_git_source(scratch: Path) -> Path:
    """A one-commit git repository below ``scratch`` holding a project uv can read from a git+file URL.

    The source a git e2e test hands a packed run (``--with``), so uv runs git to fetch it and any git
    configuration the run lets through gets its chance to run a command.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     shutil.which("git") is None or (write_git_source(Path(d)) / ".git").is_dir()
        True
    """
    repo = scratch / "git-source"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "pwshpy-git-probe"\nversion = "0.1"\n', encoding="utf-8")
    identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
    for argv in (["init", "-q", "."], ["add", "pyproject.toml"], [*identity, "commit", "-qm", "x"]):
        subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True)  # noqa: S603, S607
    return repo


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

    After the arguments it records ``env:<NAME>=<value>`` for each of :data:`INTERPRETER_VARIABLES`,
    :data:`LIBRARY_VARIABLES`, :data:`UV_SOURCE_VARIABLES`, :data:`NETWORK_VARIABLES`,
    ``SSH_ASKPASS``, ``PASSWORD_STORE_DIR``, ``PYTHONNOUSERSITE`` and every variable whose name holds
    ``GIT_`` or ``GCM_`` it was handed, so a
    test can read both what a runner passed, dropped and set. A ``uv python ...`` call (an elevated
    run resolving its interpreter) is recorded the same way into ``record`` with ``.python`` appended
    to its name, and ``uv python find`` prints the path of a stand-in ``python`` beside the double, so
    the run that follows has one to name.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     write_recording_uv(Path(d), Path(d) / "argv.txt").name
        'uv'
    """
    report = "".join(
        f'if [ -n "${{{name}+set}}" ]; then printf \'env:%s\\n\' "{name}=${name}" >> "$record"; fi\n'
        for name in (
            *INTERPRETER_VARIABLES,
            *LIBRARY_VARIABLES,
            *UV_SOURCE_VARIABLES,
            *NETWORK_VARIABLES,
            "SSH_ASKPASS",
            "PASSWORD_STORE_DIR",
            "PYTHONNOUSERSITE",
        )
    )
    # Every name holding GIT_ or GCM_, found by pattern: the runners drop by prefix, so no list can name
    # them all.
    report += "env | sed -n -e 's/^\\([A-Za-z0-9_]*GIT_[A-Za-z0-9_]*=.*\\)$/env:\\1/p' "
    report += "-e 's/^\\([A-Za-z0-9_]*GCM_[A-Za-z0-9_]*=.*\\)$/env:\\1/p' >> \"$record\"\n"
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
    # Whatever the umask: an elevated run refuses a managed-Python tree holding a file another account
    # (the group, on a host with per-user groups) can modify, and a control must leave it trusted.
    plant.chmod(0o644)
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


#: The ways a caller's environment can put its own library directory in front of an elevated Python
#: (X21 P2): an env file uv loads (holding PYTHONPATH), PYTHONUSERBASE, and the user site-packages
#: below HOME (Windows: APPDATA), which some sudo setups keep.
LIBRARY_PATH_PLANTS = ("env-file", "user-base", "home-user-site")


def plant_library_path(scratch: Path, kind: str, marker: Path, *, home: Path) -> dict[str, str]:
    """Plant code that appends to ``marker`` when a Python starts, reachable through ``kind``.

    Returns the variables that make it reachable (none for ``home-user-site`` on POSIX, where ``home``
    is the HOME the run already gets). The user site is planted for every version from 3.8 to 3.16,
    free-threaded included, so whichever managed Python runs finds it.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     sorted(plant_library_path(Path(d), "env-file", Path(d) / "m", home=Path(d)))
        ['UV_ENV_FILE']
    """
    if kind == "env-file":
        injected = scratch / "env-file-path"
        injected.mkdir()
        (injected / "sitecustomize.py").write_text(
            f"import pathlib\npathlib.Path({str(marker)!r}).open('a', encoding='utf-8').write('ran\\n')\n",
            encoding="utf-8",
        )
        env_file = scratch / "injected.env"
        # Forward slashes: uv reads a backslash in UV_ENV_FILE as an escape, so a Windows path loses its
        # separators ("No environment file found at: C:Users...", uv 0.13 on Windows); "/" works there.
        env_file.write_text(f"PYTHONPATH={injected.as_posix()}\n", encoding="utf-8")
        return {"UV_ENV_FILE": env_file.as_posix()}
    if kind == "user-base":
        base = scratch / "user-base"
        _plant_user_site(base, marker)
        return {"PYTHONUSERBASE": str(base)}
    if kind != "home-user-site":
        raise ValueError(kind)
    if sys.platform == "win32":
        appdata = scratch / "appdata"
        _plant_user_site(appdata / "Python", marker)
        return {"APPDATA": str(appdata)}
    _plant_user_site(home / ".local", marker)
    return {}


def _plant_user_site(base: Path, marker: Path) -> None:
    """A site plant in every user site-packages below user base ``base`` that site.py could pick."""
    for minor in range(8, 17):
        for suffix in ("", "t"):
            if sys.platform == "win32":
                write_site_plant(base / f"Python3{minor}{suffix}" / "site-packages", marker)
            else:
                write_site_plant(base / "lib" / f"python3.{minor}{suffix}" / "site-packages", marker)


def default_uv_cache_env(scratch: Path) -> dict[str, str]:
    """Point uv's default cache directory, the one it uses without UV_CACHE_DIR, into ``scratch``.

    Below XDG_CACHE_HOME on POSIX and LOCALAPPDATA on Windows (measured with uv 0.13), which must never
    be the real user's ([93]). An elevated run no longer reads either ([X22 C1]: it names its account's
    own cache, which a test answers with the same directory, see :func:`with_account_cache_home`), so
    this keeps any other run that names no UV_CACHE_DIR inside the test.

    Example:
        >>> sorted(default_uv_cache_env(Path("/s")).values()) == [str(Path("/s") / "default-cache")]
        True
    """
    variable = "LOCALAPPDATA" if sys.platform == "win32" else "XDG_CACHE_HOME"
    return {variable: str(scratch / "default-cache")}


def default_uv_cache_dir(scratch: Path) -> Path:
    """The cache directory uv uses under :func:`default_uv_cache_env` (``uv\\cache`` on Windows).

    Also the one an elevated runner names under :func:`with_account_cache_home`.

    Example:
        >>> default_uv_cache_dir(Path("/s")).name in ("uv", "cache")
        True
    """
    root = scratch / "default-cache" / "uv"
    return root / "cache" if sys.platform == "win32" else root


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


#: The account cache-home lookup each runner makes when elevated ([X22 C1]), as for the data home.
SH_CACHE_HOME_LOOKUP = "$(pwshpy_account_cache_home)"
PS1_CACHE_HOME_LOOKUP = "function Get-PwshPyAccountCacheHome {"


def _answering(text: str, directory: Path, *, powershell: bool, lookups: tuple[str, str]) -> str:
    """``text`` with one account lookup (``lookups`` = (sh, ps1) form) answering ``directory``."""
    value = str(directory)
    sh_lookup, ps1_lookup = lookups
    if powershell:
        assert text.count(ps1_lookup) == 1
        answer = "'" + value.replace("'", "''") + "'"
        renamed = ps1_lookup.replace(" {", "OnHost {")
        return text.replace(ps1_lookup, f"{ps1_lookup}\n    return {answer}\n}}\n\n{renamed}")
    assert text.count(sh_lookup) == 1
    return text.replace(sh_lookup, "'" + value.replace("'", "'\\''") + "'")


def with_account_cache_home(text: str, scratch: Path, *, powershell: bool) -> str:
    """``text`` (a rendered runner) with its account cache-home lookup answering ``scratch/default-cache``.

    An elevated run names ``<its account's cache home>/uv`` (``uv\\cache`` on Windows) as uv's cache
    ([X22 C1]), and the real answer is the real user's cache, which a test must never write ([93]).
    The directory is the one :func:`default_uv_cache_env` names, so :func:`default_uv_cache_dir` is the
    cache an elevated test run uses. One counted substitution.

    Example:
        >>> out = with_account_cache_home("C=$(pwshpy_account_cache_home) || exit", Path("/s"), powershell=False)
        >>> out == "C='%s' || exit" % (Path("/s") / "default-cache")
        True
    """
    return _answering(
        text, scratch / "default-cache", powershell=powershell, lookups=(SH_CACHE_HOME_LOOKUP, PS1_CACHE_HOME_LOOKUP)
    )


def with_data_home(text: str, scratch: Path, *, powershell: bool) -> str:
    """``text`` (a rendered runner) with its account data-home lookup answering :func:`data_home`.

    A test cannot make the passwd database or the known-folder API name a directory of its own, and
    the real answer is the real user's data directory, which a test must not read uv's Pythons from
    or write to. The cache-home lookup is answered too (:func:`with_account_cache_home`). One counted
    substitution each; the rest is the shipped runner. A ``.ps1`` lookup is renamed rather than
    removed, so the runner keeps a definition of it.

    Example:
        >>> text = "X=$(pwshpy_account_data_home) C=$(pwshpy_account_cache_home)"
        >>> with_data_home(text, Path("/s"), powershell=False) == "X='%s' C='%s'" % (
        ...     data_home(Path("/s")), Path("/s") / "default-cache")
        True
    """
    text = with_account_cache_home(text, scratch, powershell=powershell)
    return _answering(
        text, data_home(scratch), powershell=powershell, lookups=(SH_DATA_HOME_LOOKUP, PS1_DATA_HOME_LOOKUP)
    )


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
    "GIT_VARIABLES",
    "INTERPRETER_VARIABLES",
    "LIBRARY_PATH_PLANTS",
    "LIBRARY_VARIABLES",
    "LOCAL_WHEEL_DISTRIBUTION",
    "MANAGED_PYTHON_VARIABLES",
    "NETWORK_VARIABLES",
    "NOT_A_GCM_VARIABLE",
    "NOT_A_GIT_VARIABLE",
    "OTHER_UID",
    "PS1_CACHE_HOME_LOOKUP",
    "PS1_DATA_HOME_LOOKUP",
    "PS1_PYTHON_DIR_CHECK",
    "RUNNING_AS_ROOT",
    "UV_SOURCE_VARIABLES",
    "SH_CACHE_HOME_LOOKUP",
    "SH_DATA_HOME_LOOKUP",
    "SH_PYTHON_DIR_CHECK",
    "data_home",
    "default_uv_cache_dir",
    "default_uv_cache_env",
    "trusted_managed_python",
    "interpreter_probe_env",
    "link_tools",
    "managed_python_dir",
    "managed_python_env",
    "owned_by_another_account",
    "session_uv_cache",
    "plant_library_path",
    "planted_managed_python",
    "with_account_cache_home",
    "with_data_home",
    "without_python_dir_check",
    "write_fake_sudo",
    "write_git_source",
    "write_planted_environment",
    "write_local_wheel",
    "write_planted_python",
    "write_planted_venv",
    "write_recording_uv",
    "write_site_plant",
]
