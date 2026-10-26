"""Shortcut value objects: the ShortcutInfo record and the ShortcutSpec refusals (pure domain)."""

from __future__ import annotations

import dataclasses
import subprocess

import pytest
from pydantic import ValidationError

from pwshpy.domain.errors import ConfigurationError
from pwshpy.domain.records import ShortcutInfo
from pwshpy.domain.shortcuts import ShortcutSpec

_LNK = "C:\\Users\\me\\Desktop\\Tool.lnk"
_TARGET = "C:\\Tools\\tool.exe"
_INT32_MAX = 2**31 - 1
_INT32_MIN = -(2**31)
_LINE_BREAKS = [("NUL", "\x00"), ("CR", "\r"), ("LF", "\n")]
_OPTIONAL_FIELDS = ["working_directory", "icon", "description"]


def _spec(**overrides: object) -> ShortcutSpec:
    """Build a spec from valid defaults, replacing only the fields a test is about."""
    fields: dict[str, object] = {"path": _LNK, "target": _TARGET, **overrides}
    return ShortcutSpec(**fields)  # pyright: ignore[reportArgumentType] - wrong types ARE the input under test


# ======================== ShortcutInfo ========================


@pytest.mark.os_agnostic
def test_shortcut_info_serializes_every_field() -> None:
    info = ShortcutInfo(
        path=_LNK,
        target=_TARGET,
        arguments='--mode "a b"',
        working_directory="C:\\Tools",
        icon="C:\\Tools\\tool.ico",
        icon_index=-3,
        description="Run the tool",
        run_as_administrator=True,
    )
    assert info.to_dict() == {
        "path": _LNK,
        "target": _TARGET,
        "arguments": '--mode "a b"',
        "working_directory": "C:\\Tools",
        "icon": "C:\\Tools\\tool.ico",
        "icon_index": -3,
        "description": "Run the tool",
        "run_as_administrator": True,
    }


@pytest.mark.os_agnostic
def test_shortcut_info_unset_fields_default_to_what_com_reads_back() -> None:
    """COM reads an unset field back as "" and an unset icon index as 0."""
    info = ShortcutInfo(path=_LNK, target=_TARGET)
    assert (
        info.arguments,
        info.working_directory,
        info.icon,
        info.icon_index,
        info.description,
        info.run_as_administrator,
    ) == ("", "", "", 0, "", False)


@pytest.mark.os_agnostic
def test_shortcut_info_is_frozen() -> None:
    info = ShortcutInfo(path=_LNK, target=_TARGET)
    with pytest.raises(ValidationError, match="frozen_instance"):
        info.target = "C:\\other.exe"  # pyright: ignore[reportAttributeAccessIssue] - assignment IS the test


# ======================== ShortcutSpec: accepted shapes ========================


@pytest.mark.os_agnostic
def test_minimal_spec_defaults() -> None:
    spec = ShortcutSpec(path=_LNK, target=_TARGET)
    assert spec.arguments == ()
    assert spec.working_directory is None
    assert spec.icon is None
    assert spec.icon_index == 0
    assert spec.description is None
    assert spec.run_as_administrator is False
    assert spec.joined_arguments == ""


@pytest.mark.os_agnostic
def test_arguments_list_is_stored_as_a_tuple_and_joined_like_windows_quotes_it() -> None:
    argv = ["--name", "a b", 'say "hi"', ""]
    spec = ShortcutSpec(path=_LNK, target=_TARGET, arguments=argv)
    assert spec.arguments == tuple(argv)
    assert type(spec.arguments) is tuple
    assert spec.joined_arguments == subprocess.list2cmdline(argv)
    assert spec.joined_arguments == '--name "a b" "say \\"hi\\"" ""'


@pytest.mark.os_agnostic
def test_spec_from_a_list_equals_spec_from_a_tuple_and_hashes() -> None:
    a = ShortcutSpec(path=_LNK, target=_TARGET, arguments=["x", "y"])
    b = ShortcutSpec(path=_LNK, target=_TARGET, arguments=("x", "y"))
    assert a == b
    assert hash(a) == hash(b)


