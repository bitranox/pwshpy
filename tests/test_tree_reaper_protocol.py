"""The wire contract between ``process_tree_posix`` (parent) and ``_tree_reaper`` (helper).

The two ends run in different processes, so the JSON on both pipes is the contract.  The
byte literals below are what the helper and the parent wrote before their payloads became
typed models; each test proves the typed code still writes, and still reads, exactly those
bytes - including an argv element that is not valid UTF-8, which travels surrogate-escaped.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pwshpy.adapters.native._tree_reaper import (  # pyright: ignore[reportPrivateUsage]
    _Exited,  # pyright: ignore[reportPrivateUsage]
    _Failed,  # pyright: ignore[reportPrivateUsage]
    _Killed,  # pyright: ignore[reportPrivateUsage]
    decode_request,
    encode_status,
)
from pwshpy.adapters.native.process_tree_posix import (
    PosixReaperTree,
    _HelperRequest,  # pyright: ignore[reportPrivateUsage]
    _HelperStatus,  # pyright: ignore[reportPrivateUsage]
    _no_status_error,  # pyright: ignore[reportPrivateUsage]
    _status_error,  # pyright: ignore[reportPrivateUsage]
)
from pwshpy.domain.errors import NativeCallError

_HELPER = Path(__file__).resolve().parents[1] / "src" / "pwshpy" / "adapters" / "native" / "_tree_reaper.py"
# The POSIX decoding of a non-UTF-8 name (it carries the lone surrogate U+DCFF). Spelled out
# rather than os.fsdecode, which is strict on Windows and would fail this module's collection
# there, although its wire tests are OS-agnostic.
_BAD_NAME = b"/nonexistent-\xff-prog".decode("utf-8", "surrogateescape")

_REQUEST_BYTES = b'{"argv": ["/nonexistent-\\udcff-prog", "x"], "cwd": null, "env": null}'
_FAILED_BYTES = (
    b'{"error": "FileNotFoundError", "message": "[Errno 2] No such file or directory: \'/nonexistent-\\\\udcff-prog\'"}'
)
_FAILED_MESSAGE = "[Errno 2] No such file or directory: '/nonexistent-\\udcff-prog'"
#: Popen's own refusal of a non-str argv element, env value or cwd, as the helper reports it.
_POPEN_INT_BYTES = b'{"error": "TypeError", "message": "expected str, bytes or os.PathLike object, not int"}'


@pytest.mark.os_agnostic
def test_the_parent_writes_the_request_bytes_the_helper_always_read() -> None:
    """The typed request serialises to the old fixed-key JSON, surrogate escape included."""
    request = _HelperRequest.model_construct(argv=[_BAD_NAME, "x"], cwd=None, env=None)
    assert request.wire() == _REQUEST_BYTES


@pytest.mark.os_agnostic
def test_the_request_keeps_cwd_and_env_through_the_wire() -> None:
    """cwd and a full env (non-UTF-8 key and value too) round-trip parent -> helper unchanged."""
    env = {_BAD_NAME: _BAD_NAME, "PATH": "/bin"}
    wire = _HelperRequest.model_construct(argv=["prog"], cwd="/tmp", env=env).wire()
    assert wire == (
        b'{"argv": ["prog"], "cwd": "/tmp", "env": {"/nonexistent-\\udcff-prog": "/nonexistent-\\udcff-prog",'
        b' "PATH": "/bin"}}'
    )
    decoded = decode_request(wire)
    assert decoded.argv == ("prog",)
    assert decoded.cwd == "/tmp"
    assert decoded.env == env


@pytest.mark.os_agnostic
def test_the_helper_decodes_a_non_utf8_argv_element_back_to_its_surrogate() -> None:
    """The surrogate-escaped argv element arrives as the same str, so Popen re-encodes the original bytes."""
    decoded = decode_request(_REQUEST_BYTES)
    assert decoded.argv == (_BAD_NAME, "x")
    program = decoded.argv[0]
    assert isinstance(program, str)
    # The POSIX re-encoding the helper's Popen applies (os.fsencode is strict on Windows).
    assert program.encode("utf-8", "surrogateescape") == b"/nonexistent-\xff-prog"
    assert decoded.cwd is None
    assert decoded.env is None


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b'{"argv": ["x"]}', KeyError),
        (b"[1]", TypeError),
        (b"not json", ValueError),
        (b'{"argv": 5, "cwd": null, "env": null}', TypeError),
        (b'{"argv": ["x"], "cwd": null, "env": 5}', TypeError),
    ],
)
def test_a_structurally_malformed_request_is_refused(raw: bytes, error: type[Exception]) -> None:
    """A missing key, a non-object, bad JSON or a non-iterable argv/env raises before any program starts."""
    with pytest.raises(error):
        decode_request(raw)


@pytest.mark.os_agnostic
def test_the_helper_leaves_element_types_to_popen() -> None:
    """Non-str elements decode unchanged: Popen refuses them, with its own TypeError text, not the decoder."""
    decoded = decode_request(b'{"argv": ["echo", 5], "cwd": 1, "env": {"A": 1}}')
    assert decoded.argv == ("echo", 5)
    assert decoded.cwd == 1
    assert decoded.env == {"A": 1}


@pytest.mark.os_agnostic
def test_a_string_argv_is_split_into_characters_as_list_always_did() -> None:
    """argv goes through the same sequence conversion as before, so a bare string becomes one argument per char."""
    assert decode_request(b'{"argv": "ab", "cwd": null, "env": null}').argv == ("a", "b")


@pytest.mark.os_agnostic
def test_each_status_variant_encodes_to_its_old_bytes() -> None:
    """Every status carries only the keys it always carried, in the old order and separators."""
    assert encode_status(_Exited(exit_code=3)) == '{"exit_code": 3}'
    assert encode_status(_Exited(exit_code=-9)) == '{"exit_code": -9}'
    assert encode_status(_Killed()) == '{"killed": true}'
    failed = _Failed(error="FileNotFoundError", message=_FAILED_MESSAGE)
    assert encode_status(failed).encode("utf-8") == _FAILED_BYTES


@pytest.mark.os_agnostic
def test_a_surrogate_in_a_status_message_survives_the_wire() -> None:
    """A message holding a lone surrogate is escaped by the helper and restored by the parent."""
    wire = encode_status(_Failed(error="OSError", message=_BAD_NAME)).encode("utf-8")
    assert wire == b'{"error": "OSError", "message": "/nonexistent-\\udcff-prog"}'
    assert _HelperStatus.from_wire(wire) == _HelperStatus(error="OSError", message=_BAD_NAME)


@pytest.mark.os_agnostic
def test_the_parent_parses_each_status_variant() -> None:
    """The parent reads each helper variant into the one typed status."""
    assert _HelperStatus.from_wire(b'{"exit_code": 3}') == _HelperStatus(exit_code=3)
    assert _HelperStatus.from_wire(b'{"killed": true}') == _HelperStatus(killed=True)
    assert _HelperStatus.from_wire(_FAILED_BYTES) == _HelperStatus(error="FileNotFoundError", message=_FAILED_MESSAGE)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[1]", b"null", b'{"unexpected": 1}', b'{"exit_code": "three"}', b'{"error": 5}'],
)
def test_an_unreadable_status_is_the_empty_status(raw: bytes) -> None:
    """Missing, undecodable, non-object or wrongly typed reports all parse as the empty status."""
    assert _HelperStatus.from_wire(raw) == _HelperStatus()


@pytest.mark.os_agnostic
def test_the_status_errors_keep_their_messages() -> None:
    """The exact NativeCallError texts for a missing program, another failure, a kill and no status."""
    args = ["prog"]
    missing = _status_error(args, _HelperStatus(error="FileNotFoundError", message="gone"))
    assert str(missing) == "Executable not found: 'prog' (gone)"
    other = _status_error(args, _HelperStatus(error="PermissionError", message="denied"))
    assert str(other) == "Failed to run 'prog': PermissionError: denied"
    killed = _no_status_error(args, _HelperStatus(killed=True))
    assert str(killed) == "The wait_tree helper for 'prog' ended without a status: {'killed': True}"
    nothing = _no_status_error(args, _HelperStatus())
    assert str(nothing) == "The wait_tree helper for 'prog' ended without a status: {}"


def _helper_status_bytes(request: bytes, *, terminate_after: Path | None = None) -> bytes:
    """Run the real helper on ``request`` and return the raw bytes it wrote to the status pipe."""
    control_r, control_w = os.pipe()
    status_r, status_w = os.pipe()
    helper = subprocess.Popen(  # noqa: S603 - fixed interpreter argv, no shell
        [sys.executable, "-I", str(_HELPER), str(control_r), str(status_w)], pass_fds=(control_r, status_w)
    )
    os.close(control_r)
    os.close(status_w)
    os.write(control_w, request)
    os.close(control_w)
    if terminate_after is not None:
        _wait_for_file(terminate_after)
        helper.send_signal(signal.SIGTERM)
    helper.wait(timeout=60)
    with os.fdopen(status_r, "rb") as status:
        return status.read()


def _wait_for_file(path: Path) -> None:
    end = time.monotonic() + 30
    while not path.exists():
        assert time.monotonic() < end, f"{path} never appeared"
        time.sleep(0.02)


def _sleeper_argv(marker: Path) -> list[str]:
    script = "import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
    return [sys.executable, "-c", script, str(marker)]


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the helper runs on POSIX only")
def test_the_real_helper_writes_the_old_bytes_for_each_variant(tmp_path: Path) -> None:
    """End to end through the helper process: exit code, start failure (non-UTF-8 name) and kill."""
    exited = _HelperRequest.model_construct(argv=[sys.executable, "-c", "raise SystemExit(3)"], cwd=None, env=None)
    assert _helper_status_bytes(exited.wire()) == b'{"exit_code": 3}'
    assert _helper_status_bytes(_REQUEST_BYTES) == _FAILED_BYTES
    marker = tmp_path / "started"
    killed = _HelperRequest.model_construct(argv=_sleeper_argv(marker), cwd=None, env=None)
    assert _helper_status_bytes(killed.wire(), terminate_after=marker) == b'{"killed": true}'


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the helper runs on POSIX only")
@pytest.mark.parametrize(
    ("request_bytes", "status_bytes"),
    [
        (b"not json", b'{"error": "JSONDecodeError", "message": "Expecting value: line 1 column 1 (char 0)"}'),
        (b'{"argv": ["x"]}', b'{"error": "KeyError", "message": "\'env\'"}'),
        (b"[1]", b'{"error": "TypeError", "message": "list indices must be integers or slices, not str"}'),
        (
            b'{"argv": 5, "cwd": null, "env": null}',
            b'{"error": "TypeError", "message": "\'int\' object is not iterable"}',
        ),
        (
            b'{"argv": ["x"], "cwd": null, "env": 5}',
            b'{"error": "TypeError", "message": "\'int\' object is not iterable"}',
        ),
        (b'{"argv": ["echo", 5], "cwd": null, "env": null}', _POPEN_INT_BYTES),
        (b'{"argv": ["echo"], "cwd": null, "env": {"A": 1}}', _POPEN_INT_BYTES),
        (b'{"argv": ["echo"], "cwd": 1, "env": null}', _POPEN_INT_BYTES),
    ],
)
def test_the_real_helper_reports_a_malformed_request_as_before(request_bytes: bytes, status_bytes: bytes) -> None:
    """A request the helper cannot decode is reported as the same error status it always was."""
    assert _helper_status_bytes(request_bytes) == status_bytes


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the helper runs on POSIX only")
def test_the_parent_names_a_missing_non_utf8_program_through_the_real_helper() -> None:
    """The start failure of a non-UTF-8 program name reaches the caller as 'Executable not found'."""
    tree = PosixReaperTree()
    args = [_BAD_NAME]
    try:
        tree.launch(args, cwd=None, env=None, with_stdin=False)
        assert tree.wait(time.monotonic() + 60)
        with pytest.raises(NativeCallError) as caught:
            tree.exit_code(args)
    finally:
        tree.close()
    assert str(caught.value) == f"Executable not found: {_BAD_NAME!r} ({_FAILED_MESSAGE})"


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the helper runs on POSIX only")
def test_the_parent_reports_a_killed_helper_as_ending_without_a_status(tmp_path: Path) -> None:
    """A helper SIGTERMed mid-run reports killed; the parent turns that into the no-status error."""
    marker = tmp_path / "started"
    args = _sleeper_argv(marker)
    tree = PosixReaperTree()
    try:
        popen = tree.launch(args, cwd=None, env=None, with_stdin=False)
        _wait_for_file(marker)
        popen.send_signal(signal.SIGTERM)
        assert tree.wait(time.monotonic() + 60)
        with pytest.raises(NativeCallError) as caught:
            tree.exit_code(args)
    finally:
        tree.close()
    assert str(caught.value) == f"The wait_tree helper for {args[0]!r} ended without a status: {{'killed': True}}"


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="the helper runs on POSIX only")
def test_the_parent_reads_the_programs_exit_code_through_the_real_helper() -> None:
    """The exit-code variant reaches the caller as the program's own code."""
    args = [sys.executable, "-c", "raise SystemExit(7)"]
    tree = PosixReaperTree()
    try:
        tree.launch(args, cwd=None, env=None, with_stdin=False)
        assert tree.wait(time.monotonic() + 60)
        assert tree.exit_code(args) == 7
    finally:
        tree.close()
