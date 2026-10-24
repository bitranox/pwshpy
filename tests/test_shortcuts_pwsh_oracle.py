r"""Shortcut oracle: pwshpy's ``.lnk`` write and read vs WScript.Shell on a real Windows host.

pwsh users make and read shortcuts through ``(New-Object -ComObject WScript.Shell)
.CreateShortcut(path)``, so that object is the oracle in both directions: pwshpy writes and
WScript reads ``TargetPath`` / ``Arguments`` / ``WorkingDirectory`` / ``IconLocation`` /
``Description``, and WScript writes (``Save``) while pwshpy reads.  ``local_only`` +
``os_windows``; not ``mutating``: every file lives under ``tmp_path``.

WScript is ANSI-lossy (CJK becomes ``?``, a CJK file name cannot be saved) and reads at most
1023 argument characters, so every comparison with it uses ASCII data and a short argument line.
Non-ASCII in every field and the file name is pinned by
``test_shortcuts_adapter.py::test_real_round_trip_of_non_ascii_in_every_field_and_the_file_name``;
values at their length limits by the pwshpy-only round trip at the end of this file.

Values cross into pwsh through environment variables, never interpolated into the script, and
come back as ``ConvertTo-Json -EscapeHandling EscapeNonAscii`` so the console code page cannot
alter them.  Nothing compared here is a localized string.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from pwshpy import ps
from pwshpy.domain.errors import NativeCallError
from pwshpy.domain.records import ShortcutInfo

pytestmark = [pytest.mark.local_only, pytest.mark.os_windows]

if sys.platform != "win32":  # pragma: no cover - CI filters local_only; guards a stray run
    pytest.skip("the shortcut oracle is Windows-only: .lnk files are a Windows shell feature", allow_module_level=True)


def _find_pwsh() -> str | None:
    installed = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    return str(installed) if installed.is_file() else shutil.which("pwsh")


_PWSH = _find_pwsh()

if _PWSH is None:  # pragma: no cover - the dev box and the CI images ship pwsh 7
    pytest.skip("pwsh (PowerShell 7) is not installed: the shortcut oracle needs it", allow_module_level=True)

#: Reads one shortcut; the path arrives in PWSHPY_LNK.
_READ_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PWSHPY_LNK); "
    "[ordered]@{ TargetPath = $s.TargetPath; Arguments = $s.Arguments; "
    "WorkingDirectory = $s.WorkingDirectory; IconLocation = $s.IconLocation; "
    "Description = $s.Description } | ConvertTo-Json -Compress -EscapeHandling EscapeNonAscii"
)

#: Writes one shortcut; every value arrives in a PWSHPY_* variable.
_WRITE_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PWSHPY_LNK); "
    "$s.TargetPath = $env:PWSHPY_TARGET; $s.Arguments = $env:PWSHPY_ARGUMENTS; "
    "$s.WorkingDirectory = $env:PWSHPY_WORKING_DIRECTORY; $s.IconLocation = $env:PWSHPY_ICON_LOCATION; "
    "$s.Description = $env:PWSHPY_DESCRIPTION; $s.Save()"
)

_NOTEPAD = "%SystemRoot%\\System32\\notepad.exe"
_SHELL32 = "%SystemRoot%\\System32\\shell32.dll"
#: Quotes on the stored line: list2cmdline quotes the argument holding a blank.
_ARGV = ("--exclude-newer", "7 days", "lsdsk")
_COMMAND_LINE = '--exclude-newer "7 days" lsdsk'
#: E_FAIL: IShellLink.SetPath refuses a target whose EXPANDED form exceeds 259 characters.
_E_FAIL = "0x80004005"


def _pwsh(script: str, values: dict[str, str]) -> str:
    """Run ``script`` in pwsh with ``values`` added to the environment; return its stdout."""
    assert _PWSH is not None
    completed = subprocess.run(  # noqa: S603 - absolute pwsh path and a constant script; test oracle only
        [_PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, **values},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _wscript_read(path: Path) -> dict[str, str]:
    """Return WScript's view of the shortcut at ``path``."""
    fields: dict[str, str] = json.loads(_pwsh(_READ_SCRIPT, {"PWSHPY_LNK": str(path)}))
    return fields


