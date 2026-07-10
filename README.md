# pwshpy

<!-- Badges -->
[![CI](https://github.com/bitranox/pwshpy/actions/workflows/default_cicd_public.yml/badge.svg)](https://github.com/bitranox/pwshpy/actions/workflows/default_cicd_public.yml)
[![CodeQL](https://github.com/bitranox/pwshpy/actions/workflows/codeql.yml/badge.svg)](https://github.com/bitranox/pwshpy/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Open in Codespaces](https://img.shields.io/badge/Codespaces-Open-blue?logo=github&logoColor=white&style=flat-square)](https://codespaces.new/bitranox/pwshpy?quickstart=1)
[![PyPI](https://img.shields.io/pypi/v/pwshpy.svg)](https://pypi.org/project/pwshpy/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/pwshpy.svg)](https://pypi.org/project/pwshpy/)
[![Code Style: Ruff](https://img.shields.io/badge/Code%20Style-Ruff-46A3FF?logo=ruff&labelColor=000)](https://docs.astral.sh/ruff/)
[![codecov](https://codecov.io/gh/bitranox/pwshpy/graph/badge.svg?token=9HF64oKYEp)](https://codecov.io/gh/bitranox/pwshpy)
[![Maintainability](https://qlty.sh/badges/041ba2c1-37d6-40bb-85a0-ec5a8a0aca0c/maintainability.svg)](https://qlty.sh/gh/bitranox/projects/pwshpy)
[![security: bandit](https://img.shields.io/badge/security-bandit-yellow.svg)](https://github.com/PyCQA/bandit)

## I hate PowerShell.

Not the way you hate Mondays. The way you hate a houseguest who keeps "helpfully" rearranging your
kitchen while you sleep. PowerShell looks like a shell, quacks like a shell, and then every
convenience it hands you turns out to have a tripwire underneath. The infuriating part is not that
it is hard to learn. It is that the *obvious* code is the *wrong* code, and it fails quietly, in
production, at 3 a.m.

A short tasting menu of the betrayals:

- Ask for one service and you might get back one object, or a collection containing one object, and
  `len()` misleads you either way. That is the famous single-item collapse.
- Put `$null` on the wrong side of `-eq` and your comparison silently becomes a *filter*. Your `if`
  now depends on operand order. Nobody warned you; it just returns the wrong answer.
- A cmdlet "fails" and your `try/catch` sits there twiddling its thumbs, because the error was
  "non-terminating" and you forgot the incantation `-ErrorAction Stop`.
- `$events = Get-WinEvent ...` looks harmless and eats all your RAM, because assigning to a variable
  slurps the whole result set into memory at once. The memory-safe version is the one you would
  never write by accident.
- `Out-File -Encoding utf8` still writes a BOM on Windows PowerShell 5.1, so "the same script"
  produces different bytes on different machines.
- A function returns *everything* it emits, not just what you `return`. One stray `$list.Add(x)`
  (which quietly returns the new count) poisons your return value, and the caller gets back an array
  of junk they never asked for.
- Read `"false"` from a config file, treat it as a boolean, and it is `$true`. Casting a string to
  bool tests its *length*, not its content - every non-empty string is true, `"0"` and `"false"`
  included. Only the empty string is false.
- `$results += $item` in a loop is quietly O(n^2): every append copies the entire array. Fine at a
  thousand rows, a coffee break at a hundred thousand. (7.5 finally softened it; 5.1 still punishes
  you for the obvious code.)
- When a script does die, you get a one-line message and, half the time, the *wrong* line number
  (a real, still-open bug). The actual call stack sits unprinted on `$Error[0].ScriptStackTrace`,
  and step-debugging needs an IDE. Tracing a failure is archaeology.
- And my personal favourite: someone at Microsoft decided the *Administrators* group should be
  called *Administratoren* on a German box and *Administrateurs* on a French one. So a script that
  checks group membership by name is one locale away from a security hole. Who, precisely, thought
  translating a security principal was a good idea?

You can learn to dodge every single one of these. That is the tragedy, not the consolation:
PowerShell asks you to become an expert in its landmines instead of an expert in your actual problem.
The prize for mastery is that you get to type defensive boilerplate slightly faster than the next
person.

## pwshpy is the other bet

Stop wrapping the shell. Talk to the OS directly, in Python, and get typed objects back.

`pwshpy` is a genuinely Pythonic PowerShell: **typed records and a lazy, fluent pipeline** over
native OS bindings. It never spawns `pwsh.exe` and never parses text. Two backends feed the same
typed records - **native** (win32 / wmi / winreg / psutil / socket, fast, no .NET) and **.NET**
(the real PowerShell 7.6 engine hosted in-process for the AD / Exchange / Azure long tail, behind
the optional `[full]` extra). It ships as a library (`from pwshpy import ps`) and a `pwshpy` CLI
over the same surface.

The point is not that it is prettier. It is that the obvious code is now the *correct* code. So,
against that tasting menu of betrayals, here is what you get instead:

- **Typed objects, not text.** Records come back as Pydantic models with real fields. You never
  re-parse a formatted string and pray the columns did not shift.
- **The naive version is the right version.** No single-item collapse, no `$null` operand dance, no
  forgotten `-ErrorAction Stop`. The code you would write without thinking is the code that works.
- **Memory-bounded by default.** The natural way to write it is also the way that does not fall over
  on a twenty-million-row event log. Streaming is the default, not something you remember.
- **You can actually debug it.** When it breaks you get a Python traceback pointing at the exact
  line, `breakpoint()` works anywhere (even "mid-pipeline" - it is ordinary iteration), and every
  failure is an exception. Nothing gets appended to a global `$Error` list and quietly skipped.
- **A colleague can review it.** It reads as plain Python. A reviewer does not need to know where
  the bodies are buried to approve the pull request.
- **Identity survives a locale.** Principals are keyed by SID, not by a group name Microsoft
  translates, so a script written on an English box still works on a German one.
- **One script, every machine.** A large part of pwshpy is the same bytes on Windows, macOS, and
  Linux - not just processes and files, but services, the event log, scheduled tasks, local accounts,
  ACLs, the credential vault, and the elevation relaunch, each on its native backend (win32 on
  Windows; systemd, journald, `pwd`/`grp`, the ACL xattr, the desktop keyring, `sudo` on Linux),
  never a shelled-out fake. The second script, the `.sh` twin that always drifts, does not exist.
- **Built for LLMs and agents, on purpose.** pwshpy is deliberately shaped so a model can use it
  *optimally*: typed records, the obvious-code-is-the-correct-code discipline, and real exceptions
  give an agent an unambiguous surface with no text to re-parse and no silent failures. And it ships
  a `using-pwsh` Claude Code skill (install anywhere with `/plugin marketplace add bitranox/pwshpy`
  then `/plugin install pwshpy`) that makes an agent reach for pwshpy - whether issuing a PowerShell
  command or translating an existing, probably-buggy `.ps1` - instead of writing or debugging
  PowerShell. Migrating is a review, not a rewrite.

```bash
uv tool install pwshpy          # or: uvx pwshpy@latest --help  (no install)
```

## One script, every machine

Here is a cost that never makes it onto the spreadsheet: the *second* script. You write
`bootstrap.ps1` for the Windows fleet, then `bootstrap.sh` for the Linux fleet, and promise yourself
they do the same thing. They do not. They drift, each quietly lying about the other, and six months
on a one-line change means editing two files in two languages and testing on two machines.

A large part of pwshpy is portable by construction. The portable core - processes, network, disks,
files, environment, `exec`, HTTP downloads, the admin check, the credential prompt - is the same
bytes on Windows, macOS, and Linux. So are the pieces a real setup script actually leans on: services
(win32service / systemd), the event log (win32evtlog / journald), scheduled tasks (Task Scheduler /
systemd timers), local users and groups (win32net / `pwd`/`grp`), the credential vault (Credential
Manager / the desktop keyring), and the elevation *relaunch* (UAC / `sudo`) - each on its native
backend, talked to over its own API, never a shelled-out impersonation. So a provisioning or setup
script written once, in Python, runs on every box unchanged. No `if ($IsWindows)` fork, no parallel
`.sh` sliding out of sync.

```python
from pwshpy import ps

# the same lines on Windows, macOS, and Linux
ps.require_elevation()                                              # honest #Requires, any OS
ps.exec(["git", "clone", "https://github.com/acme/app", "/opt/app"]).check()
ps.write_text("/opt/app/config.toml", "port = 8080\n")            # UTF-8, no BOM, identical bytes
ps.download_file("https://host/tool.tgz", "/tmp/tool.tgz")         # streamed, memory-bounded
sshd = ps.get_process().where(lambda p: p.name == "sshd").first()
ps.get_service().where(lambda s: s.name.startswith("nginx")).first()   # win32service | systemd D-Bus
```

Services, the event log, scheduled tasks, local accounts, ACLs, the credential vault, and the
elevation *relaunch* all run natively on both Windows and Linux, verified on a real box on each. The
only subsystems that stay Windows-only are the ones with no honest Linux analog - the registry and
CIM/WMI (the [portability roadmap](docs/portability-roadmap.md) has the reasoning). The ambition is
deliberately dull and rather large: one setup script, every machine.

## The same task, with and without the landmines

**The job:** as an admin, make sure a service is running, then export the System error events
(there could be **millions**) to a report file. Simple. In PowerShell it is a minefield of defensive
rituals, and the memory-safe way is not the way you would reach for.

### PowerShell (correct, but every line hides a trap)

```powershell
# 1. Needs admin - but there is no clean self-relaunch, so we can only CHECK and abort.
#    (Start-Process -Verb RunAs loses your cwd, args, and any return value.)
if (-not ([Security.Principal.WindowsPrincipal]`
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Run me as Administrator"; exit 1
}

$name = "Spooler"

# 2. Get-Service may return a collection OR a single object -> force an array, take [0].
$svc = @(Get-Service -Name $name -ErrorAction SilentlyContinue)[0]
# 3. $null MUST be on the left ($svc -eq $null would *filter a collection*, not test it).
if ($null -eq $svc) { Write-Error "No such service"; exit 1 }

# 4. Cmdlet errors are non-terminating by default: without -ErrorAction Stop the catch never fires.
try {
    if ($svc.Status -ne 'Running') { Start-Service -Name $name -ErrorAction Stop }
} catch { Write-Error "Failed to start: $_"; exit 1 }

# 5. ALL error events - potentially millions. The MEMORY TRAP: assigning to a variable
#    ($events = Get-WinEvent ...) collects the ENTIRE result into RAM and can exhaust memory;
#    Sort-Object / Select-Object buffer everything too. To stay bounded you must NOT capture and
#    must pipe straight through - and Get-WinEvent still throws on empty.
# 6. ...and Out-File defaults to UTF-16LE-with-BOM on Windows PowerShell 5.1, so the bytes differ
#    by host version.
Get-WinEvent -FilterHashtable @{LogName='System'; Level=2} -ErrorAction SilentlyContinue |
    ForEach-Object { '{0}`t{1}`t{2}' -f $_.TimeCreated, $_.Id, $_.Message } |
    Out-File -FilePath "C:\reports\errors.txt" -Encoding utf8
```

### pwshpy (the traps simply do not exist)

```python
from pwshpy import ps, EventLevel, ServiceState

# 1. Self-elevate: relaunch THIS process as admin, preserving cwd + argv, then exit with the
#    child's code. No-op when already elevated. (PowerShell has no clean equivalent.)
if not ps.is_elevated():
    raise SystemExit(ps.elevate())

# 2. Find the service with a plain loop. ps.get_service() streams typed records; break stops at the
#    first match. No collection-collapse trap and no $null puzzle - just Python.
svc = None
for service in ps.get_service():
    if service.name == "Spooler":
        svc = service
        break
if svc is None:
    raise SystemExit("no such service")

# 3. Every failure raises a PwshPyError - no -ErrorAction Stop to remember, no swallowed error.
if svc.status is not ServiceState.RUNNING:
    ps.start_service("Spooler")

# 4. ALL error events, memory-bound. A plain generator function: read one event, test it, yield the
#    matches. It COLLECTS nothing; calling it reads nothing (a generator is lazy) - the log is
#    pulled one event at a time in step 5.
def error_events():
    for event in ps.get_win_event("System"):
        if event.level is EventLevel.ERROR:
            yield event

# 5. write_records STREAMS record-by-record straight to disk: read one, write one, discard. O(1)
#    memory whether the log holds twenty entries or twenty million. UTF-8, no BOM, LF.
ps.write_records("C:/reports/errors.jsonl", error_events())
```

Six defensive workarounds become six lines that say what they mean. Same task, no traps.

On a huge log the memory difference is the whole point. In PowerShell the natural
`$events = Get-WinEvent ...` capture (or a stray `Sort-Object`) pulls every matching record into RAM
at once, so staying bounded is a careful-coding burden that the obvious code gets wrong. pwshpy reads
and writes **one record at a time end to end** - a batched `EvtNext` read, a lazy `.where`, a
per-record `write_records` - so memory stays flat whether the log holds twenty entries or twenty
million. Streaming is the default, not something you have to remember. (The full memory model lives
in [Library & Pipeline](docs/library-usage.md).)

## The costly part: it stays true

Claims are cheap, so here is the receipt. Every native read command is pinned by an oracle test that
runs the real cmdlet (`Get-Service`, `Get-WinEvent`, `Get-CimInstance`, ...) on Windows and compares
pwshpy's typed records against it on locale-invariant fields. The gotchas above are not "gone because
we say so" - there are tests whose entire job is to keep them gone. Principals are identified by SID,
not by a name Microsoft translates. The whole thing runs strict-typed (pyright), memory-bounded, and
green across Linux / macOS / Windows and CPython 3.10 through 3.14.

## Documentation

Read it like a short book:

1. **[Quick Start](docs/quickstart.md)** - install and the first five commands.
2. **[Library & Pipeline](docs/library-usage.md)** - typed records, the lazy pipeline, the memory model.
3. **[Power Tools](docs/power-tools.md)** - self-elevation, `exec`, predictable file I/O, credentials, .NET binding.
4. **[CLI Reference](docs/cli-reference.md)** - the full command surface and configuration commands.
5. **[Backends & Platforms](docs/backends-and-platforms.md)** - native vs .NET, OS support, Python baseline, status.
6. **[Porting from PowerShell](COMMANDS.md)** - the cmdlet-by-cmdlet translation table.

Reference: [Installation](INSTALL.md) | [Configuration](CONFIG.md) | [Security](SECURITY.md) |
[Development](DEVELOPMENT.md) | [Contributing](CONTRIBUTING.md) | [Changelog](CHANGELOG.md) |
[License](LICENSE) | [AI stance](ai-stance.md) | [AI transparency](ai-transparency.md)

---

Still not convinced? Run the before/after above on your own event log and tell me which one you would
rather be debugging at 3 a.m. That is the only benchmark that matters.
