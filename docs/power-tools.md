# Power Tools

[Back to the README](../README.md) | **Prev:** [Library & Pipeline](library-usage.md) | **Next:** [CLI Reference](cli-reference.md)

Focused helpers that fix long-standing PowerShell / Windows pain points. Each returns typed data,
raises real Python exceptions, and works from the same `ps` facade.

## Self-elevation (no `Start-Process -Verb RunAs` boilerplate)

Checking for admin and relaunching elevated is normally a `WindowsPrincipal` / `IsInRole` /
`Start-Process -Verb RunAs` ritual that loses your working directory and arguments. pwshpy makes it
one call, and the elevated child keeps your cwd and argv:

```python
ps.is_elevated()  # bool - cross-platform (Windows token / POSIX euid 0)
ps.require_elevation()  # raises ElevationRequiredError if not admin (an honest #Requires)
ps.elevate()  # relaunch THIS process elevated (UAC / sudo), preserving cwd + argv
```

On Linux and macOS the password is asked by `sudo -v` first, and only then is the command run under
`sudo`. sudo exits 1 when it refuses, which is also an ordinary exit code, so asking separately is what
lets a refusal be told apart from the command's own result.

A refused prompt raises `ElevationDeclinedError`, a `NativeCallError` subclass, so a caller can carry on
unelevated after a refusal and still treat any other failure as one. On Windows that is a declined UAC
prompt; on Linux and macOS it is any `sudo -v` that does not grant root - a wrong password, an
interrupted prompt, or a user sudo does not allow:

```python
from pwshpy import ElevationDeclinedError, ps

try:
    code = ps.elevate()  # the elevated copy's exit code once it ends
except ElevationDeclinedError:
    code = None  # the person said no: run unelevated and say so
```

```bash
pwshpy is-elevated              # prints "elevated"/"not elevated"; exit 0 if admin, 1 if not
pwshpy is-elevated -q && pwshpy clear_event_log Application   # gate a privileged step
pwshpy --elevate clear_event_log Application                  # relaunch elevated if needed
```

When the prompt is refused, `pwshpy --elevate` exits 77 (`EX_NOPERM` from `sysexits.h`) and writes a
line starting `pwshpy: elevation declined` to stderr, so a caller can tell a refusal from the
relaunched command's own failure. The packed runners below do the same for `--pwshpy-elevate` and
`-PwshPyElevate` (on a POSIX host without `sudo` they exit 127 instead, a missing tool rather than
a refusal). A command that itself exits 77 still looks the same by exit code alone; the stderr line
is what tells them apart.

## Who am I: the token, not `USERNAME`

`$env:USERNAME` / `$env:USERDOMAIN` describe the logon environment, not the token: inside a SYSTEM
scheduled task they name the computer account (`DOMAIN\HOST$`) while the process runs as
`S-1-5-18`. `ps.get_current_identity()` reads the process token instead, like `whoami /user`:

```python
me = ps.get_current_identity()
me.sid  # "S-1-5-18" under SYSTEM; the effective uid ("0" for root) on POSIX
me.is_system  # SID S-1-5-18 / euid 0
me.elevated, me.integrity_level  # token elevation + IntegrityLevel (None on POSIX)
me.account  # "DOMAIN\\name" - display only, localized for built-in accounts
```

Match on `sid` (or `WellKnownSid.LOCAL_SYSTEM`), never on the name. CLI: `pwshpy get_current_identity
--jsonl`.

## `ps.exec` - run a program without the quoting / `$LASTEXITCODE` traps

An argv list (no shell, no quoting hell), an exit code that is **always** set, and stderr as plain
data, never mistaken for a terminating error:

```python
r = ps.exec(["git", "status", "--short"], cwd="/repo")
r.exit_code  # always populated
r.stdout, r.stderr  # captured text
r.check()  # raises NativeCallError on a nonzero exit (opt-in "stop on error")
```

A run that outlives `timeout=` is killed and raises `OperationTimeoutError` (its `.timeout` holds the
limit), a subclass of the `NativeCallError` a missing or unstartable program raises: catch
`OperationTimeoutError` first to tell the two apart.

CLI: `pwshpy exec -- git status` (put the program after `--` so its flags are not parsed by pwshpy).
A run that hits `--timeout` exits 110 (ETIMEDOUT) with an `OperationTimeoutError` line on stderr; a
program that cannot start exits 1.

## Windows Update search that cannot hang forever

`IUpdateSearcher.Search` is a synchronous COM call that can sit for 25+ minutes (measured against
an empty update datastore), and nothing inside the process can interrupt it. `ps.get_windows_update`
runs the search in a child process and kills it once `timeout` seconds pass:

