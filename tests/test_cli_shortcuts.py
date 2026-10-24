"""CLI tests for ``get_known_folder``, ``get_shortcut`` and ``new_shortcut``.

The ``os_agnostic`` tests drive the real CLI (``CliRunner`` or ``main``) against a fake ``ps``
facade, so no host state changes. The piped tests run ``python -m pwshpy`` with a cp1252 stdio
and read back the BYTES: the known-folder one on Linux/macOS through the real resolver, the
shortcut one on Windows through the real ``IShellLinkW``.

Non-ASCII values are built with chr() so the source stays ASCII.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import click
import orjson
import pytest
from click.testing import CliRunner
from fake_services import services_with_ps

from pwshpy.adapters.cli.main import main
from pwshpy.adapters.cli.root import cli
from pwshpy.adapters.native.known_folders import posix_known_folder
from pwshpy.domain.enums import KnownFolder
from pwshpy.domain.errors import NativeCallError, PlatformUnsupportedError
from pwshpy.domain.records import ShortcutInfo

#: Not representable in cp1252, so a text-layer write would raise UnicodeEncodeError.
_BEYOND_CP1252 = chr(0x4E2D) + chr(0x6587) + " " + chr(0x2603)


class _FakePs:
    """Records the facade calls the shortcut commands make; answers with canned values."""

    def __init__(self, *, folder: Path | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._folder = folder
        self._error = error

    def get_known_folder(self, folder: KnownFolder | str) -> Path | None:
        self.calls.append(("get_known_folder", folder))
        if self._error is not None:
            raise self._error
        return self._folder

    def get_shortcut(self, path: str) -> ShortcutInfo:
        self.calls.append(("get_shortcut", path))
        if self._error is not None:
            raise self._error
        return ShortcutInfo(path=path, target="C:\\Tools\\tool.exe", arguments="--x", description="d")

    def new_shortcut(
        self,
        path: str,
        *,
        target: str,
        arguments: Sequence[str] = (),
        working_directory: str | None = None,
        icon: str | None = None,
        icon_index: int = 0,
        description: str | None = None,
        force: bool = False,
    ) -> ShortcutInfo:
        self.calls.append(
            (
                "new_shortcut",
                path,
                {
                    "target": target,
                    "arguments": arguments,
                    "working_directory": working_directory,
                    "icon": icon,
                    "icon_index": icon_index,
                    "description": description,
                    "force": force,
                },
            )
        )
        if self._error is not None:
            raise self._error
        return ShortcutInfo(path=path, target=target, arguments=" ".join(arguments), description=description or "")


def _invoke(fake: _FakePs, args: list[str]) -> Any:
    services = services_with_ps(fake)
    return CliRunner().invoke(cli, args, obj=lambda: services)


def _from_json_array(out: str) -> Any:
    """The one record a ``--json`` run printed."""
    return orjson.loads(out)[0]


def _from_jsonl(out: str) -> Any:
    """The one record a ``--jsonl`` run printed."""
    return orjson.loads(out.splitlines()[0])


# --- get_known_folder ----------------------------------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize("member", list(KnownFolder), ids=lambda member: member.value)
@pytest.mark.parametrize("spell", [str, str.lower, str.upper], ids=["exact", "lower", "upper"])
def test_get_known_folder_takes_every_value_in_any_case_and_passes_the_member(member: KnownFolder, spell: Any) -> None:
    """FOLDER is a .NET SpecialFolder name in any case, and the facade receives the enum member."""
    fake = _FakePs(folder=Path("/some/folder"))
    result = _invoke(fake, ["get_known_folder", spell(member.value)])
    assert result.exit_code == 0, result.output
    assert fake.calls == [("get_known_folder", member)]
    assert type(fake.calls[0][1]) is KnownFolder


@pytest.mark.os_agnostic
def test_get_known_folder_accepts_exactly_the_dotnet_names() -> None:
    """The accepted spellings are the six .NET names (the values), never the Python member names."""
    fake = _FakePs(folder=Path("/x"))
    (folder_param,) = [param for param in cli.commands["get_known_folder"].params if param.name == "folder"]
    assert isinstance(folder_param.type, click.Choice)
    assert list(folder_param.type.choices) == [
        "ApplicationData",
        "CommonApplicationData",
        "Desktop",
        "LocalApplicationData",
        "MyDocuments",
        "Programs",
    ]
    assert _invoke(fake, ["get_known_folder", "DOCUMENTS"]).exit_code == 2
    assert _invoke(fake, ["get_known_folder", "ROAMING_APP_DATA"]).exit_code == 2


@pytest.mark.os_agnostic
def test_get_known_folder_refuses_an_unknown_folder_as_a_usage_error() -> None:
    """An unknown FOLDER is click's usage error (exit 2) and never reaches the facade."""
    fake = _FakePs(folder=Path("/x"))
    result = _invoke(fake, ["get_known_folder", "Downloads"])
    assert result.exit_code == 2
    assert "Downloads" in result.output
    assert fake.calls == []


