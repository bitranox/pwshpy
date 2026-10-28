"""Domain error types: instantiation and message preservation."""

from __future__ import annotations

import ast
import builtins
from collections.abc import Callable
from pathlib import Path

import pytest
from exception_round_trips import deepcopy_round_trip, pickle_round_trip

import pwshpy
from pwshpy.adapters.logging.setup import InvalidLoggingConfigError
from pwshpy.domain.errors import ConfigurationError, NativeCallError, OperationTimeoutError


@pytest.mark.os_agnostic
def test_configuration_error_preserves_message() -> None:
    """Instantiation stores the message for display."""
    exc = ConfigurationError("output format not configured")
    assert str(exc) == "output format not configured"


_ROUND_TRIPS: list[Callable[[BaseException], BaseException]] = [pickle_round_trip, deepcopy_round_trip]


class _LabelledTimeoutError(OperationTimeoutError):
    """A subclass with its own keyword and attribute: pickling must keep both, and the type."""

    def __init__(self, message: str, *, timeout: float, label: str) -> None:
        super().__init__(message, timeout=timeout)
        self.label = label


@pytest.mark.os_agnostic
@pytest.mark.parametrize("round_trip", _ROUND_TRIPS, ids=["pickle", "deepcopy"])
def test_operation_timeout_error_survives_a_round_trip(round_trip: Callable[[BaseException], BaseException]) -> None:
    """The keyword-only ``timeout`` is not in ``args``, so the default reduce cannot rebuild it."""
    rebuilt = round_trip(OperationTimeoutError("slow call", timeout=1.5))
    assert type(rebuilt) is OperationTimeoutError
    assert isinstance(rebuilt, OperationTimeoutError)
    assert rebuilt.timeout == 1.5
    assert str(rebuilt) == "slow call"


@pytest.mark.os_agnostic
@pytest.mark.parametrize("round_trip", _ROUND_TRIPS, ids=["pickle", "deepcopy"])
def test_an_operation_timeout_error_subclass_survives_a_round_trip(
    round_trip: Callable[[BaseException], BaseException],
) -> None:
    rebuilt = round_trip(_LabelledTimeoutError("slow call", timeout=2.0, label="wu-search"))
    assert type(rebuilt) is _LabelledTimeoutError
    assert isinstance(rebuilt, _LabelledTimeoutError)
    assert (rebuilt.timeout, rebuilt.label, str(rebuilt)) == (2.0, "wu-search", "slow call")


_PROBLEMS = ["lib_log_rich.rate_limit: Input should be a valid tuple", "lib_log_rich: Unknown log level: 'bogus'"]

#: One instance of every pwshpy exception class whose ``__init__`` differs from ``BaseException``'s.
#: Such an ``__init__`` is what breaks the default reduce, which calls ``type(self)(*self.args)``.
_CUSTOM_INIT_ERRORS: list[BaseException] = [
    NativeCallError("refused", hresult=0x80070005),
    OperationTimeoutError("slow call", timeout=1.5),
    InvalidLoggingConfigError(_PROBLEMS),
]


@pytest.mark.os_agnostic
@pytest.mark.parametrize("round_trip", _ROUND_TRIPS, ids=["pickle", "deepcopy"])
@pytest.mark.parametrize("error", _CUSTOM_INIT_ERRORS, ids=lambda error: type(error).__name__)
def test_an_error_with_its_own_init_survives_a_round_trip(
    error: BaseException,
    round_trip: Callable[[BaseException], BaseException],
) -> None:
    """Type, message, ``args`` and every instance attribute come back unchanged."""
    rebuilt = round_trip(error)
    assert type(rebuilt) is type(error)
    assert (str(rebuilt), rebuilt.args, vars(rebuilt)) == (str(error), error.args, vars(error))


@pytest.mark.os_agnostic
@pytest.mark.parametrize("round_trip", _ROUND_TRIPS, ids=["pickle", "deepcopy"])
def test_invalid_logging_config_error_keeps_its_message_after_a_round_trip(
    round_trip: Callable[[BaseException], BaseException],
) -> None:
    """The joined message is not re-joined character by character on the way back."""
    rebuilt = round_trip(InvalidLoggingConfigError(_PROBLEMS))
    assert isinstance(rebuilt, InvalidLoggingConfigError)
    assert rebuilt.problems == _PROBLEMS
    assert str(rebuilt) == "; ".join(_PROBLEMS)


def _is_exception_base(base: ast.expr, exception_names: set[str]) -> bool:
    name = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else ""
    # A third-party base (``click.ClickException``) is not a builtin; its conventional suffix names it.
    return name in exception_names or name.endswith(("Error", "Exception"))


def _exception_classes_with_own_init(package_dir: Path) -> set[str]:
    """Name every exception class under ``package_dir`` that defines ``__init__``, read from source.

    Read by AST rather than by importing, so Windows-only modules count on every OS.  Inheritance
    is followed by simple name to a fixpoint, starting from the builtin exceptions plus any base
    named ``*Error`` / ``*Exception``.
    """
    classes = [
        node
        for path in package_dir.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_bytes()))
        if isinstance(node, ast.ClassDef)
    ]
    exception_names = {
        name for name, value in vars(builtins).items() if isinstance(value, type) and issubclass(value, BaseException)
    }
    grown = True
    while grown:
        before = len(exception_names)
        exception_names |= {
            node.name for node in classes if any(_is_exception_base(base, exception_names) for base in node.bases)
        }
        grown = len(exception_names) > before
    package_exceptions = [
        node for node in classes if any(_is_exception_base(base, exception_names) for base in node.bases)
    ]
    assert len(package_exceptions) >= 10, "the scan found almost no exception classes: the instrument is broken"
    return {
        node.name
        for node in package_exceptions
        if any(isinstance(item, ast.FunctionDef) and item.name == "__init__" for item in node.body)
    }


@pytest.mark.os_agnostic
def test_every_exception_class_with_its_own_init_is_round_tripped() -> None:
    """A new exception class with its own ``__init__`` must join ``_CUSTOM_INIT_ERRORS``."""
    covered = {type(error).__name__ for error in _CUSTOM_INIT_ERRORS}
    assert _exception_classes_with_own_init(Path(pwshpy.__file__).parent) == covered
