# CLI Reference

[Back to the README](../README.md) | **Prev:** [Power Tools](power-tools.md) | **Next:** [Backends & Platforms](backends-and-platforms.md)

The `pwshpy` command mirrors the library: same names, same surface. For a full cmdlet-by-cmdlet
translation table (grouped by theme, Python API and CLI in separate columns), see
[COMMANDS.md](../COMMANDS.md).

## Portable record commands

Every native record command shares `--json` (a streamed array), `--jsonl` (one record per line,
lazy end to end), and a default human table; most take `--limit N`.

On an error the exit code is non-zero and the message goes to stderr. `--json` writes nothing
until the source has produced its first record, so a command that fails up front (a missing
path, a refused native call) leaves stdout empty rather than a bare `[`. An error after records
were already streamed leaves a partial, unterminated array: that is the price of streaming, so
check the exit code before trusting the output, or use `--jsonl`, whose lines stay individually
parseable.

`--json` and `--jsonl` write UTF-8 bytes with LF line ends on every OS, whatever the console
code page, `PYTHONIOENCODING` or `PYTHONUTF8` say (JSON is UTF-8 by spec), so a consumer reading
the pipe decodes it as UTF-8 even from a cp1252 Windows console. The JSON that `run`, `cmdlet`,
`get_command` and `invoke_rest_method` print is written the same way. The human table and
`get_content` stay on the console's own encoding.

At a terminal the human table fits the terminal's width and may shorten long cells. Piped or
redirected (`pwshpy env | grep PATH`, `> out.txt`), it is as wide as its content instead, up to
8192 columns, so no cell is cut: every value is whole, and a value without line breaks stays on one
line. A table wider than 8192 columns folds its long values onto further lines rather than dropping
characters, breaking a value that holds spaces at a space. The table is for reading: use `--jsonl`
when a script needs a value's exact bytes. An explicit `COLUMNS` in
the environment still sets the width in both cases.

**Everywhere** - one binding on Windows, macOS and Linux:

| Command                         | Shows                         | Binding                                |
|---------------------------------|-------------------------------|----------------------------------------|
| `pwshpy get_process`            | running processes             | psutil                                 |
| `pwshpy get_net_tcp_connection` | open network connections      | psutil                                 |
| `pwshpy get_volume`             | mounted filesystems and usage | psutil                                 |
| `pwshpy get_uptime`             | boot time and elapsed uptime  | psutil                                 |
| `pwshpy resolve_dns_name NAME`  | resolved DNS addresses        | stdlib socket                          |
| `pwshpy test_connection HOST`   | TCP reachability probe        | stdlib socket                          |
| `pwshpy env`                    | environment variables         | os.environ                             |
| `pwshpy get_net_adapter`        | network interfaces            | psutil                                 |
| `pwshpy get_computer_info`      | machine summary               | stdlib/psutil                          |
| `pwshpy get_windows_image PATH` | images inside a WIM file      | stdlib + defusedxml (WIM header + XML) |

`get_process` names a process as psutil does, so two Windows pseudo-processes differ from
`Get-Process`: the memory compression store is `MemCompression` (`Get-Process`:
`Memory Compression`) and PID 0 is `System Idle Process` (`Get-Process`: `Idle`). Every other name
matches `Get-Process`'s `ProcessName`, `.exe` stripped.

`get_windows_image PATH [--index N]` reads the WIM header and its XML resource directly, so it
lists the images of a `.wim` (or a split `.swm` part) on any OS without DISM. An image file whose XML
resource is compressed (resource flag 0x04) is refused with an error rather than guessed at; ESD
support is unverified. `--index N` prints only image N and stops reading right after it; an index
the file does not hold exits non-zero with a message on stderr naming it, and an index below 1 is a
usage error (exit 2).

**Windows + Linux** - one command, a native backend on each (never a subprocess, never text scraping):

