"""native script packer: os_agnostic, hermetic tests over tmp_path.

These cover what goes into a pack and what the packer refuses to do.  Whether the artefact
actually runs is the job of ``test_pack_e2e.py``, which executes it under a real pwsh.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pwshpy.adapters.native.packer import pack_script, unpack_script
from pwshpy.domain.errors import PackError
from pwshpy.domain.packing import SHIM_MODULE, PackOptions

_PEP723 = '# /// script\n# requires-python = ">=3.10"\n# dependencies = ["cowsay"]\n# ///\n'


def _project(root: Path) -> Path:
    """Build a small tree: an entry importing a package submodule and a sibling module."""
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("from . import helper\n")
    (root / "pkg" / "helper.py").write_text("VALUE = 1\n")
    (root / "sibling.py").write_text("import json\nNAME = 'sibling'\n")
    entry = root / "app.py"
    entry.write_text("import sibling\nfrom pkg import helper\nprint(helper.VALUE, sibling.NAME)\n")
    return entry


@pytest.mark.os_agnostic
def test_packs_entry_with_local_modules_and_shim(tmp_path: Path) -> None:
    """Local imports are followed transitively; the generated shim rides along."""
    manifest = pack_script(_project(tmp_path))
    assert manifest.files == [SHIM_MODULE, "app.py", "pkg/__init__.py", "pkg/helper.py", "sibling.py"]
    assert manifest.entry == "app.py"
    assert Path(manifest.output_path).is_file()


@pytest.mark.os_agnostic
def test_stdlib_imports_are_not_embedded(tmp_path: Path) -> None:
    """The standard library travels with the interpreter uv provisions."""
    entry = tmp_path / "solo.py"
    entry.write_text("import json\nimport os.path\nprint(json, os.path)\n")
    manifest = pack_script(entry)
    assert manifest.files == [SHIM_MODULE, "solo.py"]
    assert manifest.external_imports == []


@pytest.mark.os_agnostic
def test_third_party_imports_are_reported_not_embedded(tmp_path: Path) -> None:
    """External imports are surfaced for the author to declare, never guessed at."""
    entry = tmp_path / "solo.py"
    entry.write_text("import rich\nfrom yaml import safe_load\nprint(rich, safe_load)\n")
    manifest = pack_script(entry)
    assert manifest.external_imports == ["rich", "yaml"]
    assert manifest.files == [SHIM_MODULE, "solo.py"]


@pytest.mark.os_agnostic
def test_importing_a_name_from_a_local_package_is_not_reported_external(tmp_path: Path) -> None:
    """``from pkg import helper`` must not make 'pkg' look like a missing dependency.

    The candidate ``pkg.helper.VALUE`` resolves to no file because it names an attribute,
    which would otherwise mark its root external even though the package was just packed.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "helper.py").write_text("VALUE = 1\n")
    entry = tmp_path / "app.py"
    entry.write_text("from pkg.helper import VALUE\n\nprint(VALUE)\n")
    manifest = pack_script(entry)
    assert manifest.external_imports == []
    assert "pkg/helper.py" in manifest.files


@pytest.mark.os_agnostic
def test_pep723_block_is_copied_onto_the_shim(tmp_path: Path) -> None:
    """uv reads metadata from the file it runs, and it runs the shim - so the block must be there."""
    entry = tmp_path / "app.py"
    entry.write_text(_PEP723 + "import cowsay\nprint(cowsay)\n")
    manifest = pack_script(entry)
    assert manifest.has_script_metadata
    payload = _archive_of(Path(manifest.output_path))
    assert payload[SHIM_MODULE].decode().startswith(_PEP723)


@pytest.mark.os_agnostic
def test_missing_metadata_is_reported(tmp_path: Path) -> None:
    """A script with third-party imports and no block is packable but flagged."""
    entry = tmp_path / "app.py"
    entry.write_text("import cowsay\nprint(cowsay)\n")
    manifest = pack_script(entry)
    assert manifest.has_script_metadata is False
    assert manifest.external_imports == ["cowsay"]


