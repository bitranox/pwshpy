"""Cross-platform pipe-closed detection for the CLI output writer."""

from __future__ import annotations

import errno

import pytest

from pwshpy.adapters.cli.output import is_pipe_closed


class _FakeWindowsPipeError(OSError):
    """OSError carrying a Windows-style ``winerror``, for cross-platform testing."""

    def __init__(self, winerror: int) -> None:
        super().__init__("pipe closing")
        self.winerror = winerror


@pytest.mark.os_agnostic
def test_broken_pipe_error_is_pipe_closed() -> None:
    """POSIX BrokenPipeError counts as a closed pipe."""
    assert is_pipe_closed(BrokenPipeError()) is True


@pytest.mark.os_agnostic
def test_epipe_oserror_is_pipe_closed() -> None:
    """A raw OSError carrying EPIPE counts as a closed pipe."""
    assert is_pipe_closed(OSError(errno.EPIPE, "broken pipe")) is True


@pytest.mark.os_agnostic
@pytest.mark.parametrize("winerror", [109, 232])
def test_windows_pipe_closed_winerror_is_pipe_closed(winerror: int) -> None:
    """The Windows OSError variants (winerror 109/232) count as a closed pipe."""
    assert is_pipe_closed(_FakeWindowsPipeError(winerror)) is True


@pytest.mark.os_agnostic
def test_unrelated_winerror_is_not_pipe_closed() -> None:
    """A non-pipe Windows error (e.g. ERROR_ACCESS_DENIED 5) is NOT a closed pipe."""
    assert is_pipe_closed(_FakeWindowsPipeError(5)) is False


@pytest.mark.os_agnostic
def test_unrelated_oserror_is_not_pipe_closed() -> None:
    """A genuine write failure (e.g. ENOSPC) must NOT be treated as a closed pipe."""
    assert is_pipe_closed(OSError(errno.ENOSPC, "no space left on device")) is False
