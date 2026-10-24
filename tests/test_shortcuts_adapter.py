"""Windows shortcut adapter: IShellLinkW + IPersistFile through the ``load=`` seam.

``pythoncom`` and ``win32com.shell.shell`` are doubles: a fake shell link that records every
call (with the thread it ran on) and a fake ``IPersistFile`` whose ``Save`` really writes the
link's fields to the file and whose ``Load`` reads them back, so a create followed by a read is a
real round trip through the filesystem. The fake ``Get*`` methods truncate to the buffer they are
given, as pywin32 does, so a default-sized buffer shows up as a lost value.

A spec's ``.lnk`` path must be an absolute Windows path. Off Windows the tests chdir into
``tmp_path`` and use ``C:\\<name>.lnk``, which POSIX reads as one relative file name there; on
Windows they use a real path under ``tmp_path``. The adapter and the double see the same string
either way. The ``os_windows`` cases at the end call the real pywin32.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pwshpy.adapters.native.shortcuts import NativeShortcutController
from pwshpy.domain.errors import NativeCallError, PlatformUnsupportedError
from pwshpy.domain.records import ShortcutInfo
from pwshpy.domain.shortcuts import ShortcutSpec

_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
_IID_SHELL_LINK = "{000214F9-0000-0000-C000-000000000046}"
_IID_PERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
_CLSCTX_INPROC_SERVER = 1
_COINIT_MULTITHREADED = 0
_INSUFFICIENT_BUFFER = 0x8007007A
_PATH_NOT_FOUND = 0x80070003
_TARGET = "C:\\Tools\\tool.exe"


class com_error(Exception):  # noqa: N801, N818 - pywin32's class name is what the adapter recognises
    """A stand-in for ``pywintypes.com_error``: the HRESULT arrives signed, on ``.hresult``."""

    def __init__(self, code: int) -> None:
        signed = code - 0x1_0000_0000 if code & 0x8000_0000 else code
        super().__init__(signed, "localized text the adapter must not rely on", None, None)
        self.hresult = signed


class _Com:
    """Shared state of one test's doubles: the call log and the failures to inject."""

    def __init__(self, fail_at: dict[str, BaseException] | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...], int]] = []
        self.fail_at = fail_at or {}

    def record(self, name: str, *args: object) -> None:
        self.calls.append((name, args, threading.get_ident()))
        failure = self.fail_at.get(name)
        if failure is not None:
            raise failure

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def args_of(self, name: str) -> list[tuple[object, ...]]:
        return [args for call, args, _ in self.calls if call == name]


class _FakeLink:
    """An ``IShellLinkW`` double holding the fields a real link object holds."""

    def __init__(self, com: _Com) -> None:
        self.com = com
        self.fields: dict[str, Any] = {
            "target": "",
            "arguments": "",
            "working_directory": "",
            "icon": "",
            "icon_index": 0,
            "description": "",
        }

    def __del__(self) -> None:
        self.com.calls.append(("released link", (), threading.get_ident()))

    def SetPath(self, value: str) -> None:  # noqa: N802 - the pywin32 name
        self.com.record("SetPath", value)
        self.fields["target"] = value

    def SetArguments(self, value: str) -> None:  # noqa: N802 - the pywin32 name
        self.com.record("SetArguments", value)
        self.fields["arguments"] = value

    def SetWorkingDirectory(self, value: str) -> None:  # noqa: N802 - the pywin32 name
        self.com.record("SetWorkingDirectory", value)
        self.fields["working_directory"] = value

    def SetIconLocation(self, icon: str, index: int) -> None:  # noqa: N802 - the pywin32 name
        self.com.record("SetIconLocation", icon, index)
        self.fields["icon"], self.fields["icon_index"] = icon, index

    def SetDescription(self, value: str) -> None:  # noqa: N802 - the pywin32 name
        self.com.record("SetDescription", value)
        self.fields["description"] = value

    def GetPath(self, flags: int, size: int) -> tuple[str, object]:  # noqa: N802 - the pywin32 name
        self.com.record("GetPath", flags, size)
        target = str(self.fields["target"])
        if len(target) >= size:
            raise com_error(_INSUFFICIENT_BUFFER)
        return target, object()

    def GetArguments(self, size: int) -> str:  # noqa: N802 - the pywin32 name
        self.com.record("GetArguments", size)
        return str(self.fields["arguments"])[: size - 1]

    def GetWorkingDirectory(self, size: int) -> str:  # noqa: N802 - the pywin32 name
        self.com.record("GetWorkingDirectory", size)
        return str(self.fields["working_directory"])[: size - 1]

    def GetDescription(self, size: int) -> str:  # noqa: N802 - the pywin32 name
        self.com.record("GetDescription", size)
        return str(self.fields["description"])[: size - 1]

    def GetIconLocation(self, size: int) -> tuple[str, int]:  # noqa: N802 - the pywin32 name
        self.com.record("GetIconLocation", size)
        return str(self.fields["icon"])[: size - 1], int(self.fields["icon_index"])

    def QueryInterface(self, iid: str) -> _FakePersist:  # noqa: N802 - the pywin32 name
        self.com.record("QueryInterface", iid)
        return _FakePersist(self)


