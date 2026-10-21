"""Whether this test run may reach a session bus, decided from the PROCESS environment alone.

``tests/conftest.py`` points ``DBUS_SESSION_BUS_ADDRESS`` at a dead address unless the run opts in
with ``PWSHPY_TEST_SECRET_SERVICE=1`` (see ``_keep_off_the_session_bus`` there). The conftest also
loads the repo's ``.env`` at import, and a value that only a ``.env`` line supplies must never
switch the guard off: a stray line in a developer's ``.env`` would otherwise expose the desktop
keyring to every later gate run without anyone having asked for it. So the opt-in is read once,
here, when the conftest imports this module - before its ``.env`` load runs - and every reader
(the conftest and the tests that skip on it) uses :data:`PROCESS_OPTED_IN` instead of
``os.environ``.

Imported by bare module name (``import session_bus_isolation``): ``tests/`` has no ``__init__.py``.
"""

from __future__ import annotations

import os
from typing import Final

__all__ = ["GUARD_MARKER", "PROCESS_OPTED_IN", "SECRET_SERVICE_OPT_IN", "SESSION_BUS_VAR"]

#: Opts a run in to whatever session bus the environment provides.
SECRET_SERVICE_OPT_IN: Final = "PWSHPY_TEST_SECRET_SERVICE"
#: The variable the guard replaces; the SYSTEM bus variable is never touched.
SESSION_BUS_VAR: Final = "DBUS_SESSION_BUS_ADDRESS"
#: Part of every dead address the guard sets, so a test can tell the guard address from a real one.
GUARD_MARKER: Final = "pwshpy-tests-no-session-bus"

#: True only when the process that started pytest set the opt-in itself (never via ``.env``).
PROCESS_OPTED_IN: Final = os.environ.get(SECRET_SERVICE_OPT_IN) == "1"
