r"""Windows shortcuts (``.lnk``) over the shell's ``IShellLinkW`` and ``IPersistFile`` (pywin32).

``create`` writes a shortcut from a validated :class:`~pwshpy.domain.shortcuts.ShortcutSpec` and
returns what Windows then reads back; ``read`` returns a shortcut's fields as a
:class:`~pwshpy.domain.records.ShortcutInfo`. Measured on Windows (pywin32 312), and binding here:

- ``shell.IID_IShellLink`` is the wide (``W``) interface, so non-ASCII text round-trips in every
  field and in the file name; WScript.Shell, pwsh's route, turns CJK into ``?``.
- Every COM call runs on a fresh multithreaded-apartment worker thread
  (:func:`~pwshpy.adapters.native.com_thread.run_in_com_thread`), never on the caller's thread,
  and every COM object is a local of the work so it is released before ``CoUninitialize``.
- ``IPersistFile.Save`` overwrites an existing file without asking, so ``force`` is decided before
  any COM call. A Save after a failed ``SetPath`` still writes a link with no target, so the first
  failing call ends the work.
- ``IPersistFile.Load`` accepts a zero-byte file and reads every field empty, so ``read`` refuses
  a missing, empty or non-regular file before any COM call.
- pywin32's default read buffers cut values silently (``GetArguments`` at 1023 characters,
  ``GetIconLocation`` at 259 of 260), so every getter is given an explicit buffer.
- "Run as administrator" is the link flag ``SLDF_RUNAS_USER`` (0x2000), set through
  ``IShellLinkDataList.SetFlags`` before ``Save`` and stored in the header's LinkFlags. It is
  OR-ed into what ``GetFlags`` returns, so any flag the link object already holds survives (a
  fresh one reads 0; ``Save`` adds the rest).
- ``target`` is read with ``GetPath`` flags 0: environment references come back expanded, the
  value WScript's ``TargetPath`` reports. Arguments, working directory and icon come back raw.

pywin32 is imported lazily through an injectable loader, so importing this module is safe on
every OS; off Windows each call raises :class:`PlatformUnsupportedError`.

Contents:
    * :class:`NativeShortcutController` - create and read Windows shortcuts.
"""

from __future__ import annotations

import importlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from ...domain.errors import NativeCallError, PlatformUnsupportedError
from ...domain.records import ShortcutInfo
from ...domain.shortcuts import ShortcutSpec
from .com_thread import run_in_com_thread
from .marshal import com_hresult, is_com_error, native_error

__all__ = ["NativeShortcutController"]

_T = TypeVar("_T")

#: ``IPersistFile::Save`` fRemember: keep the object's current file association unchanged.
_SAVE_REMEMBER = 0
#: ``STGM_READ`` (0); pythoncom does not export it.
_STGM_READ = 0
#: ``GetPath`` flags 0: the expanded path, as WScript's ``TargetPath`` reports it.
_GET_PATH_FLAGS = 0
#: ``SLDF_RUNAS_USER`` (shlobj_core.h): start the target elevated, Explorer's "Run as administrator".
_SLDF_RUNAS_USER = 0x2000
#: The longest argument string a link stores is 32767 characters, plus the terminating NUL.
_ARGUMENTS_BUFFER = 32768
#: Path, working directory, description and icon hold at most 260 characters plus the NUL; the
#: larger buffer also reads a link another tool wrote with a longer value whole.
_FIELD_BUFFER = 1024


class _Progress:
    """The COM call the work is about to make, so a failure can name it."""

    def __init__(self) -> None:
        self.step = "CoInitializeEx"


