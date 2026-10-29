"""Name the home a pwsh known-folder answer sits under, for the oracle's failure messages.

The macOS known-folder oracle (``test_known_folders_pwsh_oracle``) sets up homes that differ
(a decoy HOME, the passwd entry, a CFFIXED_USER_HOME) and, when pwshpy and pwsh disagree, has to
say which of them pwsh followed: that sentence is the measurement the CI log carries. Kept
apart from the oracle so it is tested on every OS, also where pwsh is absent and the oracle
skips.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path

__all__ = ["home_followed"]


def _deepest_for(answer: Path, homes: Mapping[str, Path]) -> set[str]:
    """Return the labels of the deepest candidate homes that are an ancestor of ``answer``."""
    hits = {label: home for label, home in homes.items() if home in answer.parents}
    if not hits:
        return set()
    depth = max(len(home.parts) for home in hits.values())
    return {label for label, home in hits.items() if len(home.parts) == depth}


def _deepest_ancestors(answers: list[Path], homes: Mapping[str, Path]) -> list[str]:
    """Return, in ``homes`` order, each answer's deepest ancestor home, labelled with its path.

    Decided per answer: a split answer (one member under one home, another under a different or
    an enclosing one) names every home some answer followed, not only the deepest one overall.
    """
    chosen: set[str] = set()
    for answer in answers:
        chosen |= _deepest_for(answer, homes)
    return [f"{label} ({home})" for label, home in homes.items() if label in chosen]


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def home_followed(answers: Iterable[Path | None], homes: Mapping[str, Path]) -> str:
    """Name the candidate home pwsh's answers sit under.

    For each answer the deepest matching home wins, so a scratch home created inside another
    candidate is still told apart from it; answers split over several homes name each of them.
    Spellings are compared as given first; only when none matches are both sides resolved with
    :func:`os.path.realpath`, and such a match is marked, so a symlinked spelling (macOS ``/var``
    vs ``/private/var``) is neither mislabelled "neither" nor hidden.

    Args:
        answers: pwsh's answers per member; ``None`` (pwsh's ``""``) is skipped.
        homes: Candidate homes by label, e.g. ``{"HOME": decoy, "the passwd entry": home}``.

    Returns:
        The matching label(s) with their path, or ``"neither <labels>"``.

    Examples:
        >>> homes = {"HOME": Path("/h/a"), "the passwd entry": Path("/h/b")}
        >>> home_followed([Path("/h/a/Desktop"), None], homes).startswith("HOME (")
        True
        >>> home_followed([Path("/usr/share")], homes)
        'neither HOME nor the passwd entry'
    """
    given = [answer for answer in answers if answer is not None]
    exact = _deepest_ancestors(given, homes)
    if exact:
        return " or ".join(exact)
    resolved = _deepest_ancestors([_real(a) for a in given], {label: _real(home) for label, home in homes.items()})
    if resolved:
        return " or ".join(resolved) + " (same directory, different spelling)"
    return "neither " + " nor ".join(homes)