class _FakePersist:
    """An ``IPersistFile`` double whose Save and Load really use the file."""

    def __init__(self, link: _FakeLink) -> None:
        self.link = link

    def __del__(self) -> None:
        self.link.com.calls.append(("released persist", (), threading.get_ident()))

    def Save(self, path: str, remember: int) -> None:  # noqa: N802 - the pywin32 name
        self.link.com.record("Save", path, remember)
        Path(path).write_text(json.dumps(self.link.fields), encoding="utf-8")

    def Load(self, path: str, mode: int) -> None:  # noqa: N802 - the pywin32 name
        self.link.com.record("Load", path, mode)
        self.link.fields = json.loads(Path(path).read_text(encoding="utf-8"))


class _Doubles:
    """The pywin32 modules the adapter loads, wired to one :class:`_Com`."""

    def __init__(self, com: _Com) -> None:
        self.com = com
        self.loaded: list[str] = []

    def CoInitializeEx(self, flags: int) -> None:  # noqa: N802 - the pywin32 name
        self.com.record("CoInitializeEx", flags)

    def CoUninitialize(self) -> None:  # noqa: N802 - the pywin32 name
        self.com.record("CoUninitialize")

    def CoCreateInstance(self, clsid: str, outer: object, context: int, iid: str) -> _FakeLink:  # noqa: N802
        self.com.record("CoCreateInstance", clsid, outer, context, iid)
        return _FakeLink(self.com)

    def load(self, module: str) -> Any:
        self.loaded.append(module)
        if module == "pythoncom":
            return SimpleNamespace(
                COINIT_MULTITHREADED=_COINIT_MULTITHREADED,
                CLSCTX_INPROC_SERVER=_CLSCTX_INPROC_SERVER,
                IID_IPersistFile=_IID_PERSIST_FILE,
                CoInitializeEx=self.CoInitializeEx,
                CoUninitialize=self.CoUninitialize,
                CoCreateInstance=self.CoCreateInstance,
            )
        if module == "win32com.shell.shell":
            return SimpleNamespace(CLSID_ShellLink=_CLSID_SHELL_LINK, IID_IShellLink=_IID_SHELL_LINK)
        raise AssertionError(f"unexpected module {module}")


@pytest.fixture
def lnk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], str]:
    """Return a factory for an absolute Windows ``.lnk`` path that is a real file location here."""
    if sys.platform != "win32":
        monkeypatch.chdir(tmp_path)

    def make(name: str) -> str:
        return str(tmp_path / name) if sys.platform == "win32" else f"C:\\{name}"

    return make


def _controller(com: _Com) -> tuple[NativeShortcutController, _Doubles]:
    doubles = _Doubles(com)
    return NativeShortcutController(load=doubles.load), doubles


def _full_spec(path: str) -> ShortcutSpec:
    return ShortcutSpec(
        path=path,
        target=_TARGET,
        arguments=["--mode", "a b"],
        working_directory="C:\\Tools",
        icon="C:\\Tools\\tool.ico",
        icon_index=-3,
        description="Run the tool",
    )


# --- create ---------------------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_create_sets_every_field_saves_and_returns_what_was_read_back(lnk: Callable[[str], str]) -> None:
    com = _Com()
    controller, _ = _controller(com)
    path = lnk("Tool.lnk")

    info = controller.create(_full_spec(path), force=False)

    assert com.args_of("CoCreateInstance")[0] == (_CLSID_SHELL_LINK, None, _CLSCTX_INPROC_SERVER, _IID_SHELL_LINK)
    assert com.args_of("SetPath") == [(_TARGET,)]
    assert com.args_of("SetArguments") == [('--mode "a b"',)]
    assert com.args_of("SetWorkingDirectory") == [("C:\\Tools",)]
    assert com.args_of("SetIconLocation") == [("C:\\Tools\\tool.ico", -3)]
    assert com.args_of("SetDescription") == [("Run the tool",)]
    assert com.args_of("QueryInterface")[0] == (_IID_PERSIST_FILE,)
    assert com.args_of("Save") == [(path, 0)]
    assert com.args_of("Load") == [(path, 0)], "create must return the read-back, not the spec"
    assert info == ShortcutInfo(
        path=path,
        target=_TARGET,
        arguments='--mode "a b"',
        working_directory="C:\\Tools",
        icon="C:\\Tools\\tool.ico",
        icon_index=-3,
        description="Run the tool",
    )