| Command                       | Shows                            | Windows                       | Linux                 |
|-------------------------------|----------------------------------|-------------------------------|-----------------------|
| `pwshpy get_service`          | services                         | win32service                  | systemd (D-Bus)       |
| `pwshpy get_win_event LOG`    | event-log / journal entries      | win32evtlog                   | journald              |
| `pwshpy get_scheduled_task`   | scheduled tasks                  | Task Scheduler                | systemd timers        |
| `pwshpy get_local_user`       | local user accounts              | win32net                      | `pwd`                 |
| `pwshpy get_local_group`      | local groups                     | win32net                      | `grp`                 |
| `pwshpy get_acl PATH`         | ACL entries                      | win32security                 | POSIX ACL xattr       |
| `pwshpy get_current_identity` | the account this process runs as | process token (win32security) | effective uid + `pwd` |

`get_current_identity` (`whoami /user`) reads the process token, never `USERNAME`/`USERDOMAIN`: inside a
SYSTEM scheduled task those name the computer account (`DOMAIN\HOST$`) while the token is `S-1-5-18`.
Match on `sid` (a SID on Windows, the effective uid as text on POSIX); `account`, `name` and `domain`
are display only. `integrity_level` is the token's mandatory label (`Untrusted` to `System`, or
`Protected`) on Windows and `null` on POSIX, where `is_system` and `elevated` both mean euid 0.