@pytest.mark.os_agnostic
def test_get_known_folder_prints_the_path_on_one_line() -> None:
    folder = Path("/home/me") / ("Schreibtisch " + _BEYOND_CP1252)
    result = _invoke(_FakePs(folder=folder), ["get_known_folder", "Desktop"])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"{folder}\n"


@pytest.mark.os_agnostic
def test_get_known_folder_json_prints_the_path_as_a_json_string() -> None:
    folder = Path("/home/me/Desktop")
    result = _invoke(_FakePs(folder=folder), ["get_known_folder", "desktop", "--json"])
    assert result.exit_code == 0, result.output
    assert orjson.loads(result.stdout) == str(folder)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("flags", [[], ["--json"]], ids=["text", "json"])
def test_get_known_folder_with_no_folder_exits_1_with_a_stderr_line(flags: list[str]) -> None:
    """None from the facade (missing or unreadable folder) is exit 1, one stderr line, empty stdout."""
    result = _invoke(_FakePs(folder=None), ["get_known_folder", "MyDocuments", *flags])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    assert "MyDocuments" in result.stderr


@pytest.mark.os_agnostic
def test_get_known_folder_platform_refusal_leaves_through_main_as_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    """A PwshPyError (Programs off Windows) leaves through main() as one stderr line and exit 1."""
    services = services_with_ps(_FakePs(error=PlatformUnsupportedError("Programs has no folder on linux")))
    rc = main(["get_known_folder", "Programs"], services_factory=lambda: services)
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.strip() == "PlatformUnsupportedError: Programs has no folder on linux"
    assert "Traceback" not in captured.err + captured.out


# --- get_shortcut --------------------------------------------------------------------------------


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("flags", "parse"),
    [(["--json"], _from_json_array), (["--jsonl"], _from_jsonl)],
    ids=["json", "jsonl"],
)
def test_get_shortcut_emits_the_record(flags: list[str], parse: Callable[[str], Any]) -> None:
    fake = _FakePs()
    result = _invoke(fake, ["get_shortcut", "C:\\x\\t.lnk", *flags])
    assert result.exit_code == 0, result.output
    assert fake.calls == [("get_shortcut", "C:\\x\\t.lnk")]
    record = parse(result.stdout)
    assert record["target"] == "C:\\Tools\\tool.exe"
    assert record["arguments"] == "--x"


@pytest.mark.os_agnostic
def test_get_shortcut_human_table_shows_the_target() -> None:
    result = _invoke(_FakePs(), ["get_shortcut", "C:\\x\\t.lnk"])
    assert result.exit_code == 0, result.output
    assert "tool.exe" in result.stdout


@pytest.mark.os_agnostic
def test_get_shortcut_refusal_leaves_through_main_as_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    services = services_with_ps(_FakePs(error=NativeCallError("no shortcut at C:\\x\\t.lnk")))
    rc = main(["get_shortcut", "C:\\x\\t.lnk"], services_factory=lambda: services)
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.strip() == "NativeCallError: no shortcut at C:\\x\\t.lnk"


# --- new_shortcut --------------------------------------------------------------------------------


@pytest.mark.os_agnostic
def test_new_shortcut_passes_the_argv_after_the_separator_unsplit() -> None:
    """Every word after -- reaches the facade as one tuple item, spaces and leading dashes kept."""
    fake = _FakePs()
    args = ["new_shortcut", "C:\\x\\t.lnk", "--target", "C:\\uv.exe", "--", "--exclude-newer", "7 days", "lsdsk"]
    result = _invoke(fake, args)
    assert result.exit_code == 0, result.output
    assert fake.calls[0][2]["arguments"] == ("--exclude-newer", "7 days", "lsdsk")


@pytest.mark.os_agnostic
def test_new_shortcut_passes_every_option() -> None:
    fake = _FakePs()
    args = [
        "new_shortcut",
        "C:\\x\\t.lnk",
        "--target",
        "C:\\t.exe",
        "--working-directory",
        "C:\\w",
        "--icon",
        "C:\\i.ico",
        "--icon-index",
        "-3",
        "--description",
        "tip",
        "--force",
    ]
    result = _invoke(fake, args)
    assert result.exit_code == 0, result.output
    assert fake.calls == [
        (
            "new_shortcut",
            "C:\\x\\t.lnk",
            {
                "target": "C:\\t.exe",
                "arguments": (),
                "working_directory": "C:\\w",
                "icon": "C:\\i.ico",
                "icon_index": -3,
                "description": "tip",
                "force": True,
            },
        )
    ]


@pytest.mark.os_agnostic
def test_new_shortcut_defaults_pass_no_optional_field_and_no_force() -> None:
    fake = _FakePs()
    result = _invoke(fake, ["new_shortcut", "C:\\x\\t.lnk", "--target", "C:\\t.exe"])
    assert result.exit_code == 0, result.output
    assert fake.calls[0][2] == {
        "target": "C:\\t.exe",
        "arguments": (),
        "working_directory": None,
        "icon": None,
        "icon_index": 0,
        "description": None,
        "force": False,
    }