@pytest.mark.os_agnostic
def test_create_sets_nothing_for_unset_optional_fields(lnk: Callable[[str], str]) -> None:
    com = _Com()
    controller, _ = _controller(com)
    path = lnk("Bare.lnk")

    info = controller.create(ShortcutSpec(path=path, target=_TARGET), force=False)

    setters = [name for name in com.names() if name.startswith("Set")]
    assert setters == ["SetPath"]
    assert info == ShortcutInfo(path=path, target=_TARGET)


@pytest.mark.os_agnostic
def test_an_icon_index_without_an_icon_file_is_still_written(lnk: Callable[[str], str]) -> None:
    """Dropping it would lose a value the caller set; Windows decides what it means."""
    com = _Com()
    controller, _ = _controller(com)

    info = controller.create(ShortcutSpec(path=lnk("Index.lnk"), target=_TARGET, icon_index=4), force=False)

    assert com.args_of("SetIconLocation") == [("", 4)]
    assert info.icon_index == 4


@pytest.mark.os_agnostic
def test_create_refuses_an_existing_file_without_force_before_any_com_call(lnk: Callable[[str], str]) -> None:
    path = lnk("Taken.lnk")
    Path(path).write_bytes(b"not mine")
    com = _Com()
    controller, doubles = _controller(com)

    with pytest.raises(NativeCallError, match="already exists; pass force=True") as info:
        controller.create(_full_spec(path), force=False)

    assert path in str(info.value)
    assert com.calls == [], "no COM call may run when the file is refused"
    assert doubles.loaded == []
    assert Path(path).read_bytes() == b"not mine"


@pytest.mark.os_agnostic
def test_create_with_force_overwrites_an_existing_file(lnk: Callable[[str], str]) -> None:
    path = lnk("Taken.lnk")
    Path(path).write_bytes(b"old")
    controller, _ = _controller(_Com())

    info = controller.create(_full_spec(path), force=True)

    assert info.target == _TARGET
    assert Path(path).read_bytes() != b"old"


@pytest.mark.os_agnostic
def test_a_failed_save_raises_native_call_error_naming_hresult_step_and_path(lnk: Callable[[str], str]) -> None:
    failure = com_error(_PATH_NOT_FOUND)
    com = _Com(fail_at={"Save": failure})
    controller, _ = _controller(com)
    path = lnk("Missing.lnk")

    with pytest.raises(NativeCallError) as info:
        controller.create(_full_spec(path), force=False)

    message = str(info.value)
    assert "0x80070003" in message
    assert "IPersistFile.Save" in message
    assert path in message
    assert info.value.__cause__ is failure
    assert "Load" not in com.names(), "nothing is read back after a failed save"


@pytest.mark.os_agnostic
def test_a_failed_set_stops_before_save(lnk: Callable[[str], str]) -> None:
    """A Save after a failed SetPath writes a link with no target (measured), so it never runs."""
    com = _Com(fail_at={"SetPath": com_error(0x80004005)})
    controller, _ = _controller(com)
    path = lnk("NoTarget.lnk")

    with pytest.raises(NativeCallError, match="0x80004005"):
        controller.create(_full_spec(path), force=False)

    assert "Save" not in com.names()
    assert not Path(path).exists()


@pytest.mark.os_agnostic
def test_a_com_error_without_an_hresult_still_raises_native_call_error(lnk: Callable[[str], str]) -> None:
    failure = com_error(_PATH_NOT_FOUND)
    del failure.hresult
    controller, _ = _controller(_Com(fail_at={"SetDescription": failure}))

    with pytest.raises(NativeCallError, match="without an HRESULT") as info:
        controller.create(_full_spec(lnk("X.lnk")), force=False)

    assert info.value.__cause__ is failure


@pytest.mark.os_agnostic
def test_a_non_com_exception_propagates_unchanged(lnk: Callable[[str], str]) -> None:
    failure = RuntimeError("a bug, not a COM failure")
    controller, _ = _controller(_Com(fail_at={"SetArguments": failure}))

    with pytest.raises(RuntimeError) as info:
        controller.create(_full_spec(lnk("X.lnk")), force=False)

    assert info.value is failure


