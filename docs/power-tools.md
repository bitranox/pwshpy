# Power Tools

[Back to the README](../README.md) | **Prev:** [Library & Pipeline](library-usage.md) | **Next:** [CLI Reference](cli-reference.md)

Focused helpers that fix long-standing PowerShell / Windows pain points. Each returns typed data,
raises real Python exceptions, and works from the same `ps` facade.

## Self-elevation (no `Start-Process -Verb RunAs` boilerplate)

Checking for admin and relaunching elevated is normally a `WindowsPrincipal` / `IsInRole` /
`Start-Process -Verb RunAs` ritual that loses your working directory and arguments. pwshpy makes it
one call, and the elevated child keeps your cwd and argv:

```python
ps.is_elevated()          # bool - cross-platform (Windows token / POSIX euid 0)
ps.require_elevation()    # raises ElevationRequiredError if not admin (an honest #Requires)
ps.elevate()              # relaunch THIS process elevated (UAC), preserving cwd + argv
```

```bash
pwshpy is-elevated              # prints "elevated"/"not elevated"; exit 0 if admin, 1 if not
pwshpy is-elevated -q && pwshpy clear_event_log Application   # gate a privileged step
pwshpy --elevate clear_event_log Application                  # relaunch elevated if needed
```

## `ps.exec` - run a program without the quoting / `$LASTEXITCODE` traps

An argv list (no shell, no quoting hell), an exit code that is **always** set, and stderr as plain
data, never mistaken for a terminating error:

```python
r = ps.exec(["git", "status", "--short"], cwd="/repo")
r.exit_code           # always populated
r.stdout, r.stderr    # captured text
r.check()             # raises NativeCallError on a nonzero exit (opt-in "stop on error")
```

CLI: `pwshpy exec -- git status` (put the program after `--` so its flags are not parsed by pwshpy).

## Predictable, memory-bounded file I/O

No more UTF-16LE-with-a-BOM surprises from `Out-File`; the bytes are identical on every OS and
Python version. And every write path streams, so a huge source never buffers:

```python
ps.write_text("report.txt", text)                 # UTF-8, no BOM, LF - guaranteed
ps.write_text_stream("out.txt", chunks)            # same guarantee, streamed chunk-by-chunk
ps.write_records("procs.jsonl", ps.get_process())  # typed records -> UTF-8 JSONL, one at a time

for line in ps.get_content_lines("huge.log"):      # reads one line at a time (vs whole-file get_content)
    ...
ps.download_file("https://host/big.iso", "big.iso")  # copies to disk in chunks; never buffers the body
```

CLI: `pwshpy write_text PATH` (reads stdin), `pwshpy get_content PATH` (streams), `pwshpy
download_file URL DEST`.

## Credential vault - typed, secret-safe, unattended-friendly

`get_credential` is the typed `Get-Credential`; the store is the Windows Credential Manager (the
unattended-safe alternative to a DPAPI file that will not decrypt under another account). The secret
is a `SecretStr` - masked in repr / JSON / logs:

```python
cred = ps.get_credential("svc")                       # non-echoing prompt
ps.save_credential("prod-db", "svc", cred.secret.get_secret_value())
later = ps.load_credential("prod-db")                 # None if absent; secret stays masked
```

CLI: `pwshpy save_credential TARGET USER` (secret from a hidden prompt, never an argv flag),
`pwshpy load_credential TARGET` (secret shown masked), `pwshpy delete_credential TARGET`.

## .NET - safe cmdlet binding and parameter discovery

`ps.cmdlet` binds every parameter as data (never string-interpolated into a script), captures all
six streams, and `ps.get_command` tells you what parameters a cmdlet takes:

```python
result = ps.cmdlet("Get-ChildItem", Path="C:/logs", Recurse=True)   # safe bound params
result.output, result.warnings, result.errors                       # every stream, typed

info = ps.get_command("Get-Item")                                   # typed introspection
[p.name for p in info.parameters if p.mandatory]                    # discover mandatory params
```

AD / Exchange / Azure and any other module cmdlet run this way too; a few common ones have typed
shortcuts (`ps.get_ad_user(Filter="*")`, `ps.get_mailbox(...)`, `ps.get_az_vm(...)`). CLI:
`pwshpy run "..."`, `pwshpy cmdlet Get-ChildItem -p Path=C:/logs -p Recurse=true`,
`pwshpy get_command Get-Item`. These need the `[full]` extra plus the module installed.

## Whole classes of gotchas, gone by design (with tests that keep it that way)

- **No execution-policy wall.** pwshpy is Python; `pip install pwshpy` then `pwshpy get_service`
  just runs. No `Set-ExecutionPolicy` / `-ExecutionPolicy Bypass` dance (the native backend never
  touches PowerShell at all).
- **No single-item collapse.** `.to_list()` is always a real list - length 0, 1, or N - so `len()`
  is always right and a one-row result is not silently a scalar.
- **No `$null` / operand-order trap.** Filtering is ordinary Python (`if record.field is None:`),
  never "the filtered subset when you meant a bool".
- **Principals by SID, not localized name.** `Administrators` is `Administratoren` on German Windows
  and `Administrateurs` on French; pwshpy carries the SID as the canonical field and the localized
  name as display only, so identity does not break across locales. See
  [locale-and-identity.md](locale-and-identity.md).
- **Errors are exceptions.** Every failure raises a `PwshPyError`. No terminating-vs-non-terminating,
  no `-ErrorAction Stop`, no swallowed error.

Every native read command is pinned by an oracle test that compares its records to the real cmdlet
(`Get-Service`, `Get-WinEvent`, ...) on locale-invariant fields, so these properties stay true.
