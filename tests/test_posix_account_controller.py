"""Live POSIX local-account controller test - creates a SCRATCH user + group, root only.

Drives new/enable/disable + group membership + remove through PosixLocalAccountController against
throwaway accounts, verifying via pwd/grp and get_local_user. local_only + Linux + root, so it runs
on the dev box, never in CI.
"""
# subprocess is the belt-and-braces teardown (userdel/groupdel) if the controller left state behind.

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from collections.abc import Iterator

import pytest

from pwshpy import ps
from pwshpy.adapters.native.posix_account_control import PosixLocalAccountController

_USER = "pwshpy-pytest-user"
_GROUP = "pwshpy-pytest-group"

pytestmark = [
    pytest.mark.local_only,
    pytest.mark.os_linux,
    pytest.mark.skipif(
        not sys.platform.startswith("linux") or os.geteuid() != 0,  # type: ignore[attr-defined]
        reason="local-account mutation needs Linux + root",
    ),
]


def _group_members(name: str) -> list[str]:
    return list(importlib.import_module("grp").getgrnam(name).gr_mem)


@pytest.fixture
def cleanup() -> Iterator[None]:
    """Remove the scratch user + group on teardown, whatever the test left behind."""
    try:
        yield
    finally:
        subprocess.run(["userdel", _USER], check=False)  # noqa: S603, S607
        subprocess.run(["groupdel", _GROUP], check=False)  # noqa: S603, S607


def test_user_and_group_lifecycle(cleanup: None) -> None:
    """new_user (locked) -> appears; enable; new_group; add/remove member; remove -> gone."""
    controller = PosixLocalAccountController()

    created = controller.new_user(_USER, full_name="Py Test", disabled=True)
    assert created.name == _USER
    assert created.full_name == "Py Test"
    assert created.enabled is False  # created locked
    assert _USER in {u.name for u in ps.get_local_user()}  # visible to the read side

    enabled = controller.set_user_enabled(_USER, enabled=True)
    assert enabled.enabled is True

    group = controller.new_group(_GROUP)
    assert group.name == _GROUP
    assert _GROUP in {g.name for g in ps.get_local_group()}

    controller.add_group_member(_GROUP, _USER)
    assert _USER in _group_members(_GROUP)
    controller.remove_group_member(_GROUP, _USER)
    assert _USER not in _group_members(_GROUP)

    controller.remove_user(_USER)
    controller.remove_group(_GROUP)
    assert _USER not in {u.name for u in ps.get_local_user()}
    assert _GROUP not in {g.name for g in ps.get_local_group()}