```python
from pwshpy import OperationTimeoutError, ps

if __name__ == "__main__":  # required: the search child re-imports this script
    try:
        pending = ps.get_windows_update(timeout=600).to_list()  # IsInstalled=0 and Type='Software'
    except OperationTimeoutError as exc:
        print(f"gave up after {exc.timeout}s")
```

Read-only: only `Search` is called, never Download or Install. `criteria=` takes any WUA search
string (`"IsInstalled=1"` lists what is installed). The timeout must be in `(0, 2147483]` seconds
(`ValueError` otherwise, at the call); the search itself starts when the pipeline is first iterated.
A timed-out search raises `OperationTimeoutError` (its `.timeout` holds the limit), a subclass of the
`NativeCallError` a Windows Update agent failure raises (its HRESULT is in the message): catch
`OperationTimeoutError` first to tell the two apart.

The child is started with the `spawn` method on every OS, which re-imports the calling script in
the child. A script that calls `get_windows_update` at module level without an
`if __name__ == "__main__":` guard therefore fails in the child at bootstrap, reported at once as
`NativeCallError` ("worker exited without a result"); put the call under the guard (the `pwshpy`
CLI already does).

CLI: `pwshpy get_windows_update --timeout 600 --jsonl`; a timeout prints one `timed out` line on
stderr and exits **110** (ETIMEDOUT).

### Waiting for the whole process tree (`wait_tree=True`, like `Start-Process -Wait`)

By default `ps.exec` waits for the program it started, like `subprocess.run`. An installer or
launcher that hands its work to a child (`setup.exe` starting `SetupHost`, a script that starts a
background job) then reads as finished while the work still runs. PowerShell's
`Start-Process -Wait` waits for the whole tree; `wait_tree=True` does the same:

```python
r = ps.exec(["setup.exe", "/quiet"], wait_tree=True, timeout=3600)
```

`ps.exec` then returns only after the program AND every process it started have exited, and
`timeout=` covers that whole wait: on expiry every process in the tree is killed and
`OperationTimeoutError` is raised, so nothing is left running. `exit_code` is still the started
program's own. The CLI flag is `pwshpy exec --wait-tree -- setup.exe /quiet`.

What counts as "the tree" depends on what the OS can track:

| OS      | Mechanism                                                                                                | Waited for                                                                                  | Not waited for                                                                               |
|---------|----------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| Windows | A Win32 job object; the program is created suspended and joins the job before it can start anything      | Every descendant, `DETACHED_PROCESS` / `CREATE_NEW_PROCESS_GROUP` children included         | A child created with `CREATE_BREAKAWAY_FROM_JOB` (allowed, so such a program keeps working)  |
| Linux   | A helper process that is a child subreaper (`PR_SET_CHILD_SUBREAPER`) and reaps until it has no children | Every descendant, double-forked `setsid` daemons included                                   | Nothing that descends from the program                                                       |
| macOS   | A helper process that starts the program in a new session and waits until that process group is empty    | Descendants that stay in the program's process group (ordinary children, `&` jobs, `nohup`) | A descendant that calls `setsid` or daemonizes: it leaves the group (macOS has no subreaper) |

On every OS, work handed to a service manager (systemd, launchd, the Windows service control
manager, Task Scheduler, an out-of-process COM server) is not a descendant of the program and is
not waited for.

On POSIX the helper is a small stdlib-only script that pwshpy runs with the current interpreter
(`sys.executable -I`); it exists because becoming a subreaper and reaping every child would change
process-wide state of the program that imports pwshpy. With `wait_tree=True` on macOS the program
runs in a new session, so it has no controlling terminal.

Output: stdout and stderr are captured as on the default path, including whatever descendants
write to the pipes they inherited, until the tree is gone. A process outside the tree (the
"Not waited for" column) may still hold those pipes; the result is then returned about two
seconds after the tree exited, with the output read up to that point.

## Predictable, memory-bounded file I/O

No more UTF-16LE-with-a-BOM surprises from `Out-File`; the bytes are identical on every OS and
Python version. And every write path streams, so a huge source never buffers:

```python
ps.write_text("report.txt", text)  # UTF-8, no BOM, LF - guaranteed
ps.write_text_stream("out.txt", chunks)  # same guarantee, streamed chunk-by-chunk
ps.write_records("procs.jsonl", ps.get_process())  # typed records -> UTF-8 JSONL, one at a time

for line in ps.get_content_lines("huge.log"):  # reads one line at a time (vs whole-file get_content)
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
cred = ps.get_credential("svc")  # non-echoing prompt
ps.save_credential("prod-db", "svc", cred.secret.get_secret_value())
later = ps.load_credential("prod-db")  # None if absent; secret stays masked
```

