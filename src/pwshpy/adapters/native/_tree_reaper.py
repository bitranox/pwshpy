"""POSIX ``wait_tree`` helper: start one program, then wait until every process it spawned is gone.

This file runs as its OWN process (``python -I _tree_reaper.py <control-fd> <status-fd>``),
started by :mod:`~pwshpy.adapters.native.process_tree_posix`.  It never runs inside the
caller because both strategies change process-wide state that a library must not change
in its host:

* **Linux** - the helper makes itself a child subreaper (``prctl(PR_SET_CHILD_SUBREAPER)``),
  so every descendant orphaned below it - a double-forked, ``setsid`` daemon included - is
  re-parented to it instead of to init.  It then reaps with ``waitpid(-1)`` until the kernel
  says it has no children left (``ECHILD``): at that moment no descendant is alive.  In the
  caller this would re-parent orphans of unrelated threads to it, and ``waitpid(-1)`` would
  steal exit statuses that ``subprocess`` objects elsewhere in the process wait for.
* **Other POSIX (macOS)** - there is no subreaper.  The program starts in a new session (its
  own process group) and the helper waits until that group has no members.  A grandchild
  that calls ``setsid`` leaves the group and is NOT waited for - the documented gap.

The program inherits the helper's stdin/stdout/stderr, so its output reaches the caller's
pipes directly; the helper itself never writes to them (a traceback there would pollute the
captured stderr).  It reports through the status pipe as one JSON object:
``{"exit_code": N}``, ``{"error": "<ExceptionName>", "message": "..."}`` (plus ``"errno": N``
when the failure is an OSError that carries one) or ``{"killed": true}``.  SIGTERM from the
caller means "timeout": kill the whole tree, report, exit.

Stdlib only, and started with ``-I``: the helper must not import pwshpy or anything a user's
``PYTHONPATH`` or working directory could shadow.

Contents:
    * :func:`parse_ppid`, :func:`descendants` - pure helpers for the Linux tree kill.
    * :func:`decode_request`, :func:`encode_status` - the two ends of the wire contract.
    * :func:`main` - the helper's entry point.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import signal
import subprocess  # nosec B404 - starts the caller's argv list, shell=False by construction
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, TextIO, TypeAlias, cast

_PR_SET_CHILD_SUBREAPER = 36  # <linux/prctl.h>
_POLL_SECONDS = 0.05
# Upper bound for the kill loop: SIGKILL cannot be ignored, so rounds converge unless the
# tree forks faster than it is killed (a fork bomb, out of scope).
_KILL_DEADLINE_SECONDS = 10.0
# The helper only ever runs on POSIX; the guards using this narrow the platform for pyright.
_POSIX_ONLY = "the wait_tree helper runs on POSIX only"


@dataclass(frozen=True)
class _Request:
    """The program to run, as the parent sent it on the control pipe.

    The element types stay ``object`` on purpose: the parent forwards the caller's argv, cwd and
    env unchecked, and ``Popen`` is the one place that refuses a non-str element, with the
    ``TypeError`` text the caller has always seen.  Checking them here would replace that text.
    """

    argv: tuple[object, ...]
    cwd: object
    env: dict[object, object] | None


@dataclass(frozen=True)
class _Exited:
    """The program ran; ``exit_code`` is its own (``-N`` for a death by signal ``N``)."""

    exit_code: int

    def wire(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code}


@dataclass(frozen=True)
class _Failed:
    """The program could not be started, or the helper itself failed before it could run it.

    ``errno`` travels beside the text because the parent decides from it whether the start was
    refused for lack of rights (a NativeAccessDeniedError); the exception class name alone would
    leave that to string matching. Only an OSError has one. ``winerror`` is never sent: the
    helper runs on POSIX only, where CPython sets no ``winerror``.
    """

    error: str
    message: str
    errno: int | None = None

    @classmethod
    def of(cls, exc: Exception) -> _Failed:
        code = exc.errno if isinstance(exc, OSError) else None
        return cls(error=type(exc).__name__, message=str(exc), errno=code)

    def wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"error": self.error, "message": self.message}
        if self.errno is not None:
            wire["errno"] = self.errno
        return wire


@dataclass(frozen=True)
class _Killed:
    """The caller's timeout (SIGTERM) arrived; the tree was killed."""

    def wire(self) -> dict[str, Any]:
        return {"killed": True}


_Status: TypeAlias = _Exited | _Failed | _Killed


def decode_request(raw: bytes) -> _Request:
    """Decode the control-pipe request; undecodable argv/env bytes arrive surrogate-escaped.

    Example:
        >>> request = decode_request(b'{"argv": ["a\\\\udcffb"], "cwd": null, "env": {"K": "v"}}')
        >>> request.argv == ("a" + chr(0xDCFF) + "b",), request.cwd, dict(request.env or {})
        (True, None, {'K': 'v'})
    """
    decoded: Any = json.loads(raw)
    # Indexed env, argv, cwd: the order the fields were first read in, so a request missing one
    # reports the same KeyError (and a non-object the same TypeError) as it always did.
    env, argv, cwd = decoded["env"], decoded["argv"], decoded["cwd"]
    # tuple()/dict() are the conversions the helper always applied before Popen, so a
    # non-iterable argv or env still fails with their TypeError, in the same order.
    return _Request(argv=tuple(argv), cwd=cwd, env=dict(env) if env is not None else None)


def encode_status(status: _Status) -> str:
    """Return the status-pipe JSON for ``status``: only the keys its variant carries.

    ``json.dumps`` defaults (``ensure_ascii``, ``", "`` separators) are the wire format the
    parent reads; a surrogate-escaped message stays intact as ``\\udcXX``.

    Example:
        >>> encode_status(_Exited(exit_code=3))
        '{"exit_code": 3}'
        >>> encode_status(_Killed())
        '{"killed": true}'
    """
    return json.dumps(status.wire())