@pytest.mark.os_agnostic
def test_include_embeds_files_import_analysis_cannot_see(tmp_path: Path) -> None:
    """A data file reached at runtime has no import to follow, so it is passed explicitly."""
    entry = tmp_path / "app.py"
    entry.write_text("print('hi')\n")
    (tmp_path / "data.json").write_text("{}\n")
    manifest = pack_script(entry, options=PackOptions(include=[tmp_path / "data.json"]))
    assert "data.json" in manifest.files


@pytest.mark.os_agnostic
def test_with_packages_become_uv_arguments(tmp_path: Path) -> None:
    """--with values are spliced into the runner's uv invocation, in order."""
    entry = tmp_path / "app.py"
    entry.write_text("print('hi')\n")
    manifest = pack_script(entry, options=PackOptions(with_packages=["rich", "httpx>=0.27"]))
    assert manifest.uv_args == ["--with", "rich", "--with", "httpx>=0.27"]
    assert "'--with','rich','--with','httpx>=0.27'" in Path(manifest.output_path).read_text()


@pytest.mark.os_agnostic
def test_pack_is_deterministic(tmp_path: Path) -> None:
    """Identical sources must pack to identical bytes, or the runner's cache key is worthless."""
    entry = _project(tmp_path)
    first = pack_script(entry, tmp_path / "a.ps1")
    second = pack_script(entry, tmp_path / "b.ps1")
    assert first.payload_sha256 == second.payload_sha256
    assert (tmp_path / "a.ps1").read_bytes() == (tmp_path / "b.ps1").read_bytes()


@pytest.mark.os_agnostic
def test_runner_has_no_unsubstituted_placeholders(tmp_path: Path) -> None:
    """The shipped artefact must never contain a literal template placeholder."""
    entry = tmp_path / "app.py"
    entry.write_text("print('hi')\n")
    manifest = pack_script(entry)
    assert "@@PWSHPY" not in Path(manifest.output_path).read_text()


@pytest.mark.os_agnostic
def test_missing_entry_raises_pack_error(tmp_path: Path) -> None:
    """A typed error, not a bare FileNotFoundError leaking out of the adapter."""
    with pytest.raises(PackError, match="entry script not found"):
        pack_script(tmp_path / "nope.py")


@pytest.mark.os_agnostic
def test_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    """Silently clobbering an existing artefact would lose work."""
    entry = tmp_path / "app.py"
    entry.write_text("print('hi')\n")
    pack_script(entry)
    with pytest.raises(PackError, match="already exists"):
        pack_script(entry)
    assert pack_script(entry, options=PackOptions(force=True)).entry == "app.py"


@pytest.mark.os_agnostic
def test_refuses_a_file_named_like_the_generated_shim(tmp_path: Path) -> None:
    """A source whose archive name collides with the shim would be silently overwritten."""
    (tmp_path / SHIM_MODULE).write_text("SHADOW = 1\n")
    entry = tmp_path / "app.py"
    entry.write_text(f"import {SHIM_MODULE[:-3]}\nprint('hi')\n")
    with pytest.raises(PackError, match="reserved for the generated entry"):
        pack_script(entry)


@pytest.mark.os_agnostic
def test_refuses_a_destination_that_is_an_input(tmp_path: Path) -> None:
    """Writing the runner over its own source would destroy the input."""
    entry = tmp_path / "app.py"
    entry.write_text("print('hi')\n")
    with pytest.raises(PackError, match="being packed"):
        pack_script(entry, entry, options=PackOptions(force=True))


@pytest.mark.os_agnostic
def test_include_outside_the_root_is_refused(tmp_path: Path) -> None:
    """An archive member must not need '..' to describe where it came from."""
    inside = tmp_path / "project"
    inside.mkdir()
    entry = inside / "app.py"
    entry.write_text("print('hi')\n")
    outsider = tmp_path / "outside.py"
    outsider.write_text("X = 1\n")
    with pytest.raises(PackError, match="outside the pack root"):
        pack_script(entry, options=PackOptions(include=[outsider]))


