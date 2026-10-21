"""Windows image (WIM/ESD) records: the ``WindowsImage`` record and ``ImageArchitecture`` enum."""

from __future__ import annotations

import pytest

from pwshpy import ImageArchitecture, WindowsImage


@pytest.mark.os_agnostic
def test_windows_image_record_fields() -> None:
    """Fields the XML may omit default to empty, so a sparse image entry still builds."""
    image = WindowsImage(
        index=1, name="Pro", architecture=ImageArchitecture.X64, build=26200, sp_build=8973, languages=("de-DE",)
    )
    assert image.version == "" and image.default_language == "" and image.size_bytes == 0
    assert image.languages == ("de-DE",)
    assert image.to_dict()["architecture"] == "x64"


@pytest.mark.os_agnostic
def test_windows_image_architecture_defaults_to_unknown() -> None:
    """An image without an ``<ARCH>`` element reads as UNKNOWN, never as a guessed CPU."""
    assert WindowsImage(index=2, name="Core").architecture is ImageArchitecture.UNKNOWN