class NativeShortcutController:
    """Create and read Windows shortcuts (``.lnk``) through ``IShellLinkW``.

    Args:
        load: Module loader for ``pythoncom`` and ``win32com.shell.shell``; ``None`` imports the
            real pywin32 modules (a test passes doubles).

    Example:
        >>> controller = NativeShortcutController()
        >>> callable(controller.create) and callable(controller.read)
        True
    """

    def __init__(self, *, load: Callable[[str], Any] | None = None) -> None:
        """Keep the loader; nothing is imported until a call needs it."""
        self._load = load if load is not None else _load

    def create(self, spec: ShortcutSpec, *, force: bool) -> ShortcutInfo:
        """Write the shortcut ``spec`` describes and return what Windows stored.

        Unset optional fields are not written (they read back ``""``); an ``icon_index`` is
        written even without an ``icon`` file, so a value the caller set is never dropped. No
        directory is created: saving into a missing one fails.

        Args:
            spec: The validated shortcut; its ``path`` is absolute and at most 259 characters.
            force: Overwrite an existing file at ``spec.path``; False refuses it before any COM
                call.

        Returns:
            The shortcut as :meth:`read` reads it back from ``spec.path``.

        Raises:
            NativeCallError: ``spec.path`` exists and ``force`` is False; or a COM call failed
                (the message names the call, the unsigned HRESULT and the path; e.g.
                ``0x80070003`` for a missing directory); or the read-back failed, in which case
                the file at ``spec.path`` may already be written.
            PlatformUnsupportedError: pywin32 is not installed (not Windows).
        """
        if os.path.lexists(spec.path) and not force:
            raise NativeCallError(f"the shortcut {spec.path} already exists; pass force=True to overwrite it")
        pythoncom = self._load("pythoncom")
        shell = self._load("win32com.shell.shell")
        progress = _Progress()
        self._in_com_thread(
            lambda: _save(pythoncom, shell, spec, progress), verb="writing", path=spec.path, progress=progress
        )
        return self.read(spec.path)

    def read(self, path: str) -> ShortcutInfo:
        """Return the fields of the shortcut at ``path`` as Windows reads them.

        Args:
            path: The ``.lnk`` file, absolute.

        Returns:
            The shortcut; an unset field is ``""`` (an unset icon index ``0``).

        Raises:
            NativeCallError: ``path`` is missing, not a regular file, empty (zero bytes) or not
                readable; or a COM call failed (the message names the call, the unsigned HRESULT
                and the path; e.g. ``0x80004005`` for a file that is not a shortcut).
            PlatformUnsupportedError: pywin32 is not installed (not Windows).
        """
        _require_shortcut_file(path)
        pythoncom = self._load("pythoncom")
        shell = self._load("win32com.shell.shell")
        progress = _Progress()
        return self._in_com_thread(
            lambda: _read(pythoncom, shell, path, progress), verb="reading", path=path, progress=progress
        )

    def _in_com_thread(self, work: Callable[[], _T], *, verb: str, path: str, progress: _Progress) -> _T:
        """Run ``work`` on a fresh COM thread; a COM error becomes a :class:`NativeCallError`."""
        try:
            return run_in_com_thread(work, load=self._load)
        except Exception as exc:  # a pywin32 COM error; anything else is not a COM answer
            if not is_com_error(exc):
                raise
            code = com_hresult(exc)
            shown = "without an HRESULT" if code is None else f"with HRESULT 0x{code:08X}"
            raise native_error(f"{verb} the shortcut {path} failed at {progress.step} {shown}", exc) from exc


def _require_shortcut_file(path: str) -> None:
    """Refuse a missing, non-regular, empty or unreadable ``path`` before any COM call."""
    try:
        info = Path(path).stat()
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise native_error(f"the shortcut {path} does not exist", exc) from exc
    except OSError as exc:
        raise native_error(f"cannot read the shortcut {path}: {exc}", exc) from exc
    if not stat.S_ISREG(info.st_mode):
        raise NativeCallError(f"the shortcut {path} is not a file")
    if info.st_size == 0:
        raise NativeCallError(f"the shortcut {path} is empty (zero bytes), so it is not a shortcut")


def _new_link(pythoncom: Any, shell: Any, progress: _Progress) -> Any:
    """Create a ShellLink object asking for ``IShellLinkW``."""
    progress.step = "CoCreateInstance(ShellLink)"
    return pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)


