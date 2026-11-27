# Development

The `Makefile` is a thin wrapper around [bmk](https://pypi.org/project/bmk/), installed once per
machine with `uv tool`. Every target runs bmk against the project's own `.venv`, which bmk creates
and syncs from `pyproject.toml` and `uv.lock`.

## Make Targets

The table is the output of `make help`:

| Target                     | Description                                                            |
|----------------------------|------------------------------------------------------------------------|
| `build`                    | Build wheel and sdist artifacts [alias: bld]                           |
| `bump`                     | Bump patch version (default for bump)                                  |
| `bump-major`               | Bump major version (X+1).0.0                                           |
| `bump-minor`               | Bump minor version X.(Y+1).0                                           |
| `bump-patch`               | Bump patch version X.Y.(Z+1)                                           |
| `clean`                    | Remove build artifacts and caches [aliases: cln, cl]                   |
| `clean-all`                | Remove build artifacts, caches AND every virtual environment (.venv*)  |
| `codecov`                  | Upload coverage report to Codecov [aliases: coverage, cov]             |
| `commit`                   | Create a git commit with timestamped message [alias: c]                |
| `config`                   | Show current merged configuration                                      |
| `config-deploy`            | Deploy configuration to system/user directories                        |
| `config-generate-examples` | Generate example configuration files                                   |
| `custom`                   | Run a custom command (make custom <name> [args...])                    |
| `dependencies`             | Check and list project dependencies [aliases: deps, d]                 |
| `dependencies-update`      | Update dependencies to latest versions                                 |
| `dev`                      | Install package with dev extras (editable)                             |
| `ensure`                   | Install missing external tools for this OS                             |
| `help`                     | Show this help                                                         |
| `info`                     | Print resolved package metadata                                        |
| `install`                  | Editable install (no dev extras)                                       |
| `logdemo`                  | Run logging demonstration                                              |
| `push`                     | Run tests, commit, and push to remote [aliases: psh, p]                |
| `release`                  | Create a versioned release (tag + GitHub release) [aliases: rel, r]    |
| `run`                      | Run the project CLI                                                    |
| `send-email`               | Send an email via configured SMTP                                      |
| `send-notification`        | Send a plain-text notification email                                   |
| `ship`                     | Push, wait for CI, release, wait for release CI (CI-gated) [alias: sh] |
| `test`                     | Run test suite [alias: t]                                              |
| `test-all`                 | Run pytest + pyright on every declared Python version (matrix)         |
| `test-human`               | Run test suite with human-readable output [alias: th]                  |
| `testintegration`          | Run integration tests only [aliases: testi, ti]                        |
| `testintegration-human`    | Run integration tests with human-readable output [alias: tih]          |
| `version-current`          | Print current version                                                  |

### Target Parameters

- **Every target**
  - Words after the target name are forwarded to bmk (`make push fix login bug`). An option such as
    `--verbose` would be parsed by make itself, so pass options as `ARGS="--verbose"`.
- **commit** / **push**
  - `MSG="..."` - the commit message. It reaches bmk through the environment, so punctuation, `$`
    and newlines are safe; a multi-line `MSG` becomes a subject plus body. Prefer it over words or
    `ARGS`, which bash re-parses (a newline in `ARGS` is refused).
  - Without a message, bmk prompts at a terminal and uses `chores` when not run from one. The
    subject is prefixed with the local date and time (`2026-11-27 14:05:00 - fix login bug`).
  - `BMK_GIT_REMOTE` (default `origin`) and `BMK_GIT_BRANCH` (default: the current branch) choose
    where `push` pushes.
- **codecov**
  - `CODECOV_TOKEN` (from the environment or `.env`, see `.env.example`) for the upload.

### Target Details

- `test`: the local gate. It refreshes outdated dependency floors in `pyproject.toml` and the
  `uv.lock`, applies `ruff format` and `ruff check --fix`, then runs bandit, import-linter,
  pip-audit, pyright, pytest with coverage (doctests included), the ruff format and lint checks,
  PSScriptAnalyzer on `.ps1` files and shellcheck on shell scripts. It skips the markers listed in
  `[tool.scripts.test].exclude-markers` (here `mutating`) and tries a Codecov upload of the coverage
  report, which is skipped when no token or uploader is available. Output is JSON by default;
  `make test-human` prints it for reading.
- `push`: checks the dependencies against PyPI, builds the wheel and sdist and runs `test` in
  parallel, cleans, stages every change (`git add -A`), commits (an empty commit when nothing is
  staged) and pushes with `git push -u`.
- `bump` (the same as `bump-patch`), `bump-minor`, `bump-major`: raise the version in
  `pyproject.toml`, rename the `[Unreleased]` section of `CHANGELOG.md` to the new version and date,
  and write the new version into `src/pwshpy/__init__conf__.py`.
- `release`: tags the version in `pyproject.toml` (`vX.Y.Z`), pushes the tag and creates a GitHub
  release when `gh` is available. The tag push starts the release workflow.
- `ship`: `push`, wait for CI, `release`, wait for the release workflow.
- `codecov`: runs the tests with coverage and uploads the report to Codecov.
- `test-all`: pytest and pyright on every Python version the classifiers declare.
- `ensure`: installs missing external tools (such as shellcheck) for this OS.

## Test Markers and What Each Command Runs

Tests are gated by pytest markers so the local gate, the integration lane, and CI each run the
right subset:

- **`local_only`** - needs a local resource the CI runners lack (a Windows host + pwsh for the
  read-only oracles, the `[full]` .NET runtime, a readable journald). `make test` **runs** these
  locally and they skip cleanly when the resource is absent; CI **excludes** them
  (`pytest -m "not local_only"`). The local-vs-CI difference is intentional.
- **`mutating`** - **changes** real host state (creates users/services/tasks, writes the registry,
  clears an event log). `make test` **skips** these: this project sets
  `[tool.scripts.test].exclude-markers = "mutating"`. They run only on a disposable throwaway host.
- **`os_agnostic` / `os_windows` / `os_macos` / `os_posix` / `os_linux`** - label the target OS.
  `tests/conftest.py` skips an `os_windows` / `os_macos` / `os_posix` / `os_linux` test on any other
  platform; `os_agnostic` runs everywhere.

### No test touches the desktop keyring (session bus guard)

Any contact with `org.freedesktop.secrets` on the desktop session bus D-Bus-activates the user's
keyring daemon, and a locked login keyring then pops a password dialog on the desktop. So
`tests/conftest.py` points `DBUS_SESSION_BUS_ADDRESS` at an abstract socket nobody listens on for
the whole run, and every child process a test spawns inherits it. The variable is replaced, not
removed: with it unset, libdbus, sd-bus and GDBus clients fall back to `$XDG_RUNTIME_DIR/bus` or
X11 autolaunch, which reach a live session bus just the same. The system bus is untouched, and
`tests/test_session_bus_guard.py` pins the guard.

The live Secret Service round-trip (`tests/test_secret_service.py::test_save_load_delete_roundtrip`)
is opt-in with `PWSHPY_TEST_SECRET_SERVICE=1`, which also switches the guard off. Only the process
environment that starts pytest can opt in: the conftest reads the variable before it loads `.env`,
so a `.env` line setting it is ignored. Under the opt-in, a keyring that refuses the save (no
default collection, a locked one) FAILS the round-trip instead of skipping it.

Set the opt-in ONLY on a private bus with a throwaway keyring in a throwaway HOME and no display. A
private bus alone is not enough: a `gnome-keyring-daemon` started there still reads and writes the
real `~/.local/share/keyrings` (and `--unlock` creates a `login.keyring` there when none exists).

**UNVERIFIED: the recipe below has not been run yet.** Do not run it on a machine whose desktop
keyring matters until it has been verified on a throwaway box. Run it from the repo root (it calls
`.venv/bin/python` by relative path). It builds the environment from `env -i` with an explicit
allowlist, so nothing from the desktop session (bus address, display, keyring control socket) leaks
in; it fails unless the round-trip PASSED with nothing skipped, and it fails if the real keyring
directory changed during the run:

```bash
(
    real_keyrings="$HOME/.local/share/keyrings"  # captured BEFORE HOME is overridden below
    snapshot() { stat -c '%n %Y %s' "$real_keyrings" "$real_keyrings"/* 2>/dev/null | sort; }
    before=$(snapshot)
    sandbox=$(mktemp -d) || exit 1
    mkdir -m 700 "$sandbox/home" "$sandbox/share" "$sandbox/config" "$sandbox/cache" "$sandbox/run"
    env -i PATH="$PATH" LANG="${LANG:-C.UTF-8}" \
        HOME="$sandbox/home" XDG_DATA_HOME="$sandbox/share" XDG_CONFIG_HOME="$sandbox/config" \
        XDG_CACHE_HOME="$sandbox/cache" XDG_RUNTIME_DIR="$sandbox/run" \
        dbus-run-session -- sh -c '
            printf %s test | gnome-keyring-daemon --unlock --components=secrets >/dev/null
            PWSHPY_TEST_SECRET_SERVICE=1 .venv/bin/python -m pytest -p no:cacheprovider --no-cov -rs \
                tests/test_secret_service.py::test_save_load_delete_roundtrip' >"$sandbox/pytest.log" 2>&1
    rc=$?
    after=$(snapshot)
    cat "$sandbox/pytest.log"
    verdict=PASS
    [ "$before" = "$after" ] || { echo "FAIL: $real_keyrings changed during the run"; verdict=FAIL; }
    [ "$rc" -eq 0 ] || { echo "FAIL: pytest exited $rc"; verdict=FAIL; }
    grep -q ' 1 passed' "$sandbox/pytest.log" || { echo "FAIL: the round-trip did not pass"; verdict=FAIL; }
    if grep -qi 'skipped' "$sandbox/pytest.log"; then echo "FAIL: a test was skipped"; verdict=FAIL; fi
    rm -rf "$sandbox"
    echo "$verdict"
    [ "$verdict" = PASS ]
)
```

### Quick Reference

| Command                      | What it runs                                                                                    |
|------------------------------|-------------------------------------------------------------------------------------------------|
| `make test`                  | Everything EXCEPT `mutating` (unit + safe `local_only`, which skip when the resource is absent) |
| `make testintegration`       | Only `@pytest.mark.integration` (long-running / external; aliases `testi`, `ti`)                |
| `pytest -m "not local_only"` | The CI gate - unit tests only                                                                   |
| `pytest -m mutating`         | The host-mutating suite - a disposable throwaway host ONLY, never your dev box                  |
| `pytest tests/`              | ALL tests, no filter - never on your dev box (it runs `mutating`)                               |

### Adding a host-mutating test

Mark it `mutating` (alongside `local_only` and the OS marker) so `make test` skips it and only the
throwaway host runs it. Never run a `mutating` test on a machine you care about:

```python
@pytest.mark.local_only
@pytest.mark.mutating
@pytest.mark.os_windows
def test_real_windows_mutation(...):
    """Mutating test requiring a (disposable) Windows host."""
    ...
```

## Development Workflow

```bash
make test                          # the full local gate, JSON output
make test-human                    # the same gate, human-readable output
make push MSG="fix(cli): subject"  # gate, commit everything, push
```

### Versioning & Metadata

- Single source of truth for package metadata is `pyproject.toml` (`[project]`).
- The library reads its own metadata from static constants (see `src/pwshpy/__init__conf__.py`).
- Change the version only with `make bump-patch`, `make bump-minor` or `make bump-major`, which
  update `pyproject.toml`, `CHANGELOG.md` and `__init__conf__.py` together.
- The console script is `pwshpy`, declared in `[project.scripts]`.

### Dependency Auditing

- `make test` invokes `pip-audit` to check for known vulnerabilities. If pip-audit reports vulnerabilities, address them by pinning fixed versions in `[project.optional-dependencies.dev]`.

### CI & Publishing

GitHub Actions workflows are included:

- `.github/workflows/default_cicd_public.yml` (CI) - on push, pull request and a nightly schedule:
  ruff, pyright, pytest with coverage and a Codecov upload, import-linter, bandit and pip-audit
  across the Python versions and OSes `pyproject.toml` declares, then a wheel/sdist build, a clean
  install of the wheel, pipx and uv tool installs, and the notebook run.
- `.github/workflows/default_release_public.yml` (Release) - on a pushed `v*` tag (or a manual
  run), builds the wheel and sdist and publishes them to PyPI, through OIDC Trusted Publishing or
  a `PYPI_API_TOKEN` secret when one is set.
- `.github/workflows/codeql.yml` (CodeQL) - code scanning on push, pull request and weekly.

To publish a release:
1. Add the changes under `[Unreleased]` in `CHANGELOG.md` and bump the version (`make bump-patch`,
   `make bump-minor` or `make bump-major`).
2. Run `make ship`: it pushes, waits for CI, tags `vX.Y.Z` and creates the GitHub release, then
   waits for the release workflow, which uploads the wheel and sdist to PyPI.

### Local Codecov uploads

- `make test` and `make codecov` write `coverage.xml` and then try to upload it with the Codecov CLI
  (`codecovcli` on `PATH`). Without the CLI or a token the upload is skipped with a warning and the
  gate still passes.
- Set `CODECOV_TOKEN` in `.env` (see `.env.example`) or export it in your shell.
- The upload makes no commit; it reports the current git commit.