def _wscript_write(
    path: Path, *, target: str, arguments: str, working_directory: str, icon_location: str, description: str
) -> None:
    """Write a shortcut at ``path`` with WScript, the way a pwsh script does."""
    _pwsh(
        _WRITE_SCRIPT,
        {
            "PWSHPY_LNK": str(path),
            "PWSHPY_TARGET": target,
            "PWSHPY_ARGUMENTS": arguments,
            "PWSHPY_WORKING_DIRECTORY": working_directory,
            "PWSHPY_ICON_LOCATION": icon_location,
            "PWSHPY_DESCRIPTION": description,
        },
    )


def _as_wscript(info: ShortcutInfo) -> dict[str, str]:
    """Render a pwshpy record in WScript's shape (IconLocation is ``"<icon>,<index>"``)."""
    return {
        "TargetPath": info.target,
        "Arguments": info.arguments,
        "WorkingDirectory": info.working_directory,
        "IconLocation": f"{info.icon},{info.icon_index}",
        "Description": info.description,
    }


def _expanded(text: str) -> str:
    """Expand ``%NAME%`` references like Windows does for a target that does not exist."""
    return os.path.expandvars(text)


def _on_disk(text: str) -> str:
    """Return the expansion of ``text`` spelled as the filesystem stores it.

    SetPath resolves an EXISTING target to its shell item, so both readers report the on-disk
    spelling (``C:\\Windows\\System32\\notepad.exe``) even when the variable holds another case
    (``SystemRoot=C:\\WINDOWS``, measured); a missing target keeps the plain expansion.
    """
    return str(Path(_expanded(text)).resolve(strict=True))


def test_wscript_reads_every_field_pwshpy_wrote(tmp_path: Path) -> None:
    """pwshpy writes, WScript reads: the same five fields, the target expanded on both sides."""
    path = tmp_path / "PwshpyWrote.lnk"

    ours = ps.new_shortcut(
        path,
        target=_NOTEPAD,
        arguments=_ARGV,
        working_directory=str(tmp_path),
        icon=_SHELL32,
        icon_index=-3,
        description="Run lsdsk",
    )

    assert (
        _wscript_read(path)
        == _as_wscript(ours)
        == {
            "TargetPath": _on_disk(_NOTEPAD),
            "Arguments": _COMMAND_LINE,
            "WorkingDirectory": str(tmp_path),
            "IconLocation": f"{_SHELL32},-3",
            "Description": "Run lsdsk",
        }
    )


def test_wscript_and_pwshpy_agree_on_unset_fields(tmp_path: Path) -> None:
    """A target-only shortcut: WScript shows the unset icon as ``,0``, pwshpy as ``""`` and 0."""
    path = tmp_path / "TargetOnly.lnk"

    ours = ps.new_shortcut(path, target=_NOTEPAD)

    assert (ours.arguments, ours.working_directory, ours.icon, ours.icon_index, ours.description) == ("", "", "", 0, "")
    assert _wscript_read(path) == _as_wscript(ours)
    assert _wscript_read(path)["IconLocation"] == ",0"


def test_pwshpy_reads_every_field_wscript_wrote(tmp_path: Path) -> None:
    """WScript writes, pwshpy reads: raw arguments, directory and icon; the target expanded."""
    path = tmp_path / "WScriptWrote.lnk"
    _wscript_write(
        path,
        target=_NOTEPAD,
        arguments=_COMMAND_LINE,
        working_directory="%TEMP%",
        icon_location=f"{_SHELL32},-3",
        description="Written by WScript",
    )

    ours = ps.get_shortcut(path)

    assert ours == ShortcutInfo(
        path=str(path),
        target=_on_disk(_NOTEPAD),
        arguments=_COMMAND_LINE,
        working_directory="%TEMP%",
        icon=_SHELL32,
        icon_index=-3,
        description="Written by WScript",
    )
    assert _as_wscript(ours) == _wscript_read(path)