@pytest.mark.os_agnostic
def test_spec_is_frozen() -> None:
    spec = ShortcutSpec(path=_LNK, target=_TARGET)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.target = "C:\\other.exe"  # pyright: ignore[reportAttributeAccessIssue] - assignment IS the test


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "target",
    [
        "C:\\Windows\\notepad.exe",
        "c:/Windows/notepad.exe",
        "\\\\server\\share\\tool.exe",
        "%SystemRoot%\\notepad.exe",
        "%ProgramFiles(x86)%\\App\\app.exe",
        "%windir%\\system32\\cmd.exe",
    ],
)
def test_absolute_and_environment_relative_targets_are_accepted(target: str) -> None:
    assert ShortcutSpec(path=_LNK, target=target).target == target


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "path",
    [_LNK, "C:\\Users\\me\\Desktop\\TOOL.LNK", "\\\\server\\share\\Tool.Lnk", "C:/Users/me/Tool.lnk"],
)
def test_absolute_lnk_paths_are_accepted_case_insensitively(path: str) -> None:
    assert ShortcutSpec(path=path, target=_TARGET).path == path


@pytest.mark.os_agnostic
@pytest.mark.parametrize("field", _OPTIONAL_FIELDS)
def test_empty_optional_text_means_unset(field: str) -> None:
    """COM cannot tell "" from unset, so "" is stored as None: one spelling of unset."""
    assert getattr(_spec(**{field: ""}), field) is None


@pytest.mark.os_agnostic
@pytest.mark.parametrize("field", _OPTIONAL_FIELDS)
def test_optional_text_is_kept(field: str) -> None:
    assert getattr(_spec(**{field: "C:\\Tools"}), field) == "C:\\Tools"


@pytest.mark.os_agnostic
@pytest.mark.parametrize("index", [0, 7, -3, _INT32_MAX, _INT32_MIN])
def test_icon_index_accepts_the_int32_range_including_negatives(index: int) -> None:
    assert ShortcutSpec(path=_LNK, target=_TARGET, icon_index=index).icon_index == index


# ======================== ShortcutSpec: refusals ========================


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "target",
    [
        "",
        "notepad.exe",
        "Windows\\notepad.exe",
        "C:notepad.exe",
        "\\Windows\\notepad.exe",
        "%SystemRoot%",
        "%SystemRoot%notepad.exe",
        "%%\\notepad.exe",
        "x%SystemRoot%\\notepad.exe",
        "%System=Root%\\notepad.exe",
    ],
)
def test_relative_target_is_refused(target: str) -> None:
    with pytest.raises(ConfigurationError, match="target"):
        ShortcutSpec(path=_LNK, target=target)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("path", ["Tool.lnk", "Desktop\\Tool.lnk", "C:Tool.lnk", "\\Users\\Tool.lnk", ""])
def test_relative_lnk_path_is_refused(path: str) -> None:
    with pytest.raises(ConfigurationError, match="absolute"):
        ShortcutSpec(path=path, target=_TARGET)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    "path",
    [
        "C:\\Users\\me\\Tool.txt",
        "C:\\Users\\me\\Tool.lnk.txt",
        "C:\\Users\\me\\Tool",
        "C:\\Users\\me\\.lnk",
        "C:\\Users\\me\\Tool.lnk\\",
        "C:\\Users\\me\\Tool.lnk.",
        "C:\\Users\\me\\Tool.lnk ",
    ],
)
def test_non_lnk_path_is_refused(path: str) -> None:
    with pytest.raises(ConfigurationError, match=r"\.lnk"):
        ShortcutSpec(path=path, target=_TARGET)


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("label", "char"), _LINE_BREAKS)
@pytest.mark.parametrize("field", ["path", "target", *_OPTIONAL_FIELDS])
def test_nul_cr_lf_in_a_text_field_is_refused(field: str, label: str, char: str) -> None:
    valid = {"path": _LNK, "target": _TARGET}.get(field, "C:\\Tools\\x.ico")
    value = valid[:-4] + char + valid[-4:]
    with pytest.raises(ConfigurationError, match=f"{field}.*{label}"):
        _spec(**{field: value})


@pytest.mark.os_agnostic
@pytest.mark.parametrize(("label", "char"), _LINE_BREAKS)
def test_nul_cr_lf_in_an_argument_is_refused(label: str, char: str) -> None:
    with pytest.raises(ConfigurationError, match=f"argument 2.*{label}"):
        ShortcutSpec(path=_LNK, target=_TARGET, arguments=["ok", f"bad{char}value"])


@pytest.mark.os_agnostic
def test_a_bare_string_of_arguments_is_refused_never_split() -> None:
    with pytest.raises(ConfigurationError, match="not a single string"):
        ShortcutSpec(path=_LNK, target=_TARGET, arguments="--mode fast")


