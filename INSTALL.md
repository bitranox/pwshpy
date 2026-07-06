# Installation Guide

> The CLI stack uses `rich-click`, which bundles `rich` styling on top of click-style ergonomics.

This guide collects every supported method to install `pwshpy`, including
isolated environments and system package managers. Pick the option that matches your workflow.


## We recommend `uv` to install the package

### `uv` = Ultra-fast Python package manager

> lightning-fast replacement for `pip`, `venv`, `pip-tools`, and `poetry`
written in Rust, compatible with PEP 621 (`pyproject.toml`)

### `uvx` = On-demand tool runner

> runs tools temporarily in isolated environments without installing them globally


## Install uv (if not already installed)

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## One-shot run via uvx (no install needed)

```bash
uvx pwshpy@latest --help
```

## Persistent install as CLI tool

```bash
# install the CLI tool (isolated environment, added to PATH)
uv tool install pwshpy

# upgrade to latest
uv tool upgrade pwshpy
```

## Install as project dependency

```bash
uv venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
uv pip install pwshpy
```

## Verify installation

After any install method, confirm the CLI is available:

```bash
pwshpy --version
```

---

## Installation via pip

```bash
# optional, install in a venv (recommended)
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
# install from PyPI
pip install pwshpy
# optional install from GitHub
pip install "git+https://github.com/bitranox/pwshpy"
# optional development install from local
pip install -e ".[dev]"
# optional install from local runtime only:
pip install .
```

## Per-User Installation (No Virtualenv) - from local

```bash
# install from PyPI
pip install --user pwshpy
# optional install from GitHub
pip install --user "git+https://github.com/bitranox/pwshpy"
# optional install from local
pip install --user .
```

> Note: This respects PEP 668. Avoid using it on system Python builds marked as
> "externally managed". Ensure `~/.local/bin` (POSIX) is on your PATH so the CLI is available.

## pipx (Isolated CLI-Friendly Environment)

```bash
# install pipx via pip
python -m pip install pipx
# optional install pipx via apt
sudo apt install python-pipx
# install via pipx from PyPI
pipx install pwshpy
# optional install via pipx from GitHub
pipx install "git+https://github.com/bitranox/pwshpy"
# optional install from local
pipx install .
pipx upgrade pwshpy
# install from Git tag
pipx install "git+https://github.com/bitranox/pwshpy@v1.1.0"
```

## From Build Artifacts

```bash
python -m build
pip install dist/pwshpy-*.whl
pip install dist/pwshpy-*.tar.gz   # sdist
```

## Poetry or PDM Managed Environments

```bash
# Poetry
poetry add pwshpy     # as dependency
poetry install                          # for local dev

# PDM
pdm add pwshpy
pdm install
```

## Install Directly from Git

```bash
pip install "git+https://github.com/bitranox/pwshpy"
```

## System Package Managers (Optional Distribution Channels)

- Use [fpm](https://fpm.readthedocs.io/) to repackage the Python wheel into `.deb` or `.rpm` for distribution via `apt` or `yum`/`dnf`.

All methods register both the `pwshpy` and
`pwshpy` commands on your PATH.
