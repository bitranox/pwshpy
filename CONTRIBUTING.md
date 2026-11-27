# Contributing Guide

Thanks for helping improve **pwshpy**. The sections below summarise the day-to-day workflow, highlight the repository automation, and list the checks that must pass before a change is merged.

## 1. Workflow Overview

1. Fork and branch -- use short, imperative branch names (`feature/cli-extension`, `fix/codecov-token`).
2. Make focused commits -- keep unrelated refactors out of the same change.
3. Run `make test` locally before pushing (see the automation note below).
4. Update documentation and changelog entries that are affected by the change.
5. Open a pull request referencing any relevant issues.

## 2. Commits & Pushes

- Commit messages should be imperative (`Add rich handler`, `Fix CLI exit codes`).
- The test harness (`make test`) makes no commit. It does rewrite files: it raises outdated dependency floors in `pyproject.toml`, refreshes `uv.lock` and applies `ruff format` and `ruff check --fix`, so review `git diff` after a run.
- `make push` always performs a commit before pushing: it stages every change (`git add -A`) and creates an empty commit if nothing is staged. Pass the message as `MSG="..."` (safe for any punctuation and for newlines); without one it prompts at a terminal and uses `chores` otherwise.

## 3. Coding Standards

- Keep the Clean Architecture layering (domain, application, adapters, composition). The import-linter contracts under `[tool.importlinter]` in `pyproject.toml` define which layer may import which, and `make test` enforces them.
- Prefer small, single-purpose modules and functions; avoid mixing orthogonal concerns.
- Free functions and modules use `snake_case`; classes are `PascalCase`.
- Keep runtime dependencies minimal. Use the standard library where practical.

## 4. Tests & Tooling

- `make test` runs Ruff (lint + format check), Pyright, import-linter, pip-audit, bandit and Pytest with coverage (see [DEVELOPMENT.md](DEVELOPMENT.md#target-details) for the full list). bmk provisions the project's `.venv` with the dev extras itself, so no separate install step is needed.
- The Codecov upload makes no commit. It runs when `CODECOV_TOKEN` is set in your environment or `.env` and `codecovcli` is on `PATH`; otherwise it is skipped.
- Tests follow a narrative style: prefer names like `test_when_<condition>_<outcome>()`, keep each case laser-focused, and mark OS constraints with the provided markers (`@pytest.mark.os_agnostic`, `@pytest.mark.os_windows`, etc.).
- Whenever you add a CLI behaviour or change metadata fallbacks, update the relevant story in the matching `tests/test_cli_*.py` module or `tests/test_metadata.py` so the specification remains complete.

## 5. Documentation Checklist

Before opening a PR, confirm the following:

- [ ] `make test` passes locally.
- [ ] Relevant documentation (`README.md`, `COMMANDS.md`, `docs/*.md`, `CHANGELOG.md`, `skills/using-pwsh/SKILL.md`) is updated.
- [ ] No generated artefacts or virtual environments are committed.
- [ ] Version bumps, when required, go through `make bump-patch`, `make bump-minor` or `make bump-major`, which update `pyproject.toml`, `CHANGELOG.md` and `src/pwshpy/__init__conf__.py` together.

## 6. Security & Configuration

- Never commit secrets. Tokens (Codecov, PyPI) belong in `.env` (ignored by git) or CI secrets.
- Sanitise any payloads you emit via logging once richer logging features ship.

Happy hacking!
