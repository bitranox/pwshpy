"""native external-process runner - ``subprocess`` done right, as a typed record.

Calling an external ``.exe`` from PowerShell is a minefield: argument quoting with
spaces/special characters gets re-parsed (hence the ``--%`` stop-parsing hack),
stderr text can be misread as an error, and ``$LASTEXITCODE`` is not even set when
the native command runs in a pipeline.  :func:`run_process` sidesteps all of it: it
takes an **argv list** (no shell, no quoting), always captures a real ``exit_code``,
and returns ``stderr`` as plain data in a :class:`~pwshpy.domain.records.ProcessResult`.

Portable (stdlib ``subprocess``); works on every OS.  ``wait_tree=True`` waits for the
whole process tree (:mod:`~pwshpy.adapters.native.process_tree`).

Contents:
    * :func:`run_process` - run an external program and return a typed ProcessResult.
"""

from __future__ import annotations

import subprocess  # nosec B404 - this module's purpose is a controlled, shell-free process runner
import time
from collections.abc import Mapping, Sequence

from ...domain.errors import NativeCallError, OperationTimeoutError
from ...domain.records import ProcessResult
from .marshal import native_error
from .process_tree import run_process_tree


def run_process(  # noqa: PLR0913 - mirrors the ProcessRunner port: independent, all-optional exec parameters
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    wait_tree: bool = False,
) -> ProcessResult:
    """Run an external program from an argv list and return a typed result.

    Args:
        argv: The program and its arguments as a list - passed verbatim, never
            through a shell, so there is no quoting/escaping to get wrong.
        cwd: Working directory for the child (defaults to the current directory).
        timeout: Optional seconds before the child is killed and the call raises.
        env: Optional full environment mapping for the child (replaces, not merges).
        input_text: Optional text piped to the child's stdin.
        wait_tree: When ``True``, return only after the program AND every process it
            started have exited (like ``Start-Process -Wait``), and let ``timeout``
            cover that whole wait and kill the whole tree.  ``False`` (the default)
            waits for the started program only.  Coverage per OS is in
            :mod:`~pwshpy.adapters.native.process_tree`.

    Returns:
        A :class:`~pwshpy.domain.records.ProcessResult` with argv, exit_code,
        captured stdout/stderr (UTF-8 text), and wall-clock ``duration_s``.

    Raises:
        OperationTimeoutError: If ``timeout`` elapses (the child - with ``wait_tree``
            its whole process tree - is killed; a
            :class:`~pwshpy.domain.errors.NativeCallError` subclass).
        NativeCallError: If the executable is not found or cannot be started.

    Example:
        >>> import sys
        >>> run_process([sys.executable, "-c", "print('hi')"]).stdout.strip()
        'hi'
    """
    args = list(argv)
    if not args:
        raise NativeCallError("run_process requires a non-empty argv list.")
    if wait_tree:
        return run_process_tree(args, cwd=cwd, timeout=timeout, env=env, input_text=input_text)
    started = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603 - argv list, shell=False by construction
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=dict(env) if env is not None else None,
            input=input_text,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise native_error(f"Executable not found: {args[0]!r} ({exc})", exc) from exc
    except OSError as exc:
        raise native_error(f"Failed to run {args[0]!r}: {exc}", exc) from exc
    except subprocess.TimeoutExpired as exc:
        # exc.timeout is the value subprocess enforced, so the attribute cannot drift from the message.
        raise OperationTimeoutError(
            f"Command {args!r} exceeded its {timeout}s timeout and was killed.", timeout=float(exc.timeout)
        ) from exc
    duration = time.perf_counter() - started
    return ProcessResult(
        argv=args,
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_s=duration,
    )


__all__ = ["run_process"]