def test_a_read_from_a_worker_thread_returns_the_same_record(tmp_path: Path) -> None:
    """get_shortcut called on a plain worker thread reads what the calling thread reads."""
    path = tmp_path / "Threaded.lnk"
    _wscript_write(
        path,
        target=_NOTEPAD,
        arguments=_COMMAND_LINE,
        working_directory=str(tmp_path),
        icon_location=f"{_SHELL32},0",
        description="t",
    )
    results: list[ShortcutInfo] = []
    errors: list[BaseException] = []

    def read() -> None:
        try:
            results.append(ps.get_shortcut(path))
        except BaseException as exc:  # handed back to the test thread
            errors.append(exc)

    worker = threading.Thread(target=read)
    worker.start()
    worker.join(timeout=60)

    assert not worker.is_alive()
    assert errors == []
    assert results == [ps.get_shortcut(path)]
    assert results[0].target == _on_disk(_NOTEPAD)


def test_an_existing_file_is_kept_without_force_and_replaced_with_it(tmp_path: Path) -> None:
    """Without force the existing file's bytes stay as they were; with force the new target is stored."""
    path = tmp_path / "Existing.lnk"
    ps.new_shortcut(path, target=_NOTEPAD, description="first")
    before = path.read_bytes()

    with pytest.raises(NativeCallError, match="already exists"):
        ps.new_shortcut(path, target="%SystemRoot%\\System32\\calc.exe", description="second")

    assert path.read_bytes() == before
    assert _wscript_read(path)["TargetPath"] == _on_disk(_NOTEPAD)

    replaced = ps.new_shortcut(path, target="%SystemRoot%\\System32\\calc.exe", description="second", force=True)

    assert replaced.target == _on_disk("%SystemRoot%\\System32\\calc.exe")
    assert (replaced.description, _wscript_read(path)["TargetPath"]) == ("second", replaced.target)


def _env_target(expanded_length: int) -> str:
    """A ``%TEMP%\\...`` target whose raw text is shorter than its expansion, at ``expanded_length``."""
    temp = os.environ["TEMP"]
    target = "%TEMP%\\" + "a" * (expanded_length - len(temp) - 1)
    assert len(_expanded(target)) == expanded_length
    assert len(target) < expanded_length, "TEMP must expand to more characters than its own reference"
    return target


def test_an_env_target_expanding_to_259_characters_reads_back_whole(tmp_path: Path) -> None:
    """The longest expansion SetPath takes reads back whole through pwshpy and WScript alike."""
    path = tmp_path / "Exp259.lnk"
    target = _env_target(259)

    ours = ps.new_shortcut(path, target=target)

    assert ours.target == _expanded(target)
    assert _wscript_read(path)["TargetPath"] == ours.target


def test_an_env_target_expanding_past_259_characters_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    """Raw text within the 259 limit, expansion at 260: SetPath fails E_FAIL and no file appears.

    The domain check sees only the raw text (its length is within the limit), so the refusal
    comes from the shell as NativeCallError, and the adapter never saves after a failed set.
    """
    path = tmp_path / "Exp260.lnk"
    target = _env_target(260)
    assert len(target) <= 259

    with pytest.raises(NativeCallError, match=_E_FAIL):
        ps.new_shortcut(path, target=target)

    assert not path.exists()


def test_values_at_their_length_limits_round_trip_whole_through_pwshpy(tmp_path: Path) -> None:
    """pwshpy alone (WScript stops at 1023 argument characters): every field at its limit, non-ASCII included."""
    path = tmp_path / "Limits.lnk"
    wide = chr(0x65E5) + "a"
    argument = wide * 16383 + "a"
    target = "C:\\" + "t" * 252 + ".exe"
    working_directory = "C:\\" + "w" * 257
    icon = "C:\\" + "i" * 253 + ".ico"
    description = (wide * 130)[:260]
    assert (len(argument), len(target), len(working_directory), len(icon), len(description)) == (
        32767,
        259,
        260,
        260,
        260,
    )

    written = ps.new_shortcut(
        path,
        target=target,
        arguments=[argument],
        working_directory=working_directory,
        icon=icon,
        description=description,
    )

    expected = ShortcutInfo(
        path=str(path),
        target=target,
        arguments=argument,
        working_directory=working_directory,
        icon=icon,
        icon_index=0,
        description=description,
    )
    assert written == expected
    assert ps.get_shortcut(path) == expected