@pytest.mark.os_agnostic
def test_a_failed_com_init_raises_native_call_error(lnk: Callable[[str], str]) -> None:
    com = _Com(fail_at={"CoInitializeEx": com_error(0x80010106)})
    controller, _ = _controller(com)

    with pytest.raises(NativeCallError, match="0x80010106"):
        controller.create(_full_spec(lnk("X.lnk")), force=False)

    assert "CoCreateInstance" not in com.names()


@pytest.mark.os_agnostic
def test_com_work_runs_off_the_callers_thread_with_objects_released_before_uninit(lnk: Callable[[str], str]) -> None:
    com = _Com()
    controller, _ = _controller(com)

    controller.create(_full_spec(lnk("Thread.lnk")), force=False)

    assert threading.get_ident() not in {thread for _, _, thread in com.calls}
    names = com.names()
    assert names.count("CoInitializeEx") == names.count("CoUninitialize") == 2, "one apartment for save, one for read"
    for uninit in (i for i, name in enumerate(names) if name == "CoUninitialize"):
        before = names[:uninit]
        assert before.count("released link") == before.count("CoCreateInstance")
        assert before.count("released persist") == before.count("QueryInterface")
    assert com.args_of("CoInitializeEx") == [(_COINIT_MULTITHREADED,), (_COINIT_MULTITHREADED,)]


@pytest.mark.os_agnostic
def test_objects_are_released_before_uninit_when_a_com_call_fails(lnk: Callable[[str], str]) -> None:
    com = _Com(fail_at={"Save": com_error(_PATH_NOT_FOUND)})
    controller, _ = _controller(com)

    with pytest.raises(NativeCallError):
        controller.create(_full_spec(lnk("X.lnk")), force=False)

    names = com.names()
    assert names.index("released link") < names.index("CoUninitialize")
    assert names.index("released persist") < names.index("CoUninitialize")


# --- read -----------------------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_read_asks_with_explicit_buffers_and_get_path_flags_0(lnk: Callable[[str], str]) -> None:
    """pywin32's defaults cut arguments at 1023 and the icon at 259 characters (measured)."""
    path = lnk("Read.lnk")
    _controller(_Com())[0].create(_full_spec(path), force=False)
    com = _Com()
    controller, _ = _controller(com)

    controller.read(path)

    ((flags, path_buffer),) = com.args_of("GetPath")
    assert flags == 0
    assert isinstance(path_buffer, int) and path_buffer >= 261
    assert com.args_of("GetArguments") == [(32768,)]
    for getter in ("GetWorkingDirectory", "GetDescription", "GetIconLocation"):
        ((size,),) = com.args_of(getter)
        assert isinstance(size, int) and size >= 261, getter


@pytest.mark.os_agnostic
def test_read_returns_values_at_their_limits_whole(lnk: Callable[[str], str]) -> None:
    path = lnk("Long.lnk")
    argv = ["a" * 32765]  # list2cmdline leaves it unquoted: 32765 characters
    long_icon = "C:\\" + "i" * 257
    spec = ShortcutSpec(
        path=path,
        target="C:\\" + "t" * 256,
        arguments=argv,
        working_directory="C:\\" + "w" * 257,
        icon=long_icon,
        description="d" * 260,
    )
    controller, _ = _controller(_Com())

    info = controller.read(controller.create(spec, force=False).path)

    assert info.target == spec.target
    assert info.arguments == subprocess.list2cmdline(argv)
    assert info.working_directory == spec.working_directory
    assert info.icon == long_icon
    assert info.description == spec.description


@pytest.mark.os_agnostic
def test_read_refuses_a_missing_file_before_any_com_call(lnk: Callable[[str], str]) -> None:
    com = _Com()
    controller, doubles = _controller(com)
    path = lnk("Nowhere.lnk")

    with pytest.raises(NativeCallError, match="does not exist") as info:
        controller.read(path)

    assert path in str(info.value)
    assert com.calls == []
    assert doubles.loaded == []


@pytest.mark.os_agnostic
def test_read_refuses_a_zero_byte_file_before_any_com_call(lnk: Callable[[str], str]) -> None:
    """IPersistFile.Load accepts an empty file and reads every field empty (measured)."""
    path = lnk("Empty.lnk")
    Path(path).write_bytes(b"")
    com = _Com()
    controller, _ = _controller(com)

    with pytest.raises(NativeCallError, match="empty"):
        controller.read(path)

    assert com.calls == []


