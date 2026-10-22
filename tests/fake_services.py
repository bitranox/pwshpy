"""The composition's services with the ``ps`` facade replaced by a test double, typed in one place.

Imported by bare module name (``from fake_services import ...``): ``tests/`` has no ``__init__.py``,
so pytest puts this directory on ``sys.path`` for the test files beside it.

Contents:
    * :func:`services_with_ps` - :func:`build_testing` with ``ps`` swapped for a double.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, cast

from pwshpy.composition import AppServices, build_testing

if TYPE_CHECKING:
    from pwshpy.composition.facade import Ps


def services_with_ps(fake_ps: object) -> AppServices:
    """:func:`build_testing` with ``ps`` replaced by ``fake_ps``, a double of the facade.

    A CLI test's double implements only the facade methods its commands call, so it is no ``Ps``
    and never subclasses one (that would hand every other method the real, host-touching
    implementation). The cast states that duck-typed contract once, here, instead of a type-checker
    suppression at every call site; a command calling a method the double lacks fails the test with
    an AttributeError, which is the check the cast cannot make.

    Example:
        >>> services_with_ps(object()).ps.__class__ is object
        True
    """
    return dataclasses.replace(build_testing(), ps=cast("Ps", fake_ps))


__all__ = ["services_with_ps"]