CLI: `pwshpy save_credential TARGET USER` (secret from a hidden prompt, never an argv flag),
`pwshpy load_credential TARGET` (secret shown masked), `pwshpy delete_credential TARGET`.

## Ship a script as one file - `ps.pack_script`

Handing a Python tool to another admin normally means "install Python, clone this, make a venv,
pip install, then run it". Packing turns all of that into one file they can just run - a `.ps1` for
Windows or a `.sh` for POSIX: it unpacks itself into a per-user cache, installs `uv` if the machine
has none, runs the script, and exits with the script's own exit code. Nothing needs to be installed
first, not even Python.

```python
from pwshpy import ps, PackOptions, RunnerFormat

ps.pack_script("tool.py")  # -> tool.ps1, next to the entry (auto: PowerShell by default)
ps.pack_script("tool.py", "tool.sh")  # -> a POSIX shell runner (auto-detected from the .sh suffix)
ps.pack_script("tool.py", "dist/tool.ps1", options=PackOptions(with_packages=["rich"], force=True))
ps.pack_script("tool.py", "run", options=PackOptions(format=RunnerFormat.SH))  # force .sh regardless of name
ps.unpack_script("dist/tool.ps1", "src/")  # edit it and pack again - reads .ps1 OR .sh
```

The how-to-pack choices live on one `PackOptions` value object (`include`, `with_packages`,
`root`, `force`, `format`), so the call stays `pack_script(entry, dest, options=...)` rather than a
long keyword list. `format` is `RunnerFormat.AUTO` by default (pick from the `-o` extension - `.sh`
gives a shell runner, anything else a `.ps1`); `RunnerFormat.PS1` / `RunnerFormat.SH` force it.

```bash
pwshpy pack tool.py -o dist/tool.ps1        # embed the entry + the local modules it imports
pwshpy pack tool.py -o dist/tool.sh         # a POSIX shell runner instead (auto-detected)
pwshpy pack tool.py -o run --format sh      # force the shell runner for a name without a suffix
pwshpy pack tool.py --include data.json     # for what import analysis cannot see
pwshpy unpack dist/tool.sh -o src/          # restore the sources (format sniffed automatically)
```

**The two runners are the same feature, one per world.** A `.ps1` runs on Windows PowerShell 5.1 and
pwsh 7; a `.sh` runs under **any POSIX `/bin/sh`, not only bash** - dash, busybox ash, macOS `sh`,
bash-as-sh, and regardless of your login shell (zsh, fish, ...) - because the runner is strict POSIX.
Both embed the same sources and the same PEP 723 metadata, so which one you ship is purely which
machine you are shipping to.

**Pack and unpack are cross-OS - the format is about the TARGET, not the host you pack on.** The
whole pipeline is Python (`zipfile`/`tarfile` + base64), so you can build a `.sh` on Windows and a
`.ps1` on Linux, and `unpack` reads either format on either OS (it sniffs zip vs tar - it is never
told the format). Only *running* the artefact needs the matching interpreter on the recipient's box.

