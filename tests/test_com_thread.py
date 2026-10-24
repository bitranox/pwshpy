"""The fresh-MTA worker thread every in-process COM call of pwshpy runs on.

``pythoncom`` is a double handed in through ``load=``, so every case runs on any OS: it records
which thread initialised and uninitialised COM and in what order, against the work itself. The
case without a double checks the off-Windows refusal.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import pytest

from pwshpy.adapters.native.com_thread import run_in_com_thread
from pwshpy.domain.errors import PlatformUnsupportedError

_COINIT_MULTITHREADED = 0


class _PythoncomDouble:
    """A ``pythoncom`` double: records (event, thread id) pairs for init, uninit and the work."""

    COINIT_MULTITHREADED = _COINIT_MULTITHREADED

    def __init__(self, *, init_fails_with: BaseException | None = None) -> None:
        self.events: list[tuple[str, int]] = []
        self.init_flags: list[int] = []
        self.init_fails_with = init_fails_with

    def CoInitializeEx(self, flags: int) -> None:  # noqa: N802 - the pywin32 name
        self.init_flags.append(flags)
        self.events.append(("init", threading.get_ident()))
        if self.init_fails_with is not None:
            raise self.init_fails_with

    def CoUninitialize(self) -> None:  # noqa: N802 - the pywin32 name
        self.events.append(("uninit", threading.get_ident()))

    def record(self, event: str) -> None:
        self.events.append((event, threading.get_ident()))


class _Tracked:
    """Stands in for a COM object: records its release, which must come before the uninit."""

    def __init__(self, pythoncom: _PythoncomDouble) -> None:
        self.pythoncom = pythoncom

    def __del__(self) -> None:
        self.pythoncom.record("released")


def _loader(pythoncom: _PythoncomDouble, asked: list[str] | None = None) -> Any:
    def load(module: str) -> Any:
        if asked is not None:
            asked.append(module)
        assert module == "pythoncom"
        return pythoncom

    return load


@pytest.mark.os_agnostic
def test_the_work_runs_between_init_and_uninit_on_one_fresh_thread_and_its_result_comes_back() -> None:
    pythoncom = _PythoncomDouble()

    def work() -> str:
        pythoncom.record("work")
        return "answer"

    result = run_in_com_thread(work, load=_loader(pythoncom))

    assert result == "answer"
    assert [event for event, _ in pythoncom.events] == ["init", "work", "uninit"]
    threads = {thread for _, thread in pythoncom.events}
    assert len(threads) == 1, "init, work and uninit must share one thread"
    assert threading.get_ident() not in threads, "COM was touched on the caller's thread"
    assert pythoncom.init_flags == [_COINIT_MULTITHREADED]


@pytest.mark.os_agnostic
def test_each_call_gets_its_own_thread() -> None:
    pythoncom = _PythoncomDouble()

    def work() -> int:
        return threading.get_ident()

    first = run_in_com_thread(work, load=_loader(pythoncom))
    second = run_in_com_thread(work, load=_loader(pythoncom))

    assert threading.get_ident() not in {first, second}
    assert [event for event, _ in pythoncom.events] == ["init", "uninit", "init", "uninit"]


@pytest.mark.os_agnostic
def test_the_works_exception_reaches_the_caller_and_uninit_still_runs() -> None:
    pythoncom = _PythoncomDouble()
    failure = ValueError("the COM call failed")

    def work() -> None:
        raise failure

    with pytest.raises(ValueError, match="the COM call failed") as info:
        run_in_com_thread(work, load=_loader(pythoncom))

    assert info.value is failure
    assert [event for event, _ in pythoncom.events] == ["init", "uninit"]


@pytest.mark.os_agnostic
def test_com_objects_of_a_successful_work_are_released_before_uninit() -> None:
    pythoncom = _PythoncomDouble()

    def work() -> str:
        link = _Tracked(pythoncom)
        return f"done with {type(link).__name__}"

    run_in_com_thread(work, load=_loader(pythoncom))

    assert [event for event, _ in pythoncom.events] == ["init", "released", "uninit"]


@pytest.mark.os_agnostic
def test_com_objects_of_a_failing_work_are_released_before_uninit() -> None:
    """The traceback keeps the work's frame and its locals alive; they must go before the uninit.

    Released after it, a COM object is released into a torn-down apartment from whichever
    thread drops the last reference.
    """
    pythoncom = _PythoncomDouble()

    def work() -> None:
        link = _Tracked(pythoncom)
        raise RuntimeError(f"failed while holding {type(link).__name__}")

    with pytest.raises(RuntimeError, match="holding _Tracked"):
        run_in_com_thread(work, load=_loader(pythoncom))

    assert [event for event, _ in pythoncom.events] == ["init", "released", "uninit"]


@pytest.mark.os_agnostic
def test_com_objects_held_by_a_chained_exception_are_released_before_uninit() -> None:
    pythoncom = _PythoncomDouble()

    def inner() -> None:
        link = _Tracked(pythoncom)
        raise OSError(f"inner failure holding {type(link).__name__}")

    def work() -> None:
        try:
            inner()
        except OSError as exc:
            raise RuntimeError("outer failure") from exc

    with pytest.raises(RuntimeError, match="outer failure"):
        run_in_com_thread(work, load=_loader(pythoncom))

    assert [event for event, _ in pythoncom.events] == ["init", "released", "uninit"]


@pytest.mark.os_agnostic
def test_a_failed_init_reaches_the_caller_without_running_the_work_or_uninit() -> None:
    failure = RuntimeError("CoInitializeEx refused")
    pythoncom = _PythoncomDouble(init_fails_with=failure)
    ran: list[bool] = []

    with pytest.raises(RuntimeError) as info:
        run_in_com_thread(lambda: ran.append(True), load=_loader(pythoncom))

    assert info.value is failure
    assert ran == []
    assert [event for event, _ in pythoncom.events] == ["init"]


@pytest.mark.os_agnostic
def test_pythoncom_is_loaded_through_the_seam() -> None:
    pythoncom = _PythoncomDouble()
    asked: list[str] = []

    run_in_com_thread(lambda: None, load=_loader(pythoncom, asked))

    assert asked == ["pythoncom"]


@pytest.mark.os_agnostic
def test_a_loader_refusal_reaches_the_caller_before_any_thread_starts() -> None:
    def load(module: str) -> Any:
        raise PlatformUnsupportedError(f"{module} is unavailable")

    before = threading.active_count()
    with pytest.raises(PlatformUnsupportedError, match="pythoncom"):
        run_in_com_thread(lambda: None, load=load)
    assert threading.active_count() == before


@pytest.mark.os_agnostic
def test_the_work_may_return_none() -> None:
    pythoncom = _PythoncomDouble()

    assert run_in_com_thread(lambda: None, load=_loader(pythoncom)) is None


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="pywin32 is installed on Windows")
def test_without_pywin32_the_default_loader_refuses_with_platform_unsupported() -> None:
    with pytest.raises(PlatformUnsupportedError, match="pythoncom"):
        run_in_com_thread(lambda: None)


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="the real pythoncom exists only on Windows")
def test_the_real_pythoncom_gives_the_worker_a_multithreaded_apartment() -> None:
    """Measured by the apartment the worker reports: 1 is APTTYPE_MTA."""
    import ctypes

    def apartment() -> int:
        kind = ctypes.c_int()
        qualifier = ctypes.c_int()
        hresult = ctypes.windll.ole32.CoGetApartmentType(ctypes.byref(kind), ctypes.byref(qualifier))  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
        assert hresult == 0
        return kind.value

    assert run_in_com_thread(apartment) == 1