`get_scheduled_task [--folder PATH] [--strict]` skips what the caller cannot read and logs a
warning naming it: on Windows a folder whose tasks or subfolders it may not list, or a protected or
corrupt task; on Linux a refused unit-file listing or a timer unit systemd will not load or read. So a
non-elevated caller still lists everything it can read. `--strict` turns each such skip into an
error: the command exits 1 with one `NativeCallError` line on stderr naming the folder, task or unit.
Use it when "not listed" has to mean "does not exist", for example to check that a task was really
deleted. The listing streams, so the records before the unreadable item are already written: with
`--jsonl` they stay valid lines, with `--json` a failure on the first item leaves stdout empty and a
later one leaves an unterminated array (see [Portable record commands](#portable-record-commands)).
Timers systemd reports as not-found or masked, aliases and templates are no tasks in either mode.

`get_service [--strict]` follows the same contract. On Linux it skips, with a warning naming it, a
refused unit-file listing (then no stopped, unloaded service is listed) or a service unit systemd
will not load or whose load state it will not read; `--strict` exits 1 with one `NativeCallError`
line on stderr naming the unit instead, with the same `--jsonl` / `--json` output as above. On
Windows the listing drops no service, so `--strict` changes nothing there: a service whose
configuration the caller may not query is still listed, with `start_type` null. Services systemd
reports as not-found or masked (and stopped), aliases, templates and stale unit files are no services
in either mode, and a single field that cannot be read keeps its default in both.

**Windows only** - no honest Linux analog: `pwshpy get_item_property KEY` / `pwshpy registry_keys KEY`,
`pwshpy get_cim_instance CLASS`, `pwshpy get_hotfix`, `pwshpy get_windows_update`.

`pwshpy get_windows_update` searches Windows Update (read-only: never downloads or installs).
`--criteria` takes a WUA search string (default `IsInstalled=0 and Type='Software'`, the software
updates not yet installed); `--timeout SECONDS` (default 300, at most 2147483) bounds the search,
which runs in a child process that is killed when the time is up. A search that times out prints
one `timed out` line on stderr and exits **110** (ETIMEDOUT), so a script can tell it from a
failure (exit 1). Off Windows it exits 1 with `PlatformUnsupportedError`.

```bash
# streaming and piping stay lazy end to end
pwshpy get_process --jsonl | head -20
pwshpy get_net_tcp_connection --json | jq '.[] | select(.status == "LISTEN")'
pwshpy test_connection example.com -p 443 --timeout 3 --jsonl
pwshpy get_service --jsonl | jq 'select(.status == "Running")'   # same line on Windows and Linux
```

## Portable mutating verbs

These change host state and dispatch to the native backend per OS (need admin on Windows, root or
polkit on Linux). The library methods (`ps.start_service(...)`) mirror these names exactly.

| Group           | Commands                                                                                                                                                                  | Windows            | Linux                   |
|-----------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------|-------------------------|
| Services        | `start_service` `stop_service` `restart_service` `set_service`                                                                                                            | win32service       | systemd                 |
| Scheduled tasks | `register_scheduled_task` `unregister_scheduled_task` `enable_scheduled_task` `disable_scheduled_task` `start_scheduled_task` `stop_scheduled_task`                       | Task Scheduler     | systemd timers          |
| ACLs            | `add_acl_ace` `remove_acl_ace` `set_owner`                                                                                                                                | win32security      | POSIX ACL xattr + chown |
| Credentials     | `save_credential` `load_credential` `delete_credential`                                                                                                                   | Credential Manager | Secret Service keyring  |
| Local accounts  | `new_local_user` `remove_local_user` `enable_local_user` `disable_local_user` `new_local_group` `remove_local_group` `add_local_group_member` `remove_local_group_member` | win32net           | shadow-utils            |

`stop_service NAME` waits until the service is Stopped (up to 30 s). `--no-wait` issues the stop and
returns at once with the state right after it, typically `StopPending`, for a service that takes
long to stop (Windows Update's `wuauserv`). A wait that hits its deadline exits 110 (ETIMEDOUT) with
an `OperationTimeoutError` line on stderr - the stop was issued and the service is still on its
way - while a refused stop exits 1 with a `NativeCallError` line. `start_service` and
`restart_service` time out the same way.

On Linux, `add_acl_ace` takes a `user:NAME` / `group:NAME` trustee and the low 3 bits of `RIGHTS` as
`rwx` (POSIX ACLs are allow-only, so `--deny` is rejected); `save_credential` needs an unlocked
desktop keyring; the local-account verbs drive shadow-utils (`useradd` / `usermod` / `groupadd` /
`gpasswd` / ...) through a shell-free argv, the one documented subprocess exception (no stable native
account API), and a POSIX group has no description so `new_local_group`'s is ignored.

**Windows only** (no honest Linux analog): the registry verbs (`set_item_property`,
`remove_item_property`, `new_registry_key`, `remove_registry_key`) and `clear_event_log`. See
[COMMANDS.md](../COMMANDS.md) for the full cmdlet-by-cmdlet table.

### `register_scheduled_task`: principal, run level and triggers

`pwshpy register_scheduled_task TASK_PATH --program P [--arguments A] [--description D]` plus the
options below. With no trigger option the task runs only on demand (`start_scheduled_task`); with
several, it fires on any of them.

| Option              | Meaning                                                                                                        |
|---------------------|----------------------------------------------------------------------------------------------------------------|
| `--run-as ID`       | a SID, a decimal uid (Linux), or a `WellKnownSid` name in any case (`local_system`); account names are refused |
| `--run-level LEVEL` | `limited` (default) or `highest` (the elevated token; on Linux root only)                                      |
| `--logon-type TYPE` | `interactivetoken`, `serviceaccount` or `s4u` (Windows only); by default it follows `--run-as`                 |
| `--at TIME`         | run once at `TIME` (repeatable)                                                                                |
| `--daily TIME`      | run daily at the time of day of `TIME` (repeatable); Windows starts on that date, Linux runs from today        |
| `--every-days N`    | day interval for every `--daily` trigger (default 1; above 1 is Windows only)                                  |
| `--at-boot`         | run at boot                                                                                                    |
| `--boot-delay S`    | seconds after boot (with `--at-boot`)                                                                          |
| `--at-logon`        | run at logon (Windows only)                                                                                    |
| `--logon-user SID`  | whose logon triggers the task (with `--at-logon`; default any user)                                            |

`TIME` is ISO 8601 with a UTC offset: `2026-11-01T03:00:00+01:00`, or a trailing `Z` for UTC. A
time without an offset is refused, because the same text would fire at a different instant on a
machine in another zone. Without `--run-as` the task runs as the interactive caller on Windows and
as root on Linux.

```bash
# Windows: SYSTEM, elevated, nightly at 03:00 +01:00 and five minutes after each boot
pwshpy register_scheduled_task '\Maintenance\NightlyBackup' --program 'C:\Tools\backup.exe' \
    --arguments /full --run-as local_system --run-level highest \
    --daily 2026-11-01T03:00:00+01:00 --at-boot --boot-delay 300

# Linux: uid 1000, nightly at 02:00 UTC (a systemd .service plus an enabled, started .timer)
pwshpy register_scheduled_task nightly-backup --program /usr/local/bin/backup \
    --arguments=--full --run-as 1000 --daily 2026-11-01T02:00:00Z
```

Exit codes: a malformed option exits 2 with a usage error before anything is registered - an
account name for `--run-as`, an unparseable or offset-less time, a negative `--boot-delay`, or a
modifier without its trigger (`--every-days` without `--daily`, `--boot-delay` without `--at-boot`,
`--logon-user` without `--at-logon`). A refusal from the OS backend exits 1 with one
`ErrorName: message` line on stderr and no traceback. For example, on Linux a SID for `--run-as`,
any `--logon-type`, `--run-level highest` with a non-root uid, a logon trigger, `--every-days` above
1, or boot and calendar triggers in one task; on Windows a uid, a group SID such as
`--run-as administrators`, or a logon type that does not fit `--run-as`. The full per-OS mapping and the fields the task reads back are in
[Library & Pipeline](library-usage.md#scheduled-tasks-principal-run-level-and-triggers).

## Power-tool commands

```bash
pwshpy is-elevated -q && pwshpy clear_event_log Application   # elevation gate
pwshpy exec -- git status --short                             # typed external run
pwshpy exec --wait-tree -- setup.exe /quiet                  # ...and wait for its whole process tree
pwshpy download_file https://host/big.iso big.iso            # streamed download
pwshpy get_content huge.log | head                           # streamed file read
pwshpy save_credential prod-db svc                           # hidden prompt for the secret
pwshpy run "Get-Date"                                         # .NET (needs [full])
pwshpy cmdlet Get-Service -p Name=sshd                       # one cmdlet, safely-bound params
pwshpy get_ad_user -p Filter=*                               # .NET module shortcut (Get-ADUser); JSON only
pwshpy pack tool.py -o dist/tool.ps1                         # ship a script as one self-extracting .ps1
pwshpy pack tool.py -o dist/tool.sh                          # ...or a POSIX .sh (any /bin/sh, not only bash)
pwshpy unpack dist/tool.ps1 -o src/                          # get the sources back out (reads .ps1 or .sh)
```

Exit code 110 (ETIMEDOUT) means an `OperationTimeoutError` on every OS: `exec --timeout N` exits 110,
not 1, when the child outlives `N` seconds, as a service wait that hits its deadline does.

`pack` embeds the entry plus every local module it imports into a self-extracting runner that
unpacks itself, installs `uv` if the target machine has none, runs the script and returns its exit
code. Options: `-o/--out`, `--format auto|ps1|sh` (default `auto`: pick from the `-o` suffix - `.sh`
gives a POSIX-shell runner, anything else a `.ps1`), `--include PATH` (repeatable, for files no
import reveals), `--with PKG` (repeatable extra dependency), `--root DIR`, `--force`. Third-party
dependencies come from the entry's PEP 723 block or `--with`; they are never inferred from import
names. The `.ps1` runs on Windows PowerShell 5.1 / pwsh 7; the `.sh` runs under any POSIX `/bin/sh`
(dash, busybox ash, macOS `sh`, bash). Both pack and unpack are pure Python, so a `.sh` can be built
on Windows and a `.ps1` on Linux, and `unpack` sniffs the format on either OS. `unpack` restores the
packed sources (`-o DIR`, `--force`) so a pack can be edited and packed again. See
[Power Tools](power-tools.md) for the artefact's own switches (`-PwshPyInfo` / `--pwshpy-info`,
`-PwshPyClean` / `--pwshpy-clean`, `-PwshPyNoInstallUv` / `--pwshpy-no-install-uv`,
`-PwshPyElevate` / `--pwshpy-elevate`, `-PwshPyUv PATH` / `--pwshpy-uv PATH`). Run elevated (an
administrator token, SYSTEM or root) a runner only executes a `uv` that no account other than the
running one, SYSTEM, Administrators, LOCAL SERVICE, NETWORK SERVICE and TrustedInstaller can modify,
in its own directory or any directory above (on POSIX: root alone), skipping any other with a
`pwshpy: skipping uv at` line on stderr; an untrusted `-PwshPyUv` / `--pwshpy-uv` path is refused with exit 127, and a
relative one is a usage error (exit 64).

Exit code 77 (`EX_NOPERM`) means an elevation was refused or declined: `pwshpy --elevate`, a `.sh`
runner's `--pwshpy-elevate` and a `.ps1` runner's `-PwshPyElevate` all exit 77 and write a line
starting `pwshpy: elevation declined` to stderr, without running the command or the packed script.

The `.NET` commands need the `[full]` extra (else a clear `FeatureUnavailableError`). `run` executes
an arbitrary script; `cmdlet NAME -p KEY=VALUE` runs one cmdlet with safely-bound params; and the
module shortcuts `get_ad_user` / `get_ad_group` / `get_ad_computer` / `get_mailbox` / `get_az_vm` /
`get_az_resource_group` are fixed-cmdlet `cmdlet`s (same `-p` params, `--streams` for all six streams)
for the AD / Exchange / Azure long tail. Anything else is `pwshpy cmdlet Verb-Noun -p KEY=VALUE`.

## Exit codes

The `pwshpy` command and the packed runners share one set of exit codes, so a caller can tell a
refusal, a timeout and a missing tool from an ordinary failure without parsing stderr. Each error
also writes one `ErrorName: message` line (or a `pwshpy: ...` line from a runner) to stderr.

| Code | Meaning                                                                                                                                                                                                                                                                                    | Raised by                                                                        |
|------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------|
| 0    | success                                                                                                                                                                                                                                                                                    | every command                                                                    |
| 1    | a failure: a `PwshPyError` (`NativeCallError`, `PlatformUnsupportedError`, `ConfigurationError`, ...), including a missing `sudo` behind `pwshpy --elevate`                                                                                                                                | every command                                                                    |
| 2    | a usage error: an unknown option, a value out of range (`--index 0`), an offset-less `--at` time                                                                                                                                                                                           | every command (click)                                                            |
| 22   | a value the command accepted but the call refused (`ValueError`), e.g. `get_windows_update --timeout nan`                                                                                                                                                                                  | every command                                                                    |
| 64   | a relative `-PwshPyUv` / `--pwshpy-uv` path                                                                                                                                                                                                                                                | packed runners                                                                   |
| 70   | the runner failed before it ran the script: the elevated `.ps1` child (relayed with a `pwshpy: the elevated child failed before running the script` line), or an elevated run that cannot return to the caller's working directory (`pwshpy pack: cannot return to the working directory`) | packed runners                                                                   |
| 77   | an elevation was refused or declined (`ElevationDeclinedError`), with a `pwshpy: elevation declined` line on stderr                                                                                                                                                                        | `pwshpy --elevate`, `-PwshPyElevate`, `--pwshpy-elevate`                         |
| 110  | a timeout (`OperationTimeoutError`, ETIMEDOUT): `exec --timeout`, a service start, stop or restart that did not settle, `get_windows_update --timeout`                                                                                                                                     | `exec`, `start_service`, `stop_service`, `restart_service`, `get_windows_update` |
| 127  | a missing or untrusted tool: no `uv` and `-PwshPyNoInstallUv` / `--pwshpy-no-install-uv`, no `uv` an elevated run can trust, a refused `-PwshPyUv` pin, or no `sudo` for `-PwshPyElevate` / `--pwshpy-elevate` on POSIX                                                                    | packed runners                                                                   |

Any other exception that reaches the CLI keeps `lib_cli_exit_tools`' errno mapping
(`FileNotFoundError` 2, `PermissionError` 13 on POSIX and 5 on Windows). A runner otherwise exits
with the packed script's own code, so a script that itself exits 70, 77 or 127 gives the same number:
the runner's stderr line is what tells them apart. `pwshpy is-elevated` exits 0 when elevated and 1
when not.

## Configuration commands

```bash
pwshpy config                         # show current merged configuration
pwshpy config --format json           # show as JSON
pwshpy config --section lib_log_rich  # show a specific section
pwshpy config --profile production    # use a named profile

pwshpy config-deploy --target app     # system-wide config
pwshpy config-deploy --target user    # ~/.config/{slug}/config.toml
pwshpy config-generate-examples --destination ./examples

pwshpy --set lib_log_rich.console_level=DEBUG config   # repeatable runtime override
pwshpy --env-file ./environments/production.env config # explicit .env
```

See [CONFIG.md](../CONFIG.md) for the layered configuration system (precedence, profiles,
customization).
