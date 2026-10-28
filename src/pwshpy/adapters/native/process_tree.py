"""``exec(wait_tree=True)``: run a program and wait until every process it started has exited.

PowerShell's ``Start-Process -Wait`` waits for the whole process tree; ``subprocess.run``
waits for the started program only, so an installer that hands its work to a child
(``setup.exe`` -> ``SetupHost``) reads as finished while the work still runs.  This module
is the tree-waiting runner behind :func:`~pwshpy.adapters.native.process_exec.run_process`
with ``wait_tree=True``.  The OS mechanism is a :class:`ProcessTree` strategy:

* Windows - :class:`~.process_tree_windows.WindowsJobTree`: a job object; covers every
  descendant except one that explicitly breaks away from the job.
* Linux - :class:`~.process_tree_posix.PosixReaperTree` with a child-subreaper helper;
  covers every descendant, double-forked ``setsid`` daemons included.
* macOS (other POSIX) - the same helper waiting for the program's process group; covers
  descendants that stay in the group, NOT one that calls ``setsid``/daemonizes.

Work handed to a service manager (systemd, launchd, the Windows service control manager,
Task Scheduler, an out-of-process COM server) never was a descendant, so no strategy waits
for it.

Output: stdout/stderr are captured exactly as on the default path (UTF-8, invalid bytes
replaced, universal newlines), including whatever descendants write to the inherited
pipes until the tree is gone.  A process outside the tree (the gaps above) can still hold
the pipes open; after the tree has exited the readers get :data:`_DRAIN_GRACE_SECONDS`
to finish, then the result is returned with the output read so far and the readers are
abandoned (daemon threads that end when that process closes the pipes).

Contents:
    * :class:`ProcessTree` - the per-OS strategy protocol.
    * :func:`run_process_tree` - the tree-waiting runner.
    * :func:`decode_output`, :func:`encode_input` - the text conversions ``subprocess`` uses.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - the strategies start argv lists, shell=False by construction
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from typing import IO, Protocol

from ...domain.errors import NativeCallError, OperationTimeoutError
from ...domain.records import ProcessResult
from .marshal import native_error

# How long the output readers may take after the tree has exited.  With nothing outside
# the tree holding the pipes, EOF arrives as soon as the pipes are drained; the grace only
# bounds the wait when a process that escaped the tree still holds them.
_DRAIN_GRACE_SECONDS = 2.0
_READ_CHUNK = 65536


class ProcessTree(Protocol):
    """One OS mechanism for "start a program, then wait for everything it started"."""

    def launch(
        self, args: Sequence[str], *, cwd: str | None, env: Mapping[str, str] | None, with_stdin: bool
    ) -> subprocess.Popen[bytes]:
        """Start the program on fresh stdout/stderr pipes (and a stdin pipe if asked)."""
        ...

    def wait(self, deadline: float | None) -> bool:
        """Block until the whole tree has exited; ``False`` if ``deadline`` (monotonic) passed."""
        ...

    def kill(self) -> None:
        """Kill every process still in the tree."""
        ...

    def exit_code(self, args: Sequence[str]) -> int:
        """Return the started program's exit code once the tree is gone."""
        ...

    def close(self) -> None:
        """Release the strategy's OS resources."""
        ...


def decode_output(data: bytes) -> str:
    """Decode captured output the way ``subprocess`` text mode does (UTF-8, universal newlines).

    Example:
        >>> decode_output(b"a\\r\\nb\\rc\\xff")
        'a\\nb\\nc\\ufffd'
    """
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def encode_input(text: str) -> bytes:
    """Encode stdin text the way ``subprocess`` text mode does (``\\n`` becomes ``os.linesep``).

    Example:
        >>> encode_input("x") == b"x"
        True
    """
    return text.replace("\n", os.linesep).encode("utf-8", errors="replace")


