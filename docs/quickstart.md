# Quick Start

[Back to the README](../README.md) | **Next:** [Library & Pipeline](library-usage.md)

Install (see the [Installation guide](../INSTALL.md) for every method):

```bash
uv tool install pwshpy      # or: uvx pwshpy@latest --help  (no install)
```

Using Claude Code? pwshpy also ships a `using-pwsh` skill - install it with
`/plugin marketplace add bitranox/pwshpy` then `/plugin install pwshpy`. Full details in
[The included Claude Code skill](#the-included-claude-code-skill) below.

## The CLI in 30 seconds

```bash
pwshpy --version
pwshpy get_process --jsonl | head -20          # stream running processes as JSON lines
pwshpy get_volume                                # human table of mounted filesystems
pwshpy get_net_tcp_connection --json | jq '.[] | select(.status == "LISTEN")'
pwshpy resolve_dns_name example.com --jsonl
pwshpy test_connection example.com -p 443 --timeout 3
pwshpy info                                      # resolved package metadata
```

Every native record command shares three output modes:

- `--jsonl` : one JSON record per line; **stays lazy end to end** (`... --jsonl | head` stops early).
- `--json` : a single JSON array (also streamed).
- *(default)* : a human-readable rich table (soft-capped at 1000 rows; use `--jsonl` past that).

## The library in 30 seconds

```python
from pwshpy import ps

for proc in ps.get_process().where(lambda p: p.name == "python").take(5):
    print(proc.pid, proc.name, proc.status.value)
```

`from pwshpy import ps` gives you the whole surface. Read on:

- **[Library & Pipeline](library-usage.md)** : typed records, the lazy pipeline, and the memory model.
- **[Power Tools](power-tools.md)** : self-elevation, `exec`, predictable file I/O, the credential vault, .NET cmdlet binding.
- **[CLI Reference](cli-reference.md)** : the full command surface and configuration commands.
- **[Porting from PowerShell](../COMMANDS.md)** : the cmdlet-by-cmdlet translation table.

## The included Claude Code skill

pwshpy ships a `using-pwsh` Claude Code skill so an AI agent reaches for pwshpy instead of writing,
issuing, or debugging PowerShell. The repo is its own plugin marketplace - install the skill into any
project:

```bash
/plugin marketplace add bitranox/pwshpy      # add this repo as a marketplace
/plugin install pwshpy                        # install the plugin (the using-pwsh skill)
```

Then an agent asked to run a PowerShell command, port a `.ps1`, or list OS objects will use pwshpy -
typed records, clean JSON, real exit codes and exceptions - and fall back to `ps.cmdlet("Verb-Noun",
**params)` (the `[full]` extra) for any cmdlet without a native wrapper. The same skill is also
mirrored in the central [bitranox-skills](https://github.com/bitranox/bitranox-skills) marketplace as
`coding-python-pwshpy` (`/plugin marketplace add bitranox/bitranox-skills`).

## Any entry point works

```bash
pwshpy info
python -m pwshpy info
uvx pwshpy info
```