@pytest.mark.os_agnostic
def test_new_shortcut_requires_a_target() -> None:
    fake = _FakePs()
    result = _invoke(fake, ["new_shortcut", "C:\\x\\t.lnk"])
    assert result.exit_code == 2
    assert fake.calls == []


@pytest.mark.os_agnostic
def test_new_shortcut_prints_one_status_line_naming_what_windows_stored() -> None:
    result = _invoke(_FakePs(), ["new_shortcut", "C:\\x\\t.lnk", "--target", "C:\\t.exe"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "created shortcut C:\\x\\t.lnk -> C:\\t.exe\n"


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("flags", "parse"),
    [(["--json"], _from_json_array), (["--jsonl"], _from_jsonl)],
    ids=["json", "jsonl"],
)
def test_new_shortcut_json_emits_only_the_stored_record(flags: list[str], parse: Callable[[str], Any]) -> None:
    """--json/--jsonl replace the status line with the record (like exec and invoke_web_request)."""
    args = ["new_shortcut", "C:\\x\\t.lnk", "--target", "C:\\t.exe", "--description", "tip", *flags]
    result = _invoke(_FakePs(), args)
    assert result.exit_code == 0, result.output
    assert "created shortcut" not in result.stdout
    assert parse(result.stdout)["description"] == "tip"


@pytest.mark.os_agnostic
def test_new_shortcut_refuses_json_and_jsonl_together() -> None:
    fake = _FakePs()
    args = ["new_shortcut", "C:\\x\\t.lnk", "--target", "C:\\t.exe", "--json", "--jsonl"]
    assert _invoke(fake, args).exit_code == 2
    assert fake.calls == []


@pytest.mark.os_agnostic
def test_new_shortcut_existing_file_leaves_through_main_as_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    services = services_with_ps(_FakePs(error=NativeCallError("C:\\x\\t.lnk already exists; pass force=True")))
    rc = main(["new_shortcut", "C:\\x\\t.lnk", "--target", "C:\\t.exe"], services_factory=lambda: services)
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.strip() == "NativeCallError: C:\\x\\t.lnk already exists; pass force=True"
    assert captured.out == ""


# --- piped output is UTF-8 whatever the console code page ----------------------------------------


def _run_pwshpy(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    """Run ``python -m pwshpy ARGS`` with a cp1252 stdio, capturing raw bytes (a pipe, not a tty)."""
    # env= REPLACES the environment, so the caller starts from the full one (Windows needs SystemRoot).
    return subprocess.run(  # noqa: S603 - fixed argv: this interpreter running pwshpy itself
        [sys.executable, "-m", "pwshpy", *args],
        capture_output=True,
        env={**env, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
        timeout=120,
        check=False,
    )


@pytest.mark.os_posix
@pytest.mark.skipif(sys.platform == "win32", reason="steers the POSIX resolver through XDG / CoreFoundation env vars")
def test_piped_get_known_folder_with_a_beyond_cp1252_path_is_utf8(tmp_path: Path) -> None:
    """The real resolver's non-cp1252 path prints as UTF-8 bytes through a cp1252 stdout."""
    home = tmp_path / ("home " + _BEYOND_CP1252)
    config = home / ".config"
    (home / "Library" / "Application Support").mkdir(parents=True)
    config.mkdir()
    env = {**os.environ, "HOME": str(home), "CFFIXED_USER_HOME": str(home), "XDG_CONFIG_HOME": str(config)}
    expected = posix_known_folder(KnownFolder.ROAMING_APP_DATA, environ=env)
    assert expected is not None
    assert _BEYOND_CP1252 in str(expected)  # else the test would prove nothing about the encoding
    completed = _run_pwshpy("get_known_folder", "applicationdata", env=env)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stdout == f"{expected}\n".encode()


@pytest.mark.os_windows
@pytest.mark.skipif(sys.platform != "win32", reason="writes and reads a real .lnk through IShellLinkW")
def test_piped_new_and_get_shortcut_with_beyond_cp1252_text_are_utf8(tmp_path: Path) -> None:
    """A non-cp1252 .lnk name and description print as UTF-8 in the status line and the JSON."""
    lnk = tmp_path / ("t " + _BEYOND_CP1252 + ".lnk")
    target = str(Path(os.environ["SYSTEMROOT"]) / "System32" / "notepad.exe")
    env = dict(os.environ)
    created = _run_pwshpy(
        "new_shortcut", str(lnk), "--target", target, "--description", _BEYOND_CP1252, "--", "a b", env=env
    )
    assert created.returncode == 0, created.stderr.decode("utf-8", errors="replace")
    read = _run_pwshpy("get_shortcut", str(lnk), "--json", env=env)
    assert read.returncode == 0, read.stderr.decode("utf-8", errors="replace")
    record = orjson.loads(read.stdout)[0]
    assert (record["path"], record["description"], record["arguments"]) == (str(lnk), _BEYOND_CP1252, '"a b"')
    assert record["target"].casefold() == target.casefold()
    # The status line names what Windows stored, so it is compared with the read-back record.
    assert created.stdout == f"created shortcut {record['path']} -> {record['target']}\n".encode()