@pytest.mark.os_agnostic
def test_read_refuses_a_directory_before_any_com_call(lnk: Callable[[str], str]) -> None:
    path = lnk("Folder.lnk")
    Path(path).mkdir()
    com = _Com()
    controller, _ = _controller(com)

    with pytest.raises(NativeCallError, match="not a file"):
        controller.read(path)

    assert com.calls == []


@pytest.mark.os_agnostic
def test_a_failed_load_raises_native_call_error_naming_the_step(lnk: Callable[[str], str]) -> None:
    path = lnk("Garbage.lnk")
    Path(path).write_bytes(b"garbage")
    failure = com_error(0x80004005)
    controller, _ = _controller(_Com(fail_at={"Load": failure}))

    with pytest.raises(NativeCallError) as info:
        controller.read(path)

    assert "IPersistFile.Load" in str(info.value)
    assert "0x80004005" in str(info.value)
    assert path in str(info.value)
    assert info.value.__cause__ is failure


# --- off Windows ----------------------------------------------------------------------------------


_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="pywin32 is installed on Windows")


@pytest.mark.os_posix
@_POSIX_ONLY
def test_create_without_pywin32_is_platform_unsupported(lnk: Callable[[str], str]) -> None:
    with pytest.raises(PlatformUnsupportedError):
        NativeShortcutController().create(_full_spec(lnk("X.lnk")), force=False)


@pytest.mark.os_posix
@_POSIX_ONLY
def test_read_without_pywin32_is_platform_unsupported(lnk: Callable[[str], str]) -> None:
    path = lnk("X.lnk")
    Path(path).write_bytes(b"a shortcut from elsewhere")

    with pytest.raises(PlatformUnsupportedError):
        NativeShortcutController().read(path)


# --- the real pywin32 -----------------------------------------------------------------------------


_WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="calls the real IShellLinkW")


def _unicode() -> str:
    return "ae " + chr(0xE4) + " " + chr(0xE9) + " " + chr(0x65E5) + chr(0x672C)


@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_real_round_trip_of_every_field(tmp_path: Path) -> None:
    path = str(tmp_path / "Real.lnk")
    spec = ShortcutSpec(
        path=path,
        target="%SystemRoot%\\System32\\notepad.exe",
        arguments=["--mode", "a b"],
        working_directory="%TEMP%",
        icon="%SystemRoot%\\System32\\shell32.dll",
        icon_index=-3,
        description="Run the tool",
    )

    info = NativeShortcutController().create(spec, force=False)

    # Checked by suffix: SystemRoot is not always C:\Windows.
    assert info.target.lower().endswith("\\system32\\notepad.exe")
    assert "%" not in info.target, "GetPath(0) expands the environment reference"
    assert (info.arguments, info.working_directory, info.icon, info.icon_index, info.description) == (
        '--mode "a b"',
        "%TEMP%",
        "%SystemRoot%\\System32\\shell32.dll",
        -3,
        "Run the tool",
    )


@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_real_round_trip_of_non_ascii_in_every_field_and_the_file_name(tmp_path: Path) -> None:
    text = _unicode()
    folder = tmp_path / text
    folder.mkdir()
    path = str(folder / f"{text}.lnk")
    spec = ShortcutSpec(
        path=path,
        target=str(folder / f"{text}.exe"),
        arguments=[text],
        working_directory=str(folder),
        icon=str(folder / f"{text}.ico"),
        description=text,
    )

    info = NativeShortcutController().create(spec, force=False)

    assert Path(path).is_file()
    assert info.target == spec.target
    assert info.arguments == spec.joined_arguments
    assert (info.working_directory, info.icon, info.description) == (str(folder), spec.icon, text)


@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_real_save_into_a_missing_directory_is_native_call_error(tmp_path: Path) -> None:
    path = str(tmp_path / "missing" / "X.lnk")

    with pytest.raises(NativeCallError, match="0x80070003"):
        NativeShortcutController().create(ShortcutSpec(path=path, target=_TARGET), force=False)


@pytest.mark.os_windows
@_WINDOWS_ONLY
def test_real_calls_work_from_a_worker_thread_and_leave_the_main_thread_usable(tmp_path: Path) -> None:
    results: list[ShortcutInfo] = []
    errors: list[BaseException] = []

    def make() -> None:
        try:
            spec = ShortcutSpec(path=str(tmp_path / "FromThread.lnk"), target=_TARGET)
            results.append(NativeShortcutController().create(spec, force=False))
        except BaseException as exc:  # handed to the test thread
            errors.append(exc)

    worker = threading.Thread(target=make)
    worker.start()
    worker.join()

    assert errors == []
    assert results[0].target == _TARGET
    again = NativeShortcutController().read(str(tmp_path / "FromThread.lnk"))
    assert again == results[0]