@pytest.mark.os_agnostic
@pytest.mark.parametrize("arguments", [[1], ["ok", None], b"--mode"])
def test_non_string_argument_elements_are_refused(arguments: object) -> None:
    with pytest.raises(ConfigurationError, match="argument"):
        _spec(arguments=arguments)


@pytest.mark.os_agnostic
def test_an_iterator_of_arguments_is_refused() -> None:
    """A generator is consumed by one look; the spec needs a re-readable sequence."""
    with pytest.raises(ConfigurationError, match="sequence"):
        _spec(arguments=(a for a in ["x"]))


@pytest.mark.os_agnostic
@pytest.mark.parametrize("index", [_INT32_MAX + 1, _INT32_MIN - 1])
def test_icon_index_outside_int32_is_refused(index: int) -> None:
    with pytest.raises(ConfigurationError, match="icon_index"):
        ShortcutSpec(path=_LNK, target=_TARGET, icon_index=index)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("index", [True, False, "1", 1.0, None])
def test_icon_index_that_is_not_a_plain_int_is_refused(index: object) -> None:
    with pytest.raises(ConfigurationError, match="icon_index"):
        _spec(icon_index=index)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("value", [True, False])
def test_run_as_administrator_accepts_a_bool(value: bool) -> None:
    assert _spec(run_as_administrator=value).run_as_administrator is value


@pytest.mark.os_agnostic
@pytest.mark.parametrize("value", [1, 0, "yes", "no", "", None])
def test_run_as_administrator_that_is_not_a_bool_is_refused(value: object) -> None:
    """A truthy str such as "no" must never turn on elevation."""
    with pytest.raises(ConfigurationError, match="run_as_administrator must be a bool"):
        _spec(run_as_administrator=value)


@pytest.mark.os_agnostic
@pytest.mark.parametrize("field", ["path", "target", *_OPTIONAL_FIELDS])
def test_a_text_field_that_is_not_a_string_is_refused(field: str) -> None:
    with pytest.raises(ConfigurationError, match=field):
        _spec(**{field: 42})


@pytest.mark.os_agnostic
@pytest.mark.parametrize("field", ["path", "target"])
def test_a_required_text_field_of_none_is_refused(field: str) -> None:
    with pytest.raises(ConfigurationError, match=field):
        _spec(**{field: None})


def _with_length(template: str, length: int, suffix: str) -> str:
    """``template`` padded with ``x`` so the result is exactly ``length`` characters."""
    return template + "x" * (length - len(template) - len(suffix)) + suffix


@pytest.mark.os_agnostic
@pytest.mark.parametrize(
    ("field", "limit", "prefix", "suffix"),
    [
        ("path", 259, "C:\\", ".lnk"),
        ("target", 259, "C:\\", ".exe"),
        ("working_directory", 260, "C:\\", ""),
        ("icon", 260, "C:\\", ".ico"),
        ("description", 260, "", ""),
    ],
)
def test_text_fields_are_bounded(field: str, limit: int, prefix: str, suffix: str) -> None:
    at_limit = _with_length(prefix, limit, suffix)
    assert getattr(_spec(**{field: at_limit}), field) == at_limit
    over = _with_length(prefix, limit + 1, suffix)
    with pytest.raises(ConfigurationError, match=f"{field}.*{limit + 1}.*{limit}") as caught:
        _spec(**{field: over})
    # The refusal names the lengths, never echoes a value that may be thousands of characters.
    assert over not in str(caught.value)


@pytest.mark.os_agnostic
def test_joined_arguments_are_bounded_at_32767() -> None:
    at_limit = ["a" * 32767]
    assert len(ShortcutSpec(path=_LNK, target=_TARGET, arguments=at_limit).joined_arguments) == 32767
    # Unquoted these two would join to exactly 32767; the quotes around the one with a space push it over.
    with pytest.raises(ConfigurationError, match=r"arguments.*32768.*32767"):
        ShortcutSpec(path=_LNK, target=_TARGET, arguments=["a" * 32768])
    with pytest.raises(ConfigurationError, match="arguments"):
        ShortcutSpec(path=_LNK, target=_TARGET, arguments=["a" * 16383, "b c" + "d" * 16380])


@pytest.mark.os_agnostic
def test_refusals_are_configuration_errors_not_pydantic_errors() -> None:
    """The facade and CLI map ConfigurationError; a Pydantic ValidationError would escape them."""
    with pytest.raises(ConfigurationError) as caught:
        ShortcutSpec(path="Tool.lnk", target=_TARGET)
    assert type(caught.value) is ConfigurationError