def _persist(pythoncom: Any, link: Any, progress: _Progress) -> Any:
    """Return the link's ``IPersistFile``."""
    progress.step = "IShellLink.QueryInterface(IPersistFile)"
    return link.QueryInterface(pythoncom.IID_IPersistFile)


def _data_list(shell: Any, link: Any, progress: _Progress) -> Any:
    """Return the link's ``IShellLinkDataList`` (it holds the link flags)."""
    progress.step = "IShellLink.QueryInterface(IShellLinkDataList)"
    return link.QueryInterface(shell.IID_IShellLinkDataList)


def _save(pythoncom: Any, shell: Any, spec: ShortcutSpec, progress: _Progress) -> None:
    """Worker side: set every field ``spec`` holds and save; COM objects stay local here."""
    link = _new_link(pythoncom, shell, progress)
    progress.step = "IShellLink.SetPath"
    link.SetPath(spec.target)
    arguments = spec.joined_arguments
    if arguments:
        progress.step = "IShellLink.SetArguments"
        link.SetArguments(arguments)
    if spec.working_directory is not None:
        progress.step = "IShellLink.SetWorkingDirectory"
        link.SetWorkingDirectory(spec.working_directory)
    if spec.icon is not None or spec.icon_index:
        progress.step = "IShellLink.SetIconLocation"
        link.SetIconLocation(spec.icon or "", spec.icon_index)
    if spec.description is not None:
        progress.step = "IShellLink.SetDescription"
        link.SetDescription(spec.description)
    if spec.run_as_administrator:
        data = _data_list(shell, link, progress)
        progress.step = "IShellLinkDataList.GetFlags"
        flags = int(data.GetFlags())
        progress.step = "IShellLinkDataList.SetFlags"
        data.SetFlags(flags | _SLDF_RUNAS_USER)
    persist = _persist(pythoncom, link, progress)
    progress.step = "IPersistFile.Save"
    persist.Save(spec.path, _SAVE_REMEMBER)


def _read(pythoncom: Any, shell: Any, path: str, progress: _Progress) -> ShortcutInfo:
    """Worker side: load ``path`` and read every field; COM objects stay local here."""
    link = _new_link(pythoncom, shell, progress)
    persist = _persist(pythoncom, link, progress)
    progress.step = "IPersistFile.Load"
    persist.Load(path, _STGM_READ)
    progress.step = "IShellLink.GetPath"
    target, _find_data = link.GetPath(_GET_PATH_FLAGS, _FIELD_BUFFER)
    progress.step = "IShellLink.GetArguments"
    arguments = link.GetArguments(_ARGUMENTS_BUFFER)
    progress.step = "IShellLink.GetWorkingDirectory"
    working_directory = link.GetWorkingDirectory(_FIELD_BUFFER)
    progress.step = "IShellLink.GetIconLocation"
    icon, icon_index = link.GetIconLocation(_FIELD_BUFFER)
    progress.step = "IShellLink.GetDescription"
    description = link.GetDescription(_FIELD_BUFFER)
    data = _data_list(shell, link, progress)
    progress.step = "IShellLinkDataList.GetFlags"
    flags = int(data.GetFlags())
    # Trusted native values (str and int from pywin32): built without validation, like every
    # native record on the hot path.
    return ShortcutInfo.model_construct(
        path=path,
        target=str(target),
        arguments=str(arguments),
        working_directory=str(working_directory),
        icon=str(icon),
        icon_index=int(icon_index),
        description=str(description),
        run_as_administrator=bool(flags & _SLDF_RUNAS_USER),
    )


def _load(module: str) -> Any:
    """Import a pywin32 module lazily, so importing this module stays safe on every OS."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - only on a portable (non-Windows) install
        raise PlatformUnsupportedError(
            f"Windows shortcuts need pywin32 (Windows only); {module} is unavailable."
        ) from exc
