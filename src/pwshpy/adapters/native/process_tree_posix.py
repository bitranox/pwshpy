"""POSIX side of ``exec(wait_tree=True)``: drive the :mod:`._tree_reaper` helper process.

The helper does the tree work in a process of its own (see its module docstring for why it
cannot run in the caller): it starts the program on the caller's pipes, waits for the whole
tree, and reports one JSON status on a private pipe.  This class starts the helper, hands it
the request on a second private pipe (argv, cwd and env as JSON - not as argv/env of the
helper, so the helper's own interpreter always starts with the caller's environment while
the program gets exactly the one it was given), and turns the status into an exit code or a
:class:`~pwshpy.domain.errors.NativeCallError`.

Contents:
    * :class:`PosixReaperTree` - the POSIX :class:`~.process_tree.ProcessTree` strategy.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess  # nosec B404 - starts the bundled helper with an argv list, shell=False
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from ...domain.errors import NativeCallError

_HELPER = Path(__file__).with_name("_tree_reaper.py")
# The helper's own kill loop is bounded at 10 s; past this the helper is SIGKILLed itself.
_HELPER_KILL_GRACE_SECONDS = 15.0


def _close_quietly(fd: int) -> None:
    with contextlib.suppress(OSError):  # already closed
        os.close(fd)


def _interpreter() -> str:
    if not sys.executable:
        raise NativeCallError("wait_tree on POSIX needs a Python interpreter (sys.executable is empty).")
    return sys.executable


def _status_error(args: Sequence[str], status: Mapping[str, Any]) -> NativeCallError:
    message = str(status.get("message", ""))
    if status.get("error") == "FileNotFoundError":
        return NativeCallError(f"Executable not found: {args[0]!r} ({message})")
    return NativeCallError(f"Failed to run {args[0]!r}: {status.get('error')}: {message}")


class PosixReaperTree:
    """Run one program under the helper and wait for its whole process tree (POSIX).

    Example:
        >>> PosixReaperTree().close()  # holds no OS resource until launch()
    """

    def __init__(self) -> None:
        self._popen: subprocess.Popen[bytes] | None = None
        self._status_fd = -1

    def launch(
        self, args: Sequence[str], *, cwd: str | None, env: Mapping[str, str] | None, with_stdin: bool
    ) -> subprocess.Popen[bytes]:
        """Start the helper on fresh stdout/stderr pipes and send it the request."""
        control_r, control_w = os.pipe()
        status_r, status_w = os.pipe()
        self._status_fd = status_r
        try:
            self._popen = subprocess.Popen(  # noqa: S603  # nosec B603 - fixed helper argv, no shell
                [_interpreter(), "-I", str(_HELPER), str(control_r), str(status_w)],
                pass_fds=(control_r, status_w),
                stdin=subprocess.PIPE if with_stdin else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            _close_quietly(control_w)
            raise NativeCallError(f"Failed to start the wait_tree helper: {exc}") from exc
        finally:
            _close_quietly(control_r)
            _close_quietly(status_w)
        self._send_request(control_w, {"argv": list(args), "cwd": cwd, "env": dict(env) if env is not None else None})
        return self._popen

    def _send_request(self, control_w: int, request: Mapping[str, Any]) -> None:
        # ensure_ascii keeps undecodable (surrogate-escaped) argv/env bytes intact through JSON.
        payload = json.dumps(request, ensure_ascii=True).encode("ascii")
        try:
            with os.fdopen(control_w, "wb") as control:
                control.write(payload)
        except OSError as exc:
            raise NativeCallError(f"The wait_tree helper did not accept its request: {exc}") from exc

    def wait(self, deadline: float | None) -> bool:
        """Wait for the helper, which exits once the tree is gone; ``False`` if ``deadline`` passed."""
        popen = self._require_popen()
        remaining = None if deadline is None else max(deadline - time.monotonic(), 0.0)
        try:
            popen.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return False
        return True

    def kill(self) -> None:
        """Ask the helper to kill the whole tree (SIGTERM); SIGKILL the helper if it does not exit."""
        popen = self._require_popen()
        popen.send_signal(signal.SIGTERM)
        try:
            popen.wait(timeout=_HELPER_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            popen.kill()
            popen.wait()

    def exit_code(self, args: Sequence[str]) -> int:
        """Return the program's exit code from the helper's status, or raise its start failure."""
        status = self._read_status()
        if "exit_code" in status:
            return int(status["exit_code"])
        if "error" in status:
            raise _status_error(args, status)
        raise NativeCallError(f"The wait_tree helper for {args[0]!r} ended without a status: {status!r}")

    def _read_status(self) -> dict[str, Any]:
        with os.fdopen(self._status_fd, "rb") as status_pipe:
            self._status_fd = -1
            raw = status_pipe.read()
        try:
            parsed: Any = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}

    def _require_popen(self) -> subprocess.Popen[bytes]:
        if self._popen is None:
            raise NativeCallError("wait_tree: the helper was never launched.")
        return self._popen

    def close(self) -> None:
        """Release the status pipe if it was not read (an aborted run)."""
        if self._status_fd >= 0:
            _close_quietly(self._status_fd)
            self._status_fd = -1


__all__ = ["PosixReaperTree"]