class _TerminateRequestedError(Exception):
    """Raised from the SIGTERM handler: the caller's timeout expired."""


def _on_term(_signum: int, _frame: FrameType | None) -> None:
    raise _TerminateRequestedError


def _on_int(_signum: int, _frame: FrameType | None) -> None:
    # A terminal Ctrl-C reaches the whole foreground group, the program included; the helper
    # keeps reaping so it can still report the program's exit.  A Python handler (not SIG_IGN)
    # because an ignored disposition would be inherited across exec by the program.
    return None


def parse_ppid(stat: str) -> int:
    """Return the parent pid from one ``/proc/<pid>/stat`` line.

    The command name is in parentheses and may itself contain spaces and ``)``, so the
    fields are counted after the LAST ``)``.

    Example:
        >>> parse_ppid("42 (a) b) S 7 42 42 0 -1")
        7
    """
    return int(stat[stat.rindex(")") + 2 :].split()[1])


def descendants(root: int, parents: Mapping[int, int]) -> set[int]:
    """Return every pid whose parent chain reaches ``root`` (``root`` itself excluded).

    Example:
        >>> sorted(descendants(1, {2: 1, 3: 2, 4: 9, 5: 3}))
        [2, 3, 5]
    """
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _proc_parents() -> dict[int, int]:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parents[int(entry.name)] = parse_ppid((entry / "stat").read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue  # exited between the listing and the read
    return parents


def _reap_ready() -> bool:
    """Reap every child that has exited; return ``True`` once no child is left at all."""
    if sys.platform == "win32":
        raise OSError(_POSIX_ONLY)
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            return False


def _signal_quietly(pid: int, signum: int) -> None:
    # OSError: already gone, or not ours to signal.
    with contextlib.suppress(OSError):
        os.kill(pid, signum)


def _kill_tree_linux() -> None:
    if sys.platform == "win32":
        raise OSError(_POSIX_ONLY)
    me = os.getpid()
    end = time.monotonic() + _KILL_DEADLINE_SECONDS
    while time.monotonic() < end:
        for pid in descendants(me, _proc_parents()):
            _signal_quietly(pid, signal.SIGKILL)
        if _reap_ready():
            return
        time.sleep(0.01)


def _wait_tree_linux(child_pid: int) -> int:
    code = 0
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            return code
        if pid == child_pid:
            code = os.waitstatus_to_exitcode(status)


def _wait_group(child: subprocess.Popen[bytes]) -> int:
    if sys.platform == "win32":
        raise OSError(_POSIX_ONLY)
    code = child.wait()
    while True:
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            return code
        except PermissionError:
            pass  # a member we may not signal is still a member
        time.sleep(_POLL_SECONDS)


def _kill_group(child: subprocess.Popen[bytes] | None) -> None:
    if sys.platform == "win32":
        raise OSError(_POSIX_ONLY)
    if child is None:
        return
    with contextlib.suppress(OSError):  # the group is already empty
        os.killpg(child.pid, signal.SIGKILL)
    child.kill()
    child.wait()


def _become_subreaper() -> bool:
    """Make this process a child subreaper on Linux; ``False`` where the OS has none."""
    if not sys.platform.startswith("linux"):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl(PR_SET_CHILD_SUBREAPER) failed: {os.strerror(errno)}")
    return True


def _start(request: _Request, *, new_session: bool) -> subprocess.Popen[bytes]:
    # cast: the types Popen requires.  It checks every element itself and raises its own
    # TypeError for one that is not a str (see _Request), which is the error the caller gets.
    return subprocess.Popen(  # noqa: S603  # nosec B603 - the caller's argv list, no shell
        cast("list[str]", list(request.argv)),
        cwd=cast("str | None", request.cwd),
        env=cast("dict[str, str] | None", request.env),
        start_new_session=new_session,
    )


def _run(request: _Request) -> _Status:
    linux = _become_subreaper()
    child: subprocess.Popen[bytes] | None = None
    try:
        try:
            child = _start(request, new_session=not linux)
        except OSError as exc:
            return _Failed.of(exc)
        code = _wait_tree_linux(child.pid) if linux else _wait_group(child)
        child.returncode = code  # reaped by waitpid(-1); keeps Popen from waiting again
        return _Exited(exit_code=code)
    except _TerminateRequestedError:
        if linux:
            _kill_tree_linux()
        else:
            _kill_group(child)
        return _Killed()


def _report(status_pipe: TextIO, status: _Status) -> None:
    status_pipe.write(encode_status(status))
    status_pipe.flush()


def main(argv: Sequence[str]) -> int:
    """Read the request from the control fd, run it, and report on the status fd.

    Args:
        argv: ``[script, control_fd, status_fd]``.

    Returns:
        0 - the outcome travels on the status pipe, not in the exit code.
    """
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_int)
    with os.fdopen(int(argv[2]), "w", encoding="utf-8") as status:
        try:
            with os.fdopen(int(argv[1]), "rb") as control:
                request = decode_request(control.read())
            _report(status, _run(request))
        except _TerminateRequestedError:
            _report(status, _Killed())  # the timeout hit before the program started
        # Exception, not BaseException: SIGTERM is mapped to _TerminateRequestedError above, and the
        # helper must never print a traceback into the program's (captured) stderr.
        except Exception as exc:
            _report(status, _Failed.of(exc))
    return 0


if __name__ == "__main__":  # pragma: no cover - runs only as the helper process
    sys.exit(main(sys.argv))
