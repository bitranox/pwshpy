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

`timeout=` takes seconds in `(0, 2147483]` (the longest wait every OS can honour); anything else,
NaN included, raises `ValueError` before the program starts. A run that outlives `timeout=` is
killed and raises `OperationTimeoutError` (its `.timeout` holds the limit), a subclass of the `NativeCallError` a missing or unstartable program raises: catch
`OperationTimeoutError` first to tell the two apart. The output the program wrote before it was
killed is not returned; the error carries the message and `.timeout` only. A program that runs and
exits nonzero is no error: the result holds its `exit_code` until you call `.check()`.

CLI: `pwshpy exec -- git status` (put the program after `--` so its flags are not parsed by pwshpy).
Piped or redirected, the program's stdout and stderr are written as UTF-8 bytes whatever the console
code page (see [CLI Reference](cli-reference.md#portable-record-commands)).
A run that hits `--timeout` exits 110 (ETIMEDOUT) with an `OperationTimeoutError` line on stderr; a
program that cannot start exits 1. A `--timeout` outside `(0, 2147483]` is a usage error (exit 2), and
`nan` exits 22 (87 on Windows), the code for a `ValueError`.

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
string and is passed to `IUpdateSearcher.Search` verbatim (`"IsInstalled=1"` lists what is
installed; a malformed string is the agent's error, raised as `NativeCallError` with its HRESULT
on `.hresult`).
The timeout must be in `(0, 2147483]` seconds (`ValueError` otherwise, at the call). The search
starts when the pipeline is first iterated, and it is not streamed: the agent returns the whole
result before the first record is yielded, so `.take(n)` or `.first()` does not make it shorter.
A timed-out search raises `OperationTimeoutError` (its `.timeout` holds the limit), a subclass of the
`NativeCallError` a Windows Update agent failure raises (its HRESULT is in the message and in
`.hresult`; an `E_ACCESSDENIED` search raises the subclass `NativeAccessDeniedError`): catch
`OperationTimeoutError` first to tell the two apart.

The child is started with the `spawn` method on every OS, which re-imports the calling script in
the child. A script that calls `get_windows_update` at module level without an
`if __name__ == "__main__":` guard therefore fails in the child at bootstrap, reported at once as
`NativeCallError` ("worker exited without a result"); put the call under the guard (the `pwshpy`
CLI already does).

CLI: `pwshpy get_windows_update --timeout 600 --jsonl`; a timeout prints one `timed out` line on
stderr and exits **110** (ETIMEDOUT).

### Selecting updates by category

Each `WindowsUpdate` record carries its categories twice: `categories` holds the display names,
which the Windows Update agent may localize, and `category_details` holds one `UpdateCategory` per
category, in the same order, with the locale-independent identity:

- `category_id`: the category GUID (`ICategory.CategoryID`), always lower case.
- `type`: the agent's untranslated kind, e.g. `UpdateClassification` or `Product`.
- `name`: the display name (the same text as the matching `categories` entry).

Select by `category_id`, never by `name`, so a script behaves the same on every Windows language.
The classifications most scripts filter on:

| Classification     | `category_id`                          |
|--------------------|----------------------------------------|
| Security Updates   | `0fa1201d-4330-4fa8-8ae9-b877473b6441` |
| Critical Updates   | `e6cf1350-c01b-414d-a61f-263d14d133b4` |
| Definition Updates | `e0789628-ce08-4437-be74-2495b842f43b` |
| Update Rollups     | `28bc880e-0592-4cbf-8f95-c79b17911d5f` |
| Updates            | `cd5ffd1e-e932-4e3a-bf74-18bf0b1bbd83` |

They are Microsoft's
[WSUS Classification GUIDs](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ff357803(v=vs.85)),
which Microsoft publishes in upper case; pwshpy lower-cases every `category_id`, so compare against
the lower-case form.

```python
from pwshpy import ps

SECURITY_UPDATES = "0fa1201d-4330-4fa8-8ae9-b877473b6441"

if __name__ == "__main__":
    security = (
        ps.get_windows_update(timeout=600)
        .where(lambda u: any(c.category_id == SECURITY_UPDATES for c in u.category_details))
        .to_list()
    )
```

The agent can also filter for you: a `criteria=` of
`"IsInstalled=0 and CategoryIDs contains '0fa1201d-4330-4fa8-8ae9-b877473b6441'"` returns only the
security updates not yet installed (the GUID matches in either case there).

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

CLI: `pwshpy write_text PATH` (reads stdin), `pwshpy get_content PATH` (streams; UTF-8 bytes when
piped, see [CLI Reference](cli-reference.md#portable-record-commands)), `pwshpy download_file URL
DEST`. `write_text` reads a piped stdin as UTF-8 on every OS, whatever the console code page, which
is what pwsh 7 and pwshpy itself send into a pipe; a leading UTF-8 BOM in the input is dropped. Pass
`--input-encoding cp1252` (or any text encoding name) for a piped source that writes another
encoding. Bytes that are not valid in that encoding stop the command with an error naming
`--input-encoding`, and PATH then holds part of the text before them. A terminal stdin keeps its own
encoding, with or without the option. `--encoding` names the encoding of the file written.

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

| `.ps1` switch        | `.sh` switch             | Effect                                                                                                                              |
|----------------------|--------------------------|-------------------------------------------------------------------------------------------------------------------------------------|
| `-PwshPyInfo`        | `--pwshpy-info`          | print the manifest (entry, hash, file list, cache dir; the `.sh` also prints its uv args and `git source`) without running anything |
| `-PwshPyClean`       | `--pwshpy-clean`         | discard the cached extraction and unpack again                                                                                      |
| `-PwshPyNoInstallUv` | `--pwshpy-no-install-uv` | fail with exit 127 instead of installing uv                                                                                         |
| `-PwshPyElevate`     | `--pwshpy-elevate`       | relaunch elevated first (UAC on Windows, `sudo` on POSIX); exit 77 if refused                                                       |
| `-PwshPyUv PATH`     | `--pwshpy-uv PATH`       | run the uv at this absolute path, no search and no install; exit 64 if relative                                                     |
| `-PwshPyHelp`        | `--pwshpy-help`          | show the artefact's own help                                                                                                        |

Details worth knowing:

- **The `.ps1` runs on Windows PowerShell 5.1 and pwsh 7** (any OS); **the `.sh` runs under any
  POSIX `/bin/sh`** - dash, busybox ash, macOS `sh`, bash - not only bash, and independent of your
  login shell.
- **Arguments arrive exactly as typed**, including empty strings, embedded quotes, and unicode. On
  the `.sh` that is free - POSIX `sh` forwards `"$@"` untouched, so it needs no argument shim (a root
  run goes through the generated shim only to return to your directory, see below). On the `.ps1`
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
  not reach it. It is started with the PowerShell that runs the runner (`powershell.exe` under
  Windows PowerShell 5.1, `pwsh.exe` under pwsh 7) as `-NoProfile -ExecutionPolicy Bypass -File
  runner.ps1 ...`, so the `-Name:Value` caveat above applies to the elevated run even when you
  called the runner from a PowerShell prompt. Its own console window closes when it exits, so everything it has to say
  comes back through the parent: the script's stdout and stderr on the parent's stdout and stderr,
  and an error it hits before the script starts (a corrupt payload, a cache it cannot write, an
  error the `uv` installer throws) as a `pwshpy: the elevated child failed before running the
  script: <error>` line on stderr with exit code 70. A `uv` that is still missing or refused after
  that exits 127, as in a direct run, and a script that itself exits 70 gives the same code: the
  stderr line is what tells them apart. Only a child stopped before it can run a line of the runner (a
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
  through judged like the rest. On Windows an elevated run never trusts a network (UNC) path, for a
  `uv` or for a `PATH` directory: `\\server\share\...` in any separator, its `\\?\UNC\` and
  `\\.\UNC\` long forms and a link whose target is one are refused with `it is a network path, and
  an elevated run trusts only local paths`, whatever ACL the server reports, because whoever runs
  the server decides what the share holds (a drive letter mapped to a share is not detected). Any
  other device-namespace path (`\\.\...`, or `\\?\...` other than the long form of a drive path,
  `\\?\C:\...`) is refused with `it is a device path, and an elevated run trusts only local drive
  paths`: the object manager resolves such a name (`\\?\GLOBALROOT\Device\Mup\...` reaches the
  network redirector), and neither a `uv` nor a `PATH` directory lives there. As root on POSIX the
  runner also runs its own helpers (`ls`, `id`, `tar`, `base64`, `sha256sum`, the installer's `curl`
  and `sh`, ...) only from the system directories
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
- **An elevated run gives uv only the `PATH` directories no other account can modify.** uv runs
  `git` for a git dependency (a PEP 723 git source or a `--with` value), and a build backend may run
  other tools, by searching `PATH`, so a `git` planted in a `PATH` directory another account can
  write would run with the elevated rights. Elevated (an administrator token, SYSTEM, or root,
  decided exactly as for the `uv` check above), the runner judges each directory of the caller's
  `PATH` by the same rule as a `uv` (on POSIX root alone; on Windows the trusted accounts listed
  above) and starts uv on the ones that pass, in their order. Each directory left out is named on
  stderr with a `pwshpy: leaving <dir> off uv's PATH: <reason> (...)` line. A relative entry names
  the working directory and is left out like any untrusted one. In a `.sh` run a leading or inner
  empty entry is named and left out the same way; a trailing one is dropped by the shell's field
  splitting without a line, and is left off all the same. The `.ps1` drops every empty entry
  silently (a trailing `;` is common on Windows). A root run judges the `PATH` while its own helpers
  still come from the fixed system directories. When the caller's `PATH` holds a `git` but every one
  is in a directory left off, one more line names the cause, since uv would otherwise fail with only
  its bare "git not found": `pwshpy: no git on uv's PATH: <git> and any other git on the caller's
  PATH are in directories another account can modify; a git dependency fails as root until git is
  installed in a directory only root can modify` (on Windows: `fails elevated until git is installed
  in a directory no other account can modify (e.g. a system-wide install)`). That lookup only
  searches `PATH`; it never runs a `git`. A git dependency in an elevated run therefore needs a
  `git` in a directory no other account can modify, such as a system-wide install (on POSIX one only
  root can modify). The packed script still sees the caller's whole `PATH`: the shim puts it back
  before the script starts, behind the entry uv put in front of the `PATH` it was given (the script
  environment's `bin` (`Scripts` on Windows), or the interpreter's directory for a script with no
  dependencies), so the script sees the same `PATH` as an unelevated run. One exception: when no
  directory of the caller's `PATH` passes, uv's `PATH` is empty, and Windows PowerShell 5.1 deletes
  an environment variable set to an empty string. Under 5.1 the shim therefore cannot tell which
  entry uv put in front and gives the script the caller's `PATH` without uv's entry. pwsh 7 and a
  `.sh` run keep the empty variable, so the script keeps uv's entry there too. Either way nothing is
  added beyond the caller's own `PATH`. The two variables that carry it over (`PWSHPY_PACK_PATH`,
  `PWSHPY_PACK_UV_PATH`) are removed before the script starts. Not elevated, nothing changes: uv and
  the script get the caller's `PATH` as it is.
- **An elevated run only runs a Python uv manages, named to uv outright.** uv picks the interpreter
  the script runs on, and it executes every interpreter it considers to ask its version, so a
  `python` planted in a `PATH` directory another account can write would run with the elevated
  rights; and `uv run` probes a `.venv` in its working directory and EVERY directory above it, and
  under `--python-preference only-managed` even runs the script on that venv's `python` (measured
  with uv 0.13). Elevated (an administrator token, SYSTEM, or root, decided exactly as for the `uv`
  check above), the runner therefore resolves the interpreter itself before the run: `uv python find
  --system --python-preference only-managed`, which probes no `.venv` at all, with the packed
  script's `requires-python` as the request (read from its PEP 723 block when packing; `uv run`
  would only warn about an interpreter outside it). uv's managed Pythons are the ones in the running
  account's own data directory, which the runner names to uv itself (`UV_PYTHON_INSTALL_DIR`):
  `<RoamingAppData>\uv\python` on Windows, the RoamingAppData known folder of the running token, and
  `<home>/.local/share/uv/python` on POSIX, the home the passwd database records for the euid (asked
  of `getent`, `id -P` or `/etc/passwd` in the fixed helper directories; no answer stops the run
  with exit code 70), whatever `APPDATA`, `HOME` or `XDG_DATA_HOME` say. A machine with none gets
  one on the first elevated run: the runner calls `uv python install --no-bin` (plus `--no-registry`
  on Windows), which downloads a CPython build (about 36 MiB, unpacked to about 100 MB, hence a slow
  first run and a failure without network access), unless `UV_PYTHON_DOWNLOADS` is `never` or
  `manual`, in which case the run stops with uv's own `No interpreter found in managed
  installations` and its exit code 2; `uv python install` as the elevated account provides one ahead
  of time. `uv python find` executes every interpreter it considers in that directory to ask its
  version, so a `.pth` file planted in an install's `site-packages` would run with the elevated
  rights before any check of the path uv answers. The runner therefore judges the directory before
  uv starts, as it judges a `uv`: the directory itself and every directory above it (one that does
  not exist yet by the nearest one that does, where `uv python install` creates it), and then every
  file and directory below it, each install with its standard library and `site-packages` included.
  On POSIX root must own each entry, neither its group nor anyone else may write it, and it may
  carry no ACL; a symlink is judged by its owner (its target inside the directory where it lies),
  and a regular file named `.lock` is exempt from the write check (uv creates its lock file mode
  0666 and only ever locks it). On Windows no entry may let an account other than the trusted ones
  change it, with the rights of its kind; a link (symlink or junction) is judged as the link and
  never followed. One another account can modify stops the run with `refusing uv's managed-Python
  directory <dir>: <reason>` (the reason names the entry) and exit code 127, and uv never runs.
  Measured, read only: on a Linux host its two listings took about 0.8 s for 85,640 entries (15
  installs); on a Windows host about 2 to 2.5 s per install under PowerShell 7 and 3 to 3.5 s under
  Windows PowerShell 5.1 (9.9 to 11.3 s and 13.8 to 17.9 s over three runs for five installs, 18,771
  entries), paid by every elevated `.ps1` run there. Paths longer than 260 characters are read in
  their extended-length form, which Windows PowerShell 5.1 needs. The path uv then finds must pass
  the same check (see above, the directories above it included), or the run stops with `refusing the
  Python uv found at <path>: <reason>` and exit code 127. Both refusals name the remedy: on POSIX
  make the path named in the reason root's alone (`chown root`, then `chmod go-w`, with `-R` for a
  directory), on Windows take every other account's write access off it; or remove uv's
  managed-Python directory and install the Python again elevated (`uv python install`). On Windows a
  refusal whose reason is the network rule's alone names no remedy but says `the running account's
  AppData is on a network share; an elevated run cannot use it`: that reason means RoamingAppData is
  redirected to a share, and then no elevated `.ps1` run is available, since neither an ACL change
  nor a fresh install makes the share local (such a data directory already stops the run at the git
  configuration file, see below). A link to a share met below a local data directory is refused too,
  its reason naming the link and where it points, but it keeps the usual remedy: the data directory
  is not redirected then, and removing the link works. The run is
  then `uv run --no-config --python-preference only-managed --no-env-file --python <that path>`,
  started in `/` on POSIX or `System32` on Windows (`--directory`, `--project`), so the
  `.python-version` uv would read from the script's directory upwards (the cache, which
  `PWSHPY_PACK_CACHE` can put anywhere) and every `.venv` above the caller's directory or above
  `System32` (any authenticated user can create `C:\.venv`) are never consulted. `--no-config`, on
  every uv call an elevated run makes (`uv run`, `uv python find`, `uv python install`), means no
  `uv.toml` is read at all: not the system-wide one (`%SYSTEMDRIVE%\ProgramData\uv\uv.toml` on
  Windows, `/etc/uv/uv.toml` or one under `XDG_CONFIG_DIRS` on POSIX), not the user's, and not one
  above the starting directory, since any of them could name a cache directory, a package index or a
  Python download mirror. With the variables that name the same dropped too (listed below) and
  `--no-env-file` on `uv run` (no `.env` file, whatever `UV_NO_ENV_FILE` says), an elevated run uses
  uv's defaults plus the runner's own flags and directories (user decision): the default package
  index, no find-links location or insecure host, and the running account's own cache directory,
  which the runner names to uv (`UV_CACHE_DIR`, with a `pwshpy: uv caches in <dir>` line on stderr)
  the way it names the managed-Python directory, so not uv's default, which would follow
  `XDG_CACHE_HOME` or `HOME` (POSIX; `sudo -E` or `env_keep` can hand both over) or `LOCALAPPDATA`
  (Windows): `<LocalAppData>\uv\cache` on Windows, the LocalAppData known folder of the running
  token, and `<home>/.cache/uv` on POSIX, the home the passwd database records. A custom install
  directory, download mirror, package index or cache directory cannot be configured for it. A
  directory named `C:\uv.toml` therefore no longer stops elevated `.ps1` runs. The packed script
  still runs in your directory: uv runs the generated shim, which changes back to it first, or stops
  with `pwshpy pack: cannot return to the working directory <dir>` and exit code 70 when the
  elevated account cannot enter it (a root-squashed NFS export). What you gave relative to your
  directory is made absolute before uv starts elsewhere: a relative `PWSHPY_PACK_CACHE`, and a
  `--with` value that names one relative path: `./x.whl`, `../x`, `dist/x.whl`, a bare distribution
  file name (`x.whl`, `x.tar.gz`) and `name @ ./x` (any form holding a directory separator). Not
  rewritten, so in an elevated run they still resolve against uv's starting directory: a
  comma-separated list (only its first entry is prefixed), `name @ dir` with a bare directory name,
  and a `file:` URL. Give such a value as an absolute path, one `--with` per entry. Dropped from the
  environment, each with a `pwshpy: ignoring <NAME>` line on stderr: `UV_PYTHON`, `VIRTUAL_ENV`,
  `CONDA_PREFIX`, `UV_MANAGED_PYTHON` and `UV_NO_MANAGED_PYTHON` (they name an interpreter),
  `UV_PYTHON_INSTALL_DIR`, `UV_CONFIG_FILE`, `UV_PYTHON_INSTALL_MIRROR`,
  `UV_PYTHON_DOWNLOADS_JSON_URL`, `UV_ASTRAL_MIRROR_URL`, `UV_PYPY_INSTALL_MIRROR`,
  `UV_GRAALPY_INSTALL_MIRROR` and `UV_PYODIDE_INSTALL_MIRROR` (they move where uv's managed Pythons
  live or come from; uv 0.13 downloads CPython through `UV_ASTRAL_MIRROR_URL` too); `PYTHONHOME`,
  `PYTHONPATH` and `PYTHONUSERBASE`, which the interpreter itself reads (another standard library,
  the caller's directories ahead of it, the caller's own site-packages), so the packed script does
  not see them either; and `UV_INDEX_URL`, `UV_DEFAULT_INDEX`, `UV_INDEX`, `UV_EXTRA_INDEX_URL`,
  `UV_FIND_LINKS`, `UV_INSECURE_HOST`, `UV_INSECURE_NO_ZIP_VALIDATION`, `UV_CACHE_DIR`,
  `UV_PYTHON_CACHE_DIR` and `UV_ENV_FILE` (where uv takes packages, how it checks a wheel's archive,
  its caches and an env file from; an env file can set `PYTHONPATH` again, and `uv run` loads the one
  `UV_ENV_FILE` names despite `--no-config`); and `HTTPS_PROXY`,
  `HTTP_PROXY` and `ALL_PROXY` with their lower-case forms, `SSL_CERT_FILE`, `SSL_CERT_DIR`,
  `SSL_CLIENT_CERT`, `UV_NATIVE_TLS`, `UV_SYSTEM_CERTS` and `UV_KEYRING_PROVIDER` (how uv connects:
  a proxy, which uv 0.13 sends its requests through in each of these spellings, the certificate
  roots it trusts, which `SSL_CERT_FILE` and `SSL_CERT_DIR` replace, the client certificate it
  presents, and a keyring helper it would run from `PATH`), so the packed script does not see these
  either; and every variable whose name starts with `GIT_` (on Windows in any case, as the
  environment's names are) and `SSH_ASKPASS`, because uv runs git for a git source (a PEP 723
  dependency or a `--with` value) and git reads them: a `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_<n>` /
  `GIT_CONFIG_VALUE_<n>` triple sets any git config key (measured with uv 0.13 and git 2.53: a
  `core.fsmonitor` command named that way ran from uv's git fetch), others name a command to run
  (`GIT_SSH_COMMAND`, `GIT_PROXY_COMMAND`, `GIT_ASKPASS`) or a CA to trust (`GIT_SSL_CAINFO`), and git
  runs `SSH_ASKPASS` for a password when `GIT_ASKPASS` and `core.askPass` are unset (measured on an
  HTTP 401 from uv's git fetch). The credential helper git runs when a server asks for a password
  reads variables of its own, so every variable whose name starts with `GCM_` (Git Credential
  Manager, Git for Windows' default helper: `GCM_GPG_PATH` names the `gpg` it runs,
  `GCM_PLAINTEXT_STORE_PATH` and `GCM_DPAPI_STORE_PATH` the directory it reads credentials from,
  `GCM_CREDENTIAL_STORE` the store) and `PASSWORD_STORE_DIR` (where its `gpg` store reads
  credentials) are dropped too. The drop goes by the prefix, so a variable a later git or Git
  Credential Manager release adds is dropped too. git's global configuration file is not read
  either: after the drop the runner sets `GIT_CONFIG_GLOBAL`, with a line on stderr, so neither the
  `.gitconfig` below the `HOME` (or, on Windows, `USERPROFILE`, which a UAC relaunch keeps as the
  caller's) nor `git/config` below `XDG_CONFIG_HOME` that the run was given applies (a
  `core.fsmonitor` command planted there ran from uv's git fetch before, measured). On POSIX it is
  `/dev/null`. On Windows it is an empty file the runner creates below the account's own data
  directory (the RoamingAppData known folder, as for uv's managed Pythons):
  `pwshpy\empty.gitconfig`, which Git for Windows 2.55 reads as an empty configuration (measured);
  not the `NUL` device, which an MSYS2 or Cygwin git may read as a file named `NUL` in its working
  directory. The file and every directory above it pass the same trust rule as a `uv`; when another
  account could change them, or the file is not empty, the run stops with `pwshpy: refusing the git
  configuration file <path>: <reason>` and exit code 127, naming the remedy (take the other
  account's write access off, or remove the directory so the next elevated run creates it). When the
  reason is the network rule's alone, that is the RoamingAppData known folder redirected to a share,
  the refusal says `the running account's AppData is on a network share; an elevated run cannot use
  it` instead: elevated `.ps1` runs are unavailable while it is redirected, because the next run
  would create the file on the same share. A link to a share met below a local data directory is
  refused too, its reason naming the link and where it points, and keeps the usual remedy, since
  removing the link works. The packed script inherits the variable, so a git it runs reads no global
  configuration file either: no `user.name`/`user.email` and no `credential.helper` from the
  elevated account's own `.gitconfig` (an elevated 1.5.x run read them). A script that needs them
  passes them itself (`git -c user.name=... -c user.email=...`) or points `GIT_CONFIG_GLOBAL` at a
  file it trusts before it runs git. git honours `GIT_CONFIG_GLOBAL` from 2.32 on; an older git
  ignores it and still reads those files. So a root `.sh` run of a script with a git source asks the
  git uv will run (the first on uv's filtered `PATH`) for its version before any uv call (so a
  refused run downloads no managed Python), and stops with
  `pwshpy pack: refusing git <version> at <path>: it ignores GIT_CONFIG_GLOBAL, ...` and exit code
  127 when it is older than 2.32, or with `pwshpy pack: refusing git at <path>: git --version names
  no version this runner can read, ...` when `git --version` fails or prints no `git version
  <major>.<minor>` it can read; the remedy it names is to upgrade git or to run the script without
  root. With no git on uv's `PATH` nothing is asked (uv then fails on its own); for a script without
  a git source the runner never runs git, and a run that is not root checks nothing. Whether the script has
  a git source is decided at pack time, and `--pwshpy-info` shows it
  (`git source : yes (a root run needs git 2.32 or later)`, or `no`):
  a `git+` (in any case) in a `--with` value, or, outside a TOML comment in the PEP 723 block,
  `git+` anywhere, a key named `git` anywhere (a `[tool.uv.sources]` entry such as
  `pkg = { git = "..." }` or a `[tool.uv.sources.pkg]` table), a `\u`, `\U` or `\x` escape, or a
  backslash followed only by whitespace to the end of a line (in a multi-line basic string it joins
  the next line on, so `pkg @ gi\` at the end of one line and `t+https://...` on the next spell
  `pkg @ git+https://...`); either of the last two could spell the first two. The rule over-detects
  on purpose, since a false positive only refuses a root run on a git older than 2.32: a `git+` in
  any other string, a `git` key in another tool's table and a line-ending backslash in a literal
  (`'''`) string, which joins nothing, count too. It reads only the script's own declarations, so a git requirement inside a local
  wheel or path dependency is not seen. An elevated `.ps1` run (an administrator token, SYSTEM, or
  root) of a script with a git source checks the git version the same way, with the same rule and
  pack-time flag (`-PwshPyInfo` shows `git source : yes (an elevated run needs git 2.32 or later)`):
  it stops with `pwshpy: refusing git at <path>: git <version> ignores GIT_CONFIG_GLOBAL, ...` (or
  `git --version names no version this runner can read, ...`) and exit code 127, naming the remedy
  to upgrade git or to run the script without elevation; Git for Windows' `2.x.y.windows.n` counts
  by its major and minor numbers. The
  system file (`/etc/gitconfig`, or the one below Git
  for Windows' install directory) is the administrators' and still applies. The runner cannot tell
  who set a variable, so an elevated run in a network that reaches the internet only through a proxy
  downloads nothing: the proxy can be named neither in the environment (root's own included) nor in
  a `uv.toml` (none is read), and the certificate of a TLS-inspecting proxy is not trusted. What
  still works there: a script with no dependencies (no PEP 723 `dependencies`, no `--with`) on a
  managed Python already in the account's managed-Python directory, installed beforehand as that
  account with the proxy set (`uv python install` with `UV_PYTHON_INSTALL_DIR` naming that
  directory); an unelevated run, which keeps every variable; and, by the same reasoning (not
  measured), a proxy that needs no setting on the machine, such as a transparent one. The user's own
  site-packages is switched off (`PYTHONNOUSERSITE=1`, which the packed script inherits): without
  `PYTHONUSERBASE` it comes from `HOME` (POSIX, which some `sudo` setups keep) or `APPDATA`
  (Windows), so a `.pth` the caller put there would otherwise run with the elevated rights. Not
  elevated, nothing changes: uv starts in your directory with your environment and configuration,
  and the `.sh` runs the entry itself. The `-PwshPyUv` / `--pwshpy-uv` pin is unaffected: it chooses
  the `uv`, not the Python.
- **Any file name packs.** The entry name and every packed file name land in the runner as quoted
  literals, so `it's.py`, a typographic quote, `$`, a backtick or a space in a name, or in a `--with`
  value, reaches the runner and uv as given. Refused at pack time, with the reason: a name holding a
  line break (both runners keep one name per line), and `#>` in the entry name of a `.ps1` (it would
  end the runner's help block, which has no escape). The `.ps1` is written as UTF-8 with a byte-order
  mark, without which Windows PowerShell 5.1 reads it in the ANSI code page and mangles every
  non-ASCII name and value.
- **Self-elevation keeps working.** A packed script calling `ps.elevate()` re-execs the extracted
  entry with the environment uv already built, so it elevates exactly as it would unpacked.
- **The cache is verified before every run** against per-file hashes embedded in the artefact, and
  re-extracted on any mismatch, so a file changed since an earlier run is replaced before uv starts.
  That check reads each file once; it does not stop an account that can write the cache directory
  from changing a file between the check and uv reading it. So an elevated run (an administrator
  token, SYSTEM, or root) also judges the cache directory the way it judges uv's managed-Python
  directory: the directory itself, every directory above it and every entry below it, before it
  removes or writes anything there (before the first unpack, the nearest directory that exists), and
  the tree again after an unpack. A cache another account can modify, wherever `PWSHPY_PACK_CACHE`,
  `LOCALAPPDATA`, `XDG_CACHE_HOME` or `HOME` put it, stops the run with `pwshpy: refusing the cache
  directory <dir>: <reason>` and exit code 127; a relative `PWSHPY_PACK_CACHE` is judged against
  the caller's directory. A root run under umask 002 writes a group-writable tree, which the root
  rule refuses (sudo's default umask is the caller's combined with 022, so a sudo run does not). Both runners
  serialize a cold-cache extraction with an atomic lock (`FileShare.None` on Windows, a lock file
  created under `noclobber` on POSIX), so two concurrent first runs never race.
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