The entry and every local module it imports (transitively, including relative imports) are embedded.
The standard library rides along with the interpreter uv provisions, and third-party dependencies
come from the entry's [PEP 723](https://peps.python.org/pep-0723/) block or `--with` - **never**
guessed from an import name, because an import name usually is not a package name (`yaml` is PyYAML,
`cv2` is opencv-python). Imports that are neither stdlib nor local are listed in the manifest, and a
script that imports third-party modules while declaring nothing at all gets a warning.

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["rich", "httpx"]
# ///
```

With that block, uv resolves the dependencies **and** fetches a matching interpreter on the target
machine, so "no Python installed" is not a problem either.

On the recipient's side the artefact takes a few switches of its own; everything else is forwarded
to your script untouched. The `.ps1` spells them `-PwshPy...`; the `.sh` spells the same switches
`--pwshpy-...`:

| `.ps1` switch        | `.sh` switch             | Effect                                                                          |
|----------------------|--------------------------|---------------------------------------------------------------------------------|
| `-PwshPyInfo`        | `--pwshpy-info`          | print the manifest (entry, hash, file list, cache dir) without running anything |
| `-PwshPyClean`       | `--pwshpy-clean`         | discard the cached extraction and unpack again                                  |
| `-PwshPyNoInstallUv` | `--pwshpy-no-install-uv` | fail with exit 127 instead of installing uv                                     |
| `-PwshPyElevate`     | `--pwshpy-elevate`       | relaunch elevated first (UAC on Windows, `sudo` on POSIX); exit 77 if refused   |
| `-PwshPyUv PATH`     | `--pwshpy-uv PATH`       | run the uv at this absolute path, no search and no install; exit 64 if relative |
| `-PwshPyHelp`        | `--pwshpy-help`          | show the artefact's own help                                                    |

Details worth knowing:

- **The `.ps1` runs on Windows PowerShell 5.1 and pwsh 7** (any OS); **the `.sh` runs under any
  POSIX `/bin/sh`** - dash, busybox ash, macOS `sh`, bash - not only bash, and independent of your
  login shell.
- **Arguments arrive exactly as typed**, including empty strings, embedded quotes, and unicode. On
  the `.sh` that is free - POSIX `sh` forwards `"$@"` untouched, so there is no shim. On the `.ps1`
  the argument vector travels out of band (because 5.1 re-quotes arguments on their way to a native
  executable), which also protects `-v` / `-d` from binding to `-Verbose` / `-Debug`. One caveat is
  outside pwshpy's reach: `pwsh -File tool.ps1 --path=C:\dir` is split by PowerShell's own
  `-Name:Value` parsing before the script sees it - use `--path C:\dir`, or call `.\tool.ps1` from a
  PowerShell prompt, where it arrives intact.
- **Nothing the `.ps1` hands a program is globbed, on any OS.** pwsh on Linux and macOS expands
  wildcards and a leading `~` in every argument to a native program that is not a quoted literal
  in the source, a variable's value included, so the runner writes each call (`sudo` for
  `-PwshPyElevate`, `uv run`) out as single-quoted literals: a `'*.log'` argument reaches the
  script through the sudo relaunch as `*.log`, a `--with 'pkg[extra]'` reaches uv as given, and a
  cache path holding `?` runs the verified entry, never a sibling directory the pattern matches.
  The `.sh` quotes every expansion (`"$@"`), keeps its list of `--with` arguments in one
  single-quoted word (a `$` or a backtick in a dependency spec is not expanded), and never globs
  its list of packed file names.
- **The `.ps1`'s `-PwshPyElevate` child on Windows is a fresh process.** UAC builds its environment
  from your profile, so variables set only in the calling shell (`PWSHPY_PACK_CACHE` among them) do
  not reach it. It is started as `powershell.exe -ExecutionPolicy Bypass -File runner.ps1 ...`, so
  the `-Name:Value` caveat above applies to the elevated run even when you called the runner from a
  PowerShell prompt. Its own console window closes when it exits, so everything it has to say
  comes back through the parent: the script's stdout and stderr on the parent's stdout and stderr,
  and an error it hits before the script starts (a corrupt payload, a cache it cannot write, a
  failed `uv` install) as a `pwshpy: the elevated child failed before running the script: <error>`
  line on stderr with exit code 70. Only a child stopped before it can run a line of the runner (a
  Group Policy execution policy, a closed window) leaves nothing to relay; then the parent prints a
  `pwshpy: the elevated child produced no output` line and exits with the child's code.
- **An elevated run only runs a uv no other account can modify.** Run with an administrator token,
  as SYSTEM (a scheduled task) or as root, the runner executes uv with those rights, so a `uv` that
  another account can replace would hand that account the machine. What decides is who can write the
  binary and the directories above it, not where it was found: every `uv` on `PATH` (in order) and
  every fixed install location is judged, and the trusted accounts are the running one, SYSTEM,
  Administrators, LOCAL SERVICE, NETWORK SERVICE and TrustedInstaller (by SID, the running one taken
  from the process token, so a German `Administratoren` reads the same). The file and the directory
  holding it must be owned by a trusted account and grant no other account write, append, delete,
  change permissions or take ownership (on the directory also adding or removing entries). Every
  directory above, up to the root, must be owned by a trusted account and grant no other account
  delete, delete-child, change permissions or take ownership, the rights that would let it rename
  the directory below away and put its own in place; adding entries is harmless there (`C:\` grants
  Authenticated Users create-folder). Users, Authenticated Users, Everyone or another user holding
  such a right refuses the `uv`. So an elevated administrator's own per-user or winget `uv` keeps
  working, a locked `C:\Tools\bin` under a `C:\Tools` that inherits Authenticated Users Modify is
  refused, and under SYSTEM the running account adds nothing: a `uv` planted in a machine-`PATH`
  directory that non-administrators can write is refused. On POSIX the running account is root: root
  owns the file, its directory and every directory above; the file and its directory allow no group
  or other write at all (group 0 included); a directory above may be group or world writable only
  with the sticky bit (`/tmp`), which stops anyone but an entry's owner from renaming it; and an ACL
  is refused wherever it shows (`+` from GNU `ls`; on macOS, where `@` takes that column, `ls -le`
  is asked; busybox shows neither, but on Linux an ACL that grants write shows as group write
  through its mask). Every symlink on the way (the `uv` itself, a `PATH` entry, `/bin -> usr/bin` on
  a merged-`/usr` system) is resolved where it is met, its owner judged and the directories it leads
  through judged like the rest. As root on POSIX the runner also runs its own helpers (`ls`, `id`,
  `tar`, `base64`, `sha256sum`, the installer's `curl` and `sh`, ...) only from the system
  directories
  `/usr/bin:/bin:/usr/sbin:/sbin:/run/current-system/sw/bin:/run/current-system/profile/bin` (not
  `/usr/local`, which local installs populate), never from the `PATH` it was given, and asks the
  euid of an `id` in those same directories, never one on `PATH`; when none answers with a uid, the
  run is treated as root (it fails closed: such a system's non-root caller loses only the per-user
  `uv` lookup, a root run never loses the rule). The caller's `PATH` is still searched for `uv`
  (each hit judged) and handed back to the packed script. A refused `uv` is skipped with a `pwshpy:
  skipping uv at <path>: <reason>` line on stderr and the next one is tried; an elevated `.ps1`
  child relays those lines to the parent. A `-PwshPyUv` / `--pwshpy-uv` path is used as given when
  not elevated; elevated it must pass the same check or the run stops with `pwshpy: refusing the uv
  given with ...` and exit 127, without searching further. Not elevated, nothing changes: the first
  `uv` on `PATH` runs. Accepted residual risk: a process already running as the same account could
  swap that account's `uv`, before the elevated run starts or between its check and its exec; no
  other account can.
- **An elevated run only runs a Python uv manages.** uv picks the interpreter the script runs on,
  and it executes every interpreter it considers to ask its version, so a `python` planted in a
  `PATH` directory another account can write would run with the elevated rights. Elevated (an
  administrator token, SYSTEM, or root, decided exactly as for the `uv` check above), the runner
  calls `uv run --python-preference only-managed`, so uv looks only at the Pythons it installed
  itself (under the running account's own profile: `%APPDATA%\uv\python`, `~/.local/share/uv/python`).
  A machine with none gets one on the first elevated run: uv downloads a CPython build (a few tens
  of megabytes) by itself, unless uv's Python downloads are switched off
  (`UV_PYTHON_DOWNLOADS=never`, `python-downloads = "never"`), in which case uv stops with `No
  interpreter found in virtual environments or managed installations` and a hint naming the
  setting; `uv python install` as the elevated account provides one ahead of time. Three more
  ways to name an interpreter outright would beat that flag, and an elevated run closes each:
  the `.python-version` and `uv.toml` uv reads from the script's directory upwards (the cache, which
  `PWSHPY_PACK_CACHE` can put anywhere) are looked up from `/` on POSIX or `System32` on Windows
  instead (`--project`), directories only administrators can write; and `UV_PYTHON`, `VIRTUAL_ENV`,
  `CONDA_PREFIX`, `UV_MANAGED_PYTHON` and `UV_NO_MANAGED_PYTHON` are dropped from uv's environment,
  each with a `pwshpy: ignoring <NAME>` line on stderr. Not elevated, nothing changes. The
  `-PwshPyUv` / `--pwshpy-uv` pin is unaffected: it chooses the `uv`, not the Python.
- **Self-elevation keeps working.** A packed script calling `ps.elevate()` re-execs the extracted
  entry with the environment uv already built, so it elevates exactly as it would unpacked.
- **The cache is verified before every run** against per-file hashes embedded in the artefact, and
  re-extracted on any mismatch - a cache directory that may later be executed elevated must not be
  tamperable between runs. Both runners serialize a cold-cache extraction with an atomic lock
  (`FileShare.None` on Windows, a lock file created under `noclobber` on POSIX), so two concurrent first runs never race.
- **Packing is deterministic**: identical sources produce identical bytes (the `.sh` tar is built
  with fixed mtime/mode/order), so the payload hash is a stable cache key and two builds are
  comparable.

## .NET - safe cmdlet binding and parameter discovery

`ps.cmdlet` binds every parameter as data (never string-interpolated into a script), captures all
six streams, and `ps.get_command` tells you what parameters a cmdlet takes:

```python
result = ps.cmdlet("Get-ChildItem", Path="C:/logs", Recurse=True)  # safe bound params
result.output, result.warnings, result.errors  # every stream, typed

info = ps.get_command("Get-Item")  # typed introspection
[p.name for p in info.parameters if p.mandatory]  # discover mandatory params
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
