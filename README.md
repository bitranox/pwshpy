# pwshpy

<!-- Badges -->
[![CI](https://github.com/bitranox/pwshpy/actions/workflows/default_cicd_public.yml/badge.svg)](https://github.com/bitranox/pwshpy/actions/workflows/default_cicd_public.yml)
[![CodeQL](https://github.com/bitranox/pwshpy/actions/workflows/codeql.yml/badge.svg)](https://github.com/bitranox/pwshpy/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Open in Codespaces](https://img.shields.io/badge/Codespaces-Open-blue?logo=github&logoColor=white&style=flat-square)](https://codespaces.new/bitranox/pwshpy?quickstart=1)
[![PyPI](https://img.shields.io/pypi/v/pwshpy.svg)](https://pypi.org/project/pwshpy/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/pwshpy.svg)](https://pypi.org/project/pwshpy/)
[![Code Style: Ruff](https://img.shields.io/badge/Code%20Style-Ruff-46A3FF?logo=ruff&labelColor=000)](https://docs.astral.sh/ruff/)
[![codecov](https://codecov.io/gh/bitranox/pwshpy/graph/badge.svg?token=UFBaUDIgRk)](https://codecov.io/gh/bitranox/pwshpy)
[![Maintainability](https://qlty.sh/badges/041ba2c1-37d6-40bb-85a0-ec5a8a0aca0c/maintainability.svg)](https://qlty.sh/gh/bitranox/projects/pwshpy)
[![security: bandit](https://img.shields.io/badge/security-bandit-yellow.svg)](https://github.com/PyCQA/bandit)


`pwshpy` is a genuinely Pythonic PowerShell: **typed records and a lazy, fluent pipeline** over
native OS bindings. It never spawns `pwsh.exe` and never parses text.

- **Tier A (native):** win32 / wmi / winreg / psutil / socket bindings. Fast, no .NET. The default
  for common cmdlets. The portable subset (psutil + stdlib socket / os.environ) works on every OS;
  the win32/wmi bulk is Windows-only.
- **Tier B (hosted PowerShell 7.6 SDK):** `Microsoft.PowerShell.SDK` on .NET 10, hosted in-process
  via pythonnet, behind the optional `[full]` extra. Covers the long tail (AD / Exchange / Azure /
  module-only cmdlets) and runs existing `.ps1` at full fidelity - never as a subprocess.

Both tiers marshal into the SAME typed records, so the pipeline composes over either source. pwshpy
ships as an importable library (`from pwshpy import ps`) and as a `pwshpy` CLI over the same surface.

> Status: the portable Tier-A surface and CLI are implemented; Tier B is currently an import-guard
> stub (a call without `[full]` raises a clear error and loads no .NET). The win32/wmi Tier-A bulk
> and the real in-process Tier-B host are Windows/.NET work in progress.

### Python 3.10+ Baseline

- The project targets **Python 3.10 and newer**.
- Base runtime deps: `psutil`, `rich-click`, `lib_cli_exit_tools`, `lib_log_rich`,
  `lib_layered_config`, `pydantic`, `orjson` (plus `pywin32`/`wmi` on Windows only). The `[full]`
  extra adds `pythonnet` + `clr-loader` for Tier B and requires the .NET 10 runtime.
- CI exercises GitHub's rolling runner images (`ubuntu-latest`, `macos-latest`, `windows-latest`)
  across CPython 3.10 through 3.14.

---

## Install - recommended via uv

[uv](https://docs.astral.sh/uv/) is an ultrafast Python package manager written in Rust.

### One-shot run (no install needed)

```bash
uvx pwshpy@latest --help
```

### Persistent install as a CLI tool

```bash
# isolated environment, added to PATH
uv tool install pwshpy
# upgrade to latest
uv tool upgrade pwshpy
# run
pwshpy --help
```

### Install as a project dependency

```bash
uv venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
uv pip install pwshpy
# with the Tier-B hosted-PowerShell extra (requires the .NET 10 runtime):
uv pip install "pwshpy[full]"
```

For alternative install paths (pip, pipx, source builds), see [INSTALL.md](INSTALL.md). All
supported methods register the `pwshpy` command on your PATH.

---

## Quick Start

```bash
pwshpy --version
pwshpy processes --jsonl | head           # stream running processes as JSON lines
pwshpy disks                              # human table of mounted filesystems
pwshpy info                               # resolved package metadata
```

---

## Library Usage

```python
from pwshpy import ps

# typed records + a lazy, fluent pipeline
for proc in ps.processes().where(lambda p: p.name == "python").sort_by(lambda p: p.pid).take(5):
    print(proc.pid, proc.name, proc.status.value)

# every subsystem returns the same pipeline over typed records
listening = ps.connections().where(lambda c: c.status.value == "LISTEN").to_list()
root_disk = ps.disks().first(lambda d: d.mountpoint == "/")
addrs = ps.resolve("example.com").select(lambda r: r.address).to_list()
```

Records are Pydantic models (`ProcessInfo`, `NetConnection`, `DiskUsage`, `SystemUptime`,
`DnsRecord`, `ConnectionTest`, `EnvVar`); the pipeline offers `.where` / `.select` / `.sort_by` /
`.take` / `.first` / `.to_list`. Errors share one hierarchy rooted at `PwshPyError`.

---

## CLI Command Surface

The `pwshpy` command mirrors the library. Tier-A record commands share `--json` (array),
`--jsonl` (streaming, one record per line), and a default human table; most take `--limit N`.

| Command                       | Shows                         | Binding       |
|-------------------------------|-------------------------------|---------------|
| `pwshpy processes`            | running processes             | psutil        |
| `pwshpy connections`          | open network connections      | psutil        |
| `pwshpy disks`                | mounted filesystems and usage | psutil        |
| `pwshpy uptime`               | boot time and elapsed uptime  | psutil        |
| `pwshpy resolve NAME`         | resolved DNS addresses        | stdlib socket |
| `pwshpy test-connection HOST` | TCP reachability probe        | stdlib socket |
| `pwshpy env`                  | environment variables         | os.environ    |

```bash
# streaming and piping stay lazy end to end
pwshpy processes --jsonl | head -20
pwshpy connections --json | jq '.[] | select(.status == "LISTEN")'
pwshpy test-connection example.com -p 443 --timeout 3 --jsonl
pwshpy resolve example.com --jsonl

# configuration management
pwshpy config                         # show current merged configuration
pwshpy config --format json           # show as JSON
pwshpy config --section lib_log_rich  # show a specific section
pwshpy config --profile production    # use a named profile

# deploy configuration templates to target directories
pwshpy config-deploy --target app     # system-wide
pwshpy config-deploy --target user    # ~/.config/{slug}/config.toml
pwshpy config-generate-examples --destination ./examples

# runtime overrides (repeatable --set) and an explicit .env
pwshpy --set lib_log_rich.console_level=DEBUG config
pwshpy --env-file ./environments/production.env config

# works with any entry point
python -m pwshpy info
uvx pwshpy info
```

---

## Platform Support

- **Linux / macOS:** the portable Tier-A subset (processes, connections, disks, uptime, resolve,
  test-connection, env) plus Tier B (when `[full]` and the .NET 10 runtime are present).
- **Windows:** the portable subset above, plus the win32/wmi Tier-A bulk (services, registry, event
  log, CIM, scheduled tasks, local accounts, ACLs) - in progress.

pwshpy does NOT wrap `pwsh.exe`: Tier A is native bindings, Tier B hosts the PowerShell engine
in-process via pythonnet.

---

## Configuration

See [CONFIG.md](CONFIG.md) for the layered configuration system (precedence rules, profile support,
and customization). Profile names are validated: alphanumeric, hyphens, underscores; max 64 chars;
must start with a letter or digit.

---

## Further Documentation

- [Install Guide](INSTALL.md)
- [Development Handbook](DEVELOPMENT.md)
- [Contributor Guide](CONTRIBUTING.md)
- [System Design](docs/systemdesign/pwshpy-initial-design.md)
- [Changelog](CHANGELOG.md)
- [License](LICENSE)