class _Reader:
    """Read one pipe to EOF on a daemon thread, keeping every chunk read so far."""

    def __init__(self, stream: IO[bytes] | None) -> None:
        self._chunks: list[bytes] = []
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream: IO[bytes] | None) -> None:
        if stream is None:
            return
        read = getattr(stream, "read1", stream.read)
        try:
            while chunk := read(_READ_CHUNK):
                self._chunks.append(chunk)
            stream.close()
        except (OSError, ValueError):
            return  # the pipe broke or was closed under us; keep what was read

    def text(self, drain_deadline: float) -> str:
        """Return the output read so far, after letting the reader reach EOF until ``drain_deadline``."""
        self._thread.join(max(drain_deadline - time.monotonic(), 0.0))
        return decode_output(b"".join(list(self._chunks)))


def _feed(stream: IO[bytes] | None, data: bytes) -> None:
    if stream is None:
        return
    try:
        stream.write(data)
        stream.close()
    except OSError:
        pass  # the program exited or closed stdin without reading it all, as subprocess tolerates


def _start_feeder(stream: IO[bytes] | None, input_text: str | None) -> None:
    if input_text is None:
        return
    threading.Thread(target=_feed, args=(stream, encode_input(input_text)), daemon=True).start()


def _new_tree() -> ProcessTree:
    if sys.platform == "win32":
        from .process_tree_windows import WindowsJobTree  # pywin32 exists only on Windows

        return WindowsJobTree()
    from .process_tree_posix import PosixReaperTree  # keeps the two OS halves apart

    return PosixReaperTree()


def _launch(
    tree: ProcessTree, args: Sequence[str], *, cwd: str | None, env: Mapping[str, str] | None, with_stdin: bool
) -> subprocess.Popen[bytes]:
    try:
        return tree.launch(args, cwd=cwd, env=env, with_stdin=with_stdin)
    except NativeCallError:
        # The strategy's own typed failure (the POSIX helper, a Windows job call) already says what
        # failed; a denied one is a PermissionError too and would otherwise be re-wrapped below.
        raise
    except FileNotFoundError as exc:
        raise native_error(f"Executable not found: {args[0]!r} ({exc})", exc) from exc
    except OSError as exc:
        raise native_error(f"Failed to run {args[0]!r}: {exc}", exc) from exc


def run_process_tree(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> ProcessResult:
    """Run ``args`` and return once it AND every process it started have exited.

    Args:
        args: Non-empty argv list, passed verbatim (no shell).
        cwd: Working directory for the program.
        timeout: Seconds for the whole wait, tree included; on expiry the tree is killed.
        env: Full environment for the program (replaces, not merges).
        input_text: Text piped to the program's stdin.

    Returns:
        The program's :class:`~pwshpy.domain.records.ProcessResult`; ``duration_s`` covers
        the whole tree.

    Raises:
        OperationTimeoutError: ``timeout`` elapsed; every process in the tree was killed.
        NativeCallError: The program could not be started, or the OS mechanism failed.

    Example:
        >>> import sys
        >>> run_process_tree([sys.executable, "-c", "print('hi')"]).stdout.strip()
        'hi'
    """
    started = time.perf_counter()
    deadline = None if timeout is None else time.monotonic() + timeout
    tree = _new_tree()
    try:
        popen = _launch(tree, args, cwd=cwd, env=env, with_stdin=input_text is not None)
        _start_feeder(popen.stdin, input_text)
        stdout, stderr = _Reader(popen.stdout), _Reader(popen.stderr)
        if not tree.wait(deadline):
            tree.kill()
            raise OperationTimeoutError(
                f"Command {list(args)!r} exceeded its {timeout}s timeout; its process tree was killed.",
                timeout=float(timeout if timeout is not None else 0.0),
            )
        exit_code = tree.exit_code(args)
        drain_deadline = time.monotonic() + _DRAIN_GRACE_SECONDS
        return ProcessResult(
            argv=list(args),
            exit_code=exit_code,
            stdout=stdout.text(drain_deadline),
            stderr=stderr.text(drain_deadline),
            duration_s=time.perf_counter() - started,
        )
    finally:
        tree.close()


__all__ = ["ProcessTree", "decode_output", "encode_input", "run_process_tree"]
