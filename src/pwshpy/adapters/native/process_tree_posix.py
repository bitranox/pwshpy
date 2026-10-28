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

from pydantic import BaseModel, ConfigDict, ValidationError

from ...domain.errors import NativeCallError
from .marshal import native_error

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


class _HelperRequest(BaseModel):
    """The control-pipe request: the program to run, exactly as the caller gave it."""

    model_config = ConfigDict(frozen=True)

    argv: list[str]
    cwd: str | None
    env: dict[str, str] | None

    def wire(self) -> bytes:
        """Return the request as the helper reads it.

        ``json.dumps`` rather than ``model_dump_json``: ``ensure_ascii`` writes an undecodable
        (surrogate-escaped) argv/env byte as ``\\udcXX``, which survives the pipe, while
        pydantic's JSON encoder refuses a lone surrogate outright.

        Example:
            >>> _HelperRequest.model_construct(argv=["a"], cwd=None, env=None).wire()
            b'{"argv": ["a"], "cwd": null, "env": null}'
        """
        # warnings=False: the model is built with model_construct, so a non-str argv/env element
        # reaches json.dumps unchanged and without a serializer warning: an int is written as a
        # JSON number (the helper's Popen then refuses it), a bytes or other object raises
        # json.dumps's own TypeError here.
        return json.dumps(self.model_dump(warnings=False), ensure_ascii=True).encode("ascii")


class _HelperStatus(BaseModel):
    """The helper's one status report; every field is absent from the variants that do not carry it.

    The helper writes ``{"exit_code": N}``, ``{"error": ..., "message": ...}`` or
    ``{"killed": true}``; a missing, undecodable or malformed report parses as the empty status.

    Example:
        >>> _HelperStatus.from_wire(b'{"exit_code": 3}').exit_code
        3
        >>> _HelperStatus.from_wire(b"not json") == _HelperStatus()
        True
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    exit_code: int | None = None
    error: str | None = None
    message: str = ""
    killed: bool = False

    @classmethod
    def from_wire(cls, raw: bytes) -> _HelperStatus:
        """Parse the status-pipe bytes once; anything unreadable is the empty status."""
        if not raw:
            return cls()
        # json.loads, not model_validate_json: pydantic's JSON parser rejects the lone-surrogate
        # escape a non-UTF-8 program name puts into the message.
        try:
            return cls.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            return cls()


def _status_error(args: Sequence[str], status: _HelperStatus) -> NativeCallError:
    if status.error == "FileNotFoundError":
        return NativeCallError(f"Executable not found: {args[0]!r} ({status.message})")
    return NativeCallError(f"Failed to run {args[0]!r}: {status.error}: {status.message}")


def _no_status_error(args: Sequence[str], status: _HelperStatus) -> NativeCallError:
    # A killed report carries no exit code: exit_code() is only asked after a wait that ended on
    # its own, so it means the tree was killed under us.  The text quotes the report as the
    # helper wrote it ({'killed': True}, or {} when nothing readable arrived).
    received = {"killed": True} if status.killed else {}
    return NativeCallError(f"The wait_tree helper for {args[0]!r} ended without a status: {received!r}")


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
            raise native_error(f"Failed to start the wait_tree helper: {exc}", exc) from exc
        finally:
            _close_quietly(control_r)
            _close_quietly(status_w)
        # model_construct: args/env are the caller's already-typed values, and validating them here
        # would turn the TypeError a non-str argv/env element has always raised (json.dumps here,
        # or Popen in the helper) into a ValidationError.
        request = _HelperRequest.model_construct(argv=list(args), cwd=cwd, env=dict(env) if env is not None else None)
        self._send_request(control_w, request)
        return self._popen

    def _send_request(self, control_w: int, request: _HelperRequest) -> None:
        payload = request.wire()
        try:
            with os.fdopen(control_w, "wb") as control:
                control.write(payload)
        except OSError as exc:
            raise native_error(f"The wait_tree helper did not accept its request: {exc}", exc) from exc

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
        if status.exit_code is not None:
            return status.exit_code
        if status.error is not None:
            raise _status_error(args, status)
        raise _no_status_error(args, status)

    def _read_status(self) -> _HelperStatus:
        with os.fdopen(self._status_fd, "rb") as status_pipe:
            self._status_fd = -1
            raw = status_pipe.read()
        return _HelperStatus.from_wire(raw)

    def _require_popen(self) -> subprocess.Popen[bytes]:
        if self._popen is None:
            raise NativeCallError("wait_tree: the helper was never launched.")
        return self._popen

    def close(self) -> None:
        """Kill a tree whose wait was aborted, and release the status pipe if it was not read.

        A wait aborted by an exception (Ctrl-C included) must not leave the tree running:
        ``subprocess.run`` kills its child on any exception, and the Windows job does the
        same through ``KILL_ON_JOB_CLOSE``.
        """
        if self._popen is not None and self._popen.poll() is None:
            self.kill()
        if self._status_fd >= 0:
            _close_quietly(self._status_fd)
            self._status_fd = -1


__all__ = ["PosixReaperTree"]
