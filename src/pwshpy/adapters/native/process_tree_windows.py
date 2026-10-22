"""Windows side of ``exec(wait_tree=True)``: a Win32 job object around the started program.

Every process a job member creates joins the job too, ``DETACHED_PROCESS`` and
``CREATE_NEW_PROCESS_GROUP`` children included, so "the tree is gone" is exactly
"the job has no active process" (``JobObjectBasicAccountingInformation.ActiveProcesses == 0``).
That is the documented mechanism behind PowerShell's ``Start-Process -Wait``.

The race this module closes: the program must be in the job BEFORE it can start anything,
or a fast child escapes.  ``subprocess`` has no "assign to a job" hook, so the program is
created with ``CREATE_SUSPENDED`` (``subprocess`` passes ``creationflags`` straight to
``CreateProcess``), assigned to the job, and only then resumed.  ``subprocess`` closes the
primary-thread handle ``CreateProcess`` returned, so the suspended thread is found by id
(psutil) and resumed through a fresh ``OpenThread`` handle.  Nested jobs are allowed since
Windows 8, so a caller that already runs inside a job (a CI runner, an SSH session) is fine.

Job limits:

* ``KILL_ON_JOB_CLOSE`` - closing the job handle kills whatever is still in it, so an aborted
  wait (an exception, Ctrl-C in the caller) leaves nothing running.
* ``BREAKAWAY_OK`` - a program that explicitly asks for ``CREATE_BREAKAWAY_FROM_JOB`` still
  gets it; without the flag its ``CreateProcess`` would fail and the program would break.
  Such a process is outside the tree and is not waited for (the documented gap).

``pywin32`` and ``psutil`` are imported lazily, so the module imports on every OS.

Contents:
    * :class:`WindowsJobTree` - the Windows :class:`~.process_tree.ProcessTree` strategy.
"""

from __future__ import annotations

import importlib
import subprocess  # nosec B404 - starts the caller's argv list, shell=False by construction
import time
from collections.abc import Mapping, Sequence
from typing import Any

from ...domain.errors import NativeCallError, PlatformUnsupportedError

_CREATE_SUSPENDED = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_POLL_SECONDS = 0.05
_INHERIT_HANDLE = False  # the bInheritHandle argument; pywin32 takes it positionally only
_TERMINATED_EXIT_CODE = 1


def _load(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(f"wait_tree on Windows requires {name} (pywin32/psutil).") from exc


def _native(what: str, call: Any, *args: Any) -> Any:
    """Run one fallible native call, mapping any failure into :class:`NativeCallError`."""
    try:
        return call(*args)
    except Exception as exc:
        raise NativeCallError(f"wait_tree: {what} failed: {exc}") from exc


def _create_job(win32job: Any) -> Any:
    job = _native("CreateJobObject", win32job.CreateJobObject, None, "")
    try:
        info = _native(
            "QueryInformationJobObject",
            win32job.QueryInformationJobObject,
            job,
            win32job.JobObjectExtendedLimitInformation,
        )
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        _native(
            "SetInformationJobObject",
            win32job.SetInformationJobObject,
            job,
            win32job.JobObjectExtendedLimitInformation,
            info,
        )
    except NativeCallError:
        job.Close()
        raise
    return job


class WindowsJobTree:
    """Run one program inside a fresh job object and wait until the job is empty (Windows).

    Example:
        >>> callable(WindowsJobTree.launch)
        True
    """

    def __init__(self) -> None:
        self._win32job = _load("win32job")
        self._win32api = _load("win32api")
        self._job: Any = _create_job(self._win32job)
        self._popen: subprocess.Popen[bytes] | None = None

    def launch(
        self, args: Sequence[str], *, cwd: str | None, env: Mapping[str, str] | None, with_stdin: bool
    ) -> subprocess.Popen[bytes]:
        """Create the program suspended, put it in the job, then let it run."""
        popen = subprocess.Popen(  # noqa: S603  # nosec B603 - argv list, shell=False by construction
            list(args),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE if with_stdin else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_SUSPENDED,
        )
        self._popen = popen
        try:
            self._assign(popen.pid)
            self._resume(popen.pid)
        except NativeCallError:
            popen.kill()  # never leave a suspended or half-assigned process behind
            popen.wait()
            raise
        return popen

    def _assign(self, pid: int) -> None:
        access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE
        handle = _native("OpenProcess", self._win32api.OpenProcess, access, _INHERIT_HANDLE, pid)
        try:
            _native("AssignProcessToJobObject", self._win32job.AssignProcessToJobObject, self._job, handle)
        finally:
            handle.Close()

    def _resume(self, pid: int) -> None:
        psutil = _load("psutil")
        win32process = _load("win32process")
        threads = _native("thread enumeration", lambda: psutil.Process(pid).threads())
        if not threads:
            # Created suspended, the program runs only once its thread is resumed: with none found it
            # would never run and the job would never empty, so wait() would block for ever. launch()
            # kills and reaps the process on this error.
            raise NativeCallError(f"wait_tree: no thread of the suspended process {pid} was found to resume")
        for thread in threads:
            handle = _native(
                "OpenThread", self._win32api.OpenThread, _THREAD_SUSPEND_RESUME, _INHERIT_HANDLE, thread.id
            )
            try:
                _native("ResumeThread", win32process.ResumeThread, handle)
            finally:
                handle.Close()

    def _active_processes(self) -> int:
        info = _native(
            "QueryInformationJobObject",
            self._win32job.QueryInformationJobObject,
            self._job,
            self._win32job.JobObjectBasicAccountingInformation,
        )
        return int(info["ActiveProcesses"])

    def wait(self, deadline: float | None) -> bool:
        """Poll until the job has no active process; ``False`` if ``deadline`` passed first."""
        while self._active_processes() > 0:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SECONDS)
        return True

    def kill(self) -> None:
        """Terminate every process in the job, then reap the program itself."""
        _native("TerminateJobObject", self._win32job.TerminateJobObject, self._job, _TERMINATED_EXIT_CODE)
        if self._popen is not None:
            self._popen.wait()

    def exit_code(self, args: Sequence[str]) -> int:
        """Return the program's own exit code (the tree is already gone)."""
        if self._popen is None:
            raise NativeCallError(f"wait_tree: {args[0]!r} was never launched.")
        return self._popen.wait()

    def close(self) -> None:
        """Close the job handle; KILL_ON_JOB_CLOSE ends anything an aborted wait left behind."""
        self._job.Close()


__all__ = ["WindowsJobTree"]