@pytest.mark.os_agnostic
def test_root_widens_the_pack(tmp_path: Path) -> None:
    """An explicit root lets an entry in a subdirectory import from the tree above it."""
    (tmp_path / "app").mkdir()
    (tmp_path / "shared.py").write_text("VALUE = 2\n")
    entry = tmp_path / "app" / "main.py"
    entry.write_text("import shared\nprint(shared.VALUE)\n")
    manifest = pack_script(entry, tmp_path / "out.ps1", options=PackOptions(root=tmp_path))
    assert manifest.entry == "app/main.py"
    assert "shared.py" in manifest.files


@pytest.mark.os_agnostic
def test_unpack_restores_sources_without_the_shim(tmp_path: Path) -> None:
    """What comes back is the source that went in - the shim is machinery, not source."""
    entry = _project(tmp_path)
    manifest = pack_script(entry)
    restored = unpack_script(manifest.output_path, tmp_path / "restored")
    assert restored.files == ["app.py", "pkg/__init__.py", "pkg/helper.py", "sibling.py"]
    assert restored.entry == "app.py"
    assert not (tmp_path / "restored" / SHIM_MODULE).exists()


@pytest.mark.os_agnostic
def test_unpack_round_trip_is_byte_identical(tmp_path: Path) -> None:
    """Edit-and-repack only works if unpacking gives back exactly what was packed."""
    entry = _project(tmp_path)
    manifest = pack_script(entry)
    unpack_script(manifest.output_path, tmp_path / "restored")
    for relative in ("app.py", "pkg/helper.py", "sibling.py"):
        assert (tmp_path / relative).read_bytes() == (tmp_path / "restored" / relative).read_bytes()


@pytest.mark.os_agnostic
def test_repack_of_restored_sources_matches_the_original(tmp_path: Path) -> None:
    """A pack -> unpack -> pack cycle is a fixed point when nothing was edited."""
    original = pack_script(_project(tmp_path))
    unpack_script(original.output_path, tmp_path / "restored")
    repacked = pack_script(tmp_path / "restored" / "app.py", tmp_path / "again.ps1")
    assert repacked.payload_sha256 == original.payload_sha256


@pytest.mark.os_agnostic
def test_unpack_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    """Unpacking over edited sources without asking would discard the edits."""
    entry = tmp_path / "app.py"
    entry.write_text("print('hi')\n")
    manifest = pack_script(entry)
    unpack_script(manifest.output_path, tmp_path / "out")
    with pytest.raises(PackError, match="already exists"):
        unpack_script(manifest.output_path, tmp_path / "out")
    assert unpack_script(manifest.output_path, tmp_path / "out", force=True).files == ["app.py"]


@pytest.mark.os_agnostic
def test_unpack_rejects_a_file_without_a_payload(tmp_path: Path) -> None:
    """An ordinary .ps1 is not a pack; say so plainly."""
    stray = tmp_path / "ordinary.ps1"
    stray.write_text("Write-Host 'hello'\n")
    with pytest.raises(PackError, match="does not look like a pwshpy pack"):
        unpack_script(stray, tmp_path / "out")


@pytest.mark.os_agnostic
def test_unpack_refuses_a_member_escaping_the_destination(tmp_path: Path) -> None:
    """A hand-edited pack must not be able to write outside the target directory (zip slip)."""
    entry = tmp_path / "app.py"
    entry.write_text("print('hi')\n")
    manifest = pack_script(entry)
    _replace_payload(Path(manifest.output_path), {"../escaped.py": b"X = 1\n"})
    with pytest.raises(PackError, match="outside"):
        unpack_script(manifest.output_path, tmp_path / "out")


def _archive_of(runner: Path) -> dict[str, bytes]:
    """Read the payload a generated runner carries, as a name -> bytes mapping."""
    import base64
    import io

    lines = runner.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "$PwshPyPayload = @'")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "'@")
    blob = base64.b64decode("".join(lines[start + 1 : end]))
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _replace_payload(runner: Path, members: dict[str, bytes]) -> None:
    """Swap a generated runner's payload for a hand-built archive (tamper simulation)."""
    import base64
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    encoded = base64.b64encode(buffer.getvalue()).decode()
    lines = runner.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "$PwshPyPayload = @'")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "'@")
    runner.write_text("\n".join([*lines[: start + 1], encoded, *lines[end:]]) + "\n")
