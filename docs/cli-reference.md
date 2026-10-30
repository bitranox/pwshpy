# CLI Reference

[Back to the README](../README.md) | **Prev:** [Power Tools](power-tools.md) | **Next:** [Backends & Platforms](backends-and-platforms.md)

The `pwshpy` command mirrors the library: same names, same surface. For a full cmdlet-by-cmdlet
translation table (grouped by theme, Python API and CLI in separate columns), see
[COMMANDS.md](../COMMANDS.md).

## Portable record commands

Every native record command shares `--json` (a streamed array), `--jsonl` (one record per line,
lazy end to end), and a default human table; most take `--limit N`.

On an error the exit code is non-zero and the message goes to stderr, after any log lines the
command already wrote (queued log lines are drained first, so each stays on its own line).
`--json` writes nothing
until the source has produced its first record, so a command that fails up front (a missing
path, a refused native call) leaves stdout empty rather than a bare `[`. An error after records
were already streamed leaves a partial, unterminated array: that is the price of streaming, so
check the exit code before trusting the output, or use `--jsonl`, whose lines stay individually
parseable.

`--json` and `--jsonl` write UTF-8 bytes with LF line ends on every OS, whatever the console
code page, `PYTHONIOENCODING` or `PYTHONUTF8` say (JSON is UTF-8 by spec), so a consumer reading
the pipe decodes it as UTF-8 even from a cp1252 Windows console. The JSON that `run`, `cmdlet`,
`get_command`, `invoke_rest_method` and `config --format json` print is written the same way. So is the text that `exec`
(the program's stdout and stderr), `invoke_web_request` (the response body), `get_content` (the
file's lines) and `download_file` (its `downloaded URL -> DEST` line) write when piped or redirected:
a character the console code page cannot hold no longer fails the command after the work was done.
Those bytes are the text's own: `exec` output and `get_content` lines have LF line ends (both are
read with universal newlines), a web body keeps its line ends, and ANSI sequences are not stripped.
At a terminal they print as before.

A record field holding a byte that is not valid UTF-8 (a POSIX environment variable, file, process
or account name) is written with U+FFFD for each such byte, so `--json` / `--jsonl` stay valid UTF-8
JSON and the stream goes on, exit 0; one line per affected record on stderr names the record type,
its position and the field, never the value (`warning: EnvVar record 7, field value: not valid
UTF-8; each undecodable byte written as U+FFFD`; `fields name, value` when several are). The human
table writes such a byte as U+FFFD too, with the same warning line, so `pwshpy env > file` is valid
UTF-8 as well. The JSON
that `get_known_folder --json`, `run`, `cmdlet`, its wrappers (`get_ad_user`, `get_ad_group`,
`get_ad_computer`, `get_mailbox`, `get_az_vm`, `get_az_resource_group`) and `invoke_rest_method`
print follows the same rule, with one `warning: output field <path>: ...` line
(`warning: output: ...` for a bare string, such as `get_known_folder`'s path), and so does
`config --format json` (`warning: output field lib_log_rich.service: ...` for such a `--set` value
or environment override). A mapping key
that collides with another once repaired is kept as `<key>~2` (`~3`, ...), and the field paths in a
warning show each non-ASCII key character as `?`. The original bytes cannot be recovered from the
JSON; for a single undecodable byte this is what pwsh 7 shows.

Every other line the CLI writes to a piped or redirected stdout or stderr is UTF-8 with LF line ends
too, on every OS: the human table, the status lines (`wrote`, `created`, `copied`, `moved`,
`removed`, `registered`, ...), `config`, and error messages. A value the
console code page cannot hold (`pwshpy env | more` with a CJK value on German Windows) used to fail
the command with exit 22 (87 on Windows) after the work was done. A reader that decodes the pipe in
a legacy code page sees non-ASCII characters as mojibake rather than as an error. At a terminal
nothing changes: the console's own encoding is used.

At a terminal the human table fits the terminal's width and may shorten long cells. Piped or
redirected (`pwshpy env | grep PATH`, `> out.txt`), it is as wide as its content instead, up to
8192 columns, so no cell is cut: every value is whole, and a value without line breaks stays on one
line. A table wider than 8192 columns folds its long values onto further lines rather than dropping
characters, breaking a value that holds spaces at a space. The table is for reading: use `--jsonl`
when a script needs a value's exact bytes. An explicit `COLUMNS` in
the environment still sets the width in both cases. Piped or redirected, the table's frame is ASCII
(`+`, `-`, `|`) on every OS, so a reader that decodes the pipe in a legacy code page never garbles
it; at a terminal the table keeps its box-drawing frame. A nested record (`get_scheduled_task`'s
`settings`) reads as `key=value, key=value` of its set fields, and a list of records or mappings
(`get_windows_update`'s `category_details`) as one such group per item, joined by `; `; any other
list or mapping prints as it is.

**Everywhere** - one binding on Windows, macOS and Linux:

| Command                         | Shows                         | Binding                                |
|---------------------------------|-------------------------------|----------------------------------------|
| `pwshpy get_process`            | running processes             | psutil                                 |
| `pwshpy get_net_tcp_connection` | open TCP connections          | psutil                                 |
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

`get_net_tcp_connection` lists TCP sockets only, IPv4 and IPv6, like `Get-NetTCPConnection`; UDP
sockets are `pwshpy get_net_udp_endpoint` (`Get-NetUDPEndpoint`).

`get_windows_image PATH [--index N]` reads the WIM header and its XML resource directly, so it
lists the images of a `.wim` (or a split `.swm` part) on any OS without DISM. An image file whose XML
resource is compressed (resource flag 0x04) is refused with an error rather than guessed at. The
two `install.esd` files measured from Microsoft's catalog (2026-10-23, build 26100.4349 en-us, x64
and arm64) store their XML resource uncompressed, so they are read like a `.wim`; the CLI was run on
the x64 one. Other ESD files are unverified. An image whose XML has no `WINDOWS` element (image 1,
`Windows Setup Media`, in the x64 file) is listed with `build` 0, `sp_build` 0, `architecture`
`Unknown` and an empty `version`. That is the one difference from `Get-WindowsImage` measured on
that file: it shows the same image with architecture 0 (x86) and version `0.0.0.0`, a default
rather than a number read from the image, so pwshpy does not report a guessed x86. The other nine
images matched `Get-WindowsImage` on index, name, build, SP build, architecture and version.
`--index N` prints only image N and stops reading right after it; an index
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
Timers systemd reports as not-found or masked (and stopped), aliases and templates are no tasks in
either mode; a running one is still listed.

`get_scheduled_task --task-name NAME [--folder PATH]` lists nothing: it reads the one task `NAME` in
`--folder` (default root; on Windows a trailing backslash, the form `task_path` carries, is
accepted). A folder elsewhere that the caller cannot read never affects the answer. A task that
does not exist prints nothing (`--json`: `[]`) and exits 0, with `--strict` too. Every other
failure is skipped with a warning, or with `--strict` exits 1 with `NativeCallError`: on Windows any
error opening the folder or the task other than not-found (HRESULT 0x80070002) - access denied on
the folder, a name the scheduler refuses (0x8007007B), a scheduler error - and a found task that
cannot be read; on Linux a refused `LoadUnit` or `LoadState` read, or a unit file whose ownership
marker cannot be read. `NAME` is the task's own name: an empty one, or one with a folder part,
exits 1 with `ConfigurationError`. Windows matches the name without regard to case and echoes the
spelling you passed in `task_name` and `task_path`. On Linux `--folder` is ignored and the answer is
the task's `.timer`, else an on-demand task pwshpy registered: a `.service` with no `.timer` counts
only when its unit file carries pwshpy's `X-PwshPy=` marker in `[Unit]`, so a system daemon such as
`ssh` or `cron` is no task. Such an on-demand task is found by name although the listing, timers
only, leaves it out. A masked unit counts as absent unless it is running, and an alias as absent,
as in the listing. The suffix is optional, and `job.service` names the task `job`, so it answers
with the timer record when `job` has a timer.

`get_service [--strict]` follows the same contract. On Linux it skips, with a warning naming it, a
refused unit-file listing (then no stopped, unloaded service is listed) or a service unit systemd
will not load or whose load state it will not read; `--strict` exits 1 with one `NativeCallError`
line on stderr naming the unit instead, with the same `--jsonl` / `--json` output as above. On
Windows the listing drops no service, so `--strict` changes nothing there: a service whose
configuration the caller may not query is still listed, with `start_type` null. Services systemd
reports as not-found or masked (and stopped), aliases, templates and stale unit files are no services
in either mode, and a single field that cannot be read keeps its default in both.

**Known folders** - `pwshpy get_known_folder FOLDER [--json]` prints one well-known folder, the path
pwsh's `[Environment]::GetFolderPath` prints on the same machine: the Windows shell's known folder
(`SHGetKnownFolderPath`, so a redirected Desktop is found), the XDG user directories on Linux, the
home's `Desktop`, `Documents` and `Library/Application Support` on macOS. `FOLDER` is the .NET
`SpecialFolder` name, in any case: `Desktop`, `MyDocuments`, `LocalApplicationData`,
`ApplicationData`, `CommonApplicationData` or `Programs`. It prints the bare path (with `--json` a
JSON string). On macOS the path is standardized as Foundation does it: a leading `/private`,
`/private/var/automount` or `/var/automount` component is dropped when the rest still exists, so a
home under `/private/var/folders/...` prints as `/var/folders/...`
([Backends & Platforms](backends-and-platforms.md#known-folders), the macOS bullet). A folder that
is missing or not readable, where pwsh prints an empty string, exits 1
with a line on stderr naming it; `Programs` on Linux or macOS, which .NET maps to no folder, exits 1
with `PlatformUnsupportedError`. The per-OS rules are in
[Backends & Platforms](backends-and-platforms.md#known-folders).

**Windows only** - no honest Linux analog: `pwshpy get_item_property KEY` / `pwshpy registry_keys KEY`,
`pwshpy get_cim_instance CLASS`, `pwshpy get_hotfix`, `pwshpy get_windows_update`,
`pwshpy get_shortcut PATH`.

`pwshpy get_shortcut PATH` shows a shortcut (`.lnk`) as Windows reads it: `target` with environment
references expanded (`%SystemRoot%\notepad.exe` reads back as `C:\Windows\notepad.exe`),
`arguments` as the one command-line string the shortcut stores, and `working_directory`, `icon`,
`icon_index` and `description` raw; an unset field is empty (`icon_index` 0). A relative `PATH`
resolves against the working directory. A missing file, an empty file or one that is no shortcut
exits 1 with `NativeCallError`.

`pwshpy get_windows_update` searches Windows Update (read-only: never downloads or installs).
`--criteria` takes a WUA search string (default `IsInstalled=0 and Type='Software'`, the software
updates not yet installed); `--timeout SECONDS` (default 300, at most 2147483) bounds the search,
which runs in a child process that is killed when the time is up. A search that times out prints
one `timed out` line on stderr and exits **110** (ETIMEDOUT), so a script can tell it from a
failure (exit 1). Off Windows it exits 1 with `PlatformUnsupportedError`. Each record's
`category_details` lists its categories as `{"category_id", "type", "name"}` objects; filter on the
lower-case `category_id` GUID, not the localized name (the classification GUIDs are in
[Power Tools](power-tools.md#selecting-updates-by-category)).

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
`restart_service` time out the same way. On Windows a service still in the opposite transition is
waited out first, within the same timeout: `start_service` on a service in `StopPending`, and
`stop_service` (with or without `--no-wait`) on one in `StartPending`, since the service control
manager cannot take the control until it settles. If it does not settle in time the command exits 110
without having sent the control.

On Linux, `add_acl_ace` takes a `user:NAME` / `group:NAME` trustee and the low 3 bits of `RIGHTS` as
`rwx` (POSIX ACLs are allow-only, so `--deny` is rejected); `save_credential` needs an unlocked
desktop keyring; the local-account verbs drive shadow-utils (`useradd` / `usermod` / `groupadd` /
`gpasswd` / ...) through a shell-free argv, the one documented subprocess exception (no stable native
account API), and a POSIX group has no description so `new_local_group`'s is ignored.

`get_acl` and the three ACL verbs run on Windows and Linux only: on macOS they raise
`PlatformUnsupportedError` before touching the path, since the Linux backend reads an extended
attribute macOS does not offer.

**Windows only** (no honest Linux analog): the registry verbs (`set_item_property`,
`remove_item_property`, `new_registry_key`, `remove_registry_key`), `clear_event_log` and
`new_shortcut`. See [COMMANDS.md](../COMMANDS.md) for the full cmdlet-by-cmdlet table.

`pwshpy new_shortcut PATH --target TARGET [--working-directory D] [--icon I] [--icon-index N]
[--description T] [--run-as-administrator] [-f] [-- ARGS...]` writes a shortcut (`.lnk`). The target's arguments go after
`--`, one word per argument, as with `exec`; the shortcut stores them as one command line quoted
the way `subprocess.list2cmdline` quotes it:

```bash
pwshpy new_shortcut 'C:\Users\alice\Desktop\Tool.lnk' --target 'C:\Tools\uv.exe' \
    --description 'Run the tool' -- tool run --exclude-newer "7 days" mytool
```

`TARGET` must be an absolute Windows path or start with an environment reference
(`%SystemRoot%\notepad.exe`). A value Windows would silently cut, wrap or rename is refused before
anything is written (exit 1, `ConfigurationError`): a target or `.lnk` path over 259 characters (a
relative `PATH` is made absolute first), arguments joining to over 32767, a description, working
directory or icon over 260, NUL, CR or LF in any value, or a `PATH` not ending in `.lnk`. A target
whose environment reference expands past 259 characters passes those checks but is refused by
Windows (exit 1, `NativeCallError`, HRESULT 0x80004005), and no file is written. An existing `PATH` exits 1 with
`NativeCallError` unless `--force` / `-f` is given, and a missing directory is not created. On
success it prints `created shortcut PATH -> TARGET` (the target as Windows stored it, expanded), or
with `--json` / `--jsonl` the stored record, the same one `get_shortcut` shows. Off Windows it exits
1 with `PlatformUnsupportedError`. `--run-as-administrator` ticks the shortcut's "Run as
administrator" box: opening it starts the target elevated after one UAC prompt, and `get_shortcut`
shows the record's `run_as_administrator` as `True` (`true` with `--json` / `--jsonl`).

### `register_scheduled_task`: principal, run level, triggers and settings

`pwshpy register_scheduled_task TASK_PATH --program P [--arguments A] [--description D]` plus the
options below. With no trigger option the task runs only on demand (`start_scheduled_task`); with
several, it fires on any of them.

| Option                                                               | Meaning                                                                                                                                                      |
|----------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `--run-as ID`                                                        | a SID, a decimal uid (Linux), or a `WellKnownSid` name in any case (`local_system`); account names are refused                                               |
| `--run-level LEVEL`                                                  | `limited` (default) or `highest` (the elevated token; on Linux root only)                                                                                    |
| `--logon-type TYPE`                                                  | `interactivetoken`, `serviceaccount` or `s4u` (Windows only); by default it follows `--run-as`                                                               |
| `--at TIME`                                                          | run once at `TIME` (repeatable)                                                                                                                              |
| `--daily TIME`                                                       | run daily at the time of day of `TIME` (repeatable); Windows starts on that date, Linux runs from today                                                      |
| `--every-days N`                                                     | day interval for every `--daily` trigger (default 1; above 1 is Windows only)                                                                                |
| `--at-boot`                                                          | run at boot                                                                                                                                                  |
| `--boot-delay S`                                                     | seconds after boot (with `--at-boot`)                                                                                                                        |
| `--at-logon`                                                         | run at logon (Windows only)                                                                                                                                  |
| `--logon-user SID`                                                   | whose logon triggers the task (with `--at-logon`; default any user)                                                                                          |
| `--allow-start-if-on-batteries` / `--disallow-start-if-on-batteries` | whether a run may start on battery power                                                                                                                     |
| `--stop-if-going-on-batteries` / `--dont-stop-if-going-on-batteries` | whether a running task is stopped when the machine switches to battery; Linux refuses the first form                                                         |
| `--execution-time-limit LIMIT`                                       | the longest a run may last: whole seconds (`259200`) or `HH:MM:SS` (`72:00:00`, hours may pass 23); `0` means no limit                                       |
| `--multiple-instances POLICY`                                        | what a start does while a run is active: `parallel`, `queue`, `ignorenew` or `stopexisting` (any case); Linux takes only `ignorenew`, and not with `--daily` |
| `--allow-hard-terminate` / `--disallow-hard-terminate`               | whether a run that ignores a stop request may be killed                                                                                                      |
| `--allow-demand-start` / `--disallow-demand-start`                   | whether `start_scheduled_task` may start the task besides its triggers                                                                                       |

`TIME` is ISO 8601 with a UTC offset: `2026-11-01T03:00:00+01:00`, or a trailing `Z` for UTC. A
time without an offset is refused, because the same text would fire at a different instant on a
machine in another zone. Without `--run-as` the task runs as the interactive caller on Windows and
as root on Linux.

The six settings flags are the CLI form of `TaskSettings` (the names follow PowerShell's
`New-ScheduledTaskSettingsSet`). A flag you leave out writes nothing, so the OS default applies;
with none of them the task is registered exactly as without settings. `--execution-time-limit`
takes at most 2147483647 seconds (`596523:14:07`): Task Scheduler wraps a longer limit modulo
2^32 seconds, so it would silently mean something else. Windows stores the limit in its own form
(`72:00:00` is `PT72H`), and `get_scheduled_task --json` / `--jsonl` print it in ISO 8601 form
(`P3D`; `PT0S` for no limit). With `--disallow-demand-start` only the triggers start the task:
`start_scheduled_task` exits 1 with a `NativeCallError` line on both OSes (Task Scheduler's
0x80041328 SCHED_E_START_ON_DEMAND on Windows, systemd's "Operation refused" on Linux), and a task
with no trigger can then never run. On Linux `--disallow-hard-terminate` writes `SendSIGKILL=no`: a
program that ignores SIGTERM survives `stop_scheduled_task` and blocks every new start until it
exits.

```bash
# Windows: SYSTEM, elevated, nightly at 03:00 +01:00 and five minutes after each boot
pwshpy register_scheduled_task '\Maintenance\NightlyBackup' --program 'C:\Tools\backup.exe' \
    --arguments /full --run-as local_system --run-level highest \
    --daily 2026-11-01T03:00:00+01:00 --at-boot --boot-delay 300

# Linux: uid 1000, nightly at 02:00 UTC (a systemd .service plus an enabled, started .timer)
pwshpy register_scheduled_task nightly-backup --program /usr/local/bin/backup \
    --arguments=--full --run-as 1000 --daily 2026-11-01T02:00:00Z

# Windows: a SYSTEM job started on demand that may run on battery for up to 72 hours,
# ignores a second start while it runs, and may be killed if it ignores a stop
pwshpy register_scheduled_task '\Maintenance\Repair' --program 'C:\Tools\repair.exe' \
    --run-as local_system --run-level highest \
    --allow-start-if-on-batteries --dont-stop-if-going-on-batteries \
    --execution-time-limit 72:00:00 --multiple-instances ignorenew \
    --allow-hard-terminate --allow-demand-start
```

Exit codes: a malformed option exits 2 with a usage error before anything is registered - an
account name for `--run-as`, an unparseable or offset-less time, a negative `--boot-delay`, a
modifier without its trigger (`--every-days` without `--daily`, `--boot-delay` without `--at-boot`,
`--logon-user` without `--at-logon`), or an `--execution-time-limit` that is neither form or exceeds
2147483647 seconds. A refusal from the OS backend exits 1 with one
`ErrorName: message` line on stderr and no traceback. For example, on Linux a SID for `--run-as`,
any `--logon-type`, `--run-level highest` with a non-root uid, a logon trigger, `--every-days` above
1, boot and calendar triggers in one task, `--stop-if-going-on-batteries`, a `--multiple-instances`
other than `ignorenew`, or `ignorenew` together with `--daily` (each a `PlatformUnsupportedError`,
raised before any unit is written); on Windows a uid, a group SID such as
`--run-as administrators`, or a logon type that does not fit `--run-as`. The full per-OS mapping and the fields the task reads back are in
[Library & Pipeline](library-usage.md#scheduled-tasks-principal-run-level-and-triggers), and the
settings in [Task settings](library-usage.md#task-settings).

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

Exit code 110 (ETIMEDOUT) means an `OperationTimeoutError` on every OS: `exec --timeout N` exits
110, not 1, when the child outlives `N` seconds, as a service wait that hits its deadline does, and
so do `wait_process --timeout`, `resolve_dns_name --timeout`, `download_file`, `invoke_web_request`
and `invoke_rest_method` (a connect or a read that stalls past `--timeout`, default 30 s) and, on
Linux, the local-account commands (`new_local_user`, `remove_local_user`, `enable_local_user`,
`disable_local_user`, `new_local_group`, `remove_local_group`, `add_local_group_member` and
`remove_local_group_member`), whose shadow-utils commands share one 30 s limit: each command gets
what is left of it, and the one running when it passes is killed. When `new_local_user` has
already created the account and a later step (`chpasswd`, `usermod --lock`) fails or times out,
`--disabled` still locks the account within a fixed 5 s grace, and the error line names what was
done: created, password set or not, locked or not. The other timeouts exit 1: `run`, `cmdlet` and
the module shortcuts (`get_ad_user`, ...) with `--timeout` raise `PowerShellError`.

`invoke_web_request` and `invoke_rest_method` fail (exit 1, `NativeCallError`) on any HTTP status
urllib does not follow - a refused 3xx such as a redirect loop, 304, 4xx and 5xx - as
`Invoke-WebRequest` and `Invoke-RestMethod` do; the error line names the status and the first 200
characters of the body, on one line. `--skip-http-error-check` (pwsh's `-SkipHttpErrorCheck`)
prints such an answer instead, and `invoke_rest_method` then parses its body like any other; an
empty or whitespace-only body (a 304) prints `null`, where `Invoke-RestMethod` returns the body
text.
`download_file` has no such flag: it always fails on such a status. It removes `DEST` only when
it had opened that file itself (a transfer that breaks off, or a body that ends before its
`Content-Length`); a refused connect or an HTTP error leaves an existing file of that name
untouched. All three follow a redirect only to an `http` or `https` URL: one to any other scheme
fails before anything connects to it, with or without `--skip-http-error-check`. An answer that
is not HTTP, or a body cut short, fails with `NativeCallError` too.

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
relative one is a usage error (exit 64). Elevated, uv also runs on only the directories of the
caller's `PATH` that pass the same rule, each one left out named with a
`pwshpy: leaving <dir> off uv's PATH:` line, so a git dependency needs a `git` in such a directory
(a system-wide install); the packed script gets the caller's whole `PATH` back. A root `.sh` run of a
script with a git source also refuses that git (exit 127) when it is older than 2.32, which ignores
`GIT_CONFIG_GLOBAL` (see [Power Tools](power-tools.md)).

Exit code 77 (`EX_NOPERM`) means an elevation was refused or declined: `pwshpy --elevate`, a `.sh`
runner's `--pwshpy-elevate` and a `.ps1` runner's `-PwshPyElevate` all exit 77 and write a line
starting `pwshpy: elevation declined` to stderr, without running the command or the packed script.
On Linux and macOS `pwshpy --elevate` first asks `sudo -n` to run the program the relaunch runs
(pwshpy's Python); when that needs no password (a `NOPASSWD` sudoers rule), the command runs under
`sudo -n` without any prompt. Otherwise it asks for the password once (`sudo -v`), checks with
`sudo -n -v` that sudo cached a credential, and runs the command under `sudo -n`, so sudo never
prompts a second time; a sudo that caches nothing (sudoers `timestamp_timeout=0`) is declined with
77 too. Run the command under `sudo` yourself on such a system. The packed runners make the same
first check with the program their relaunch runs (`/bin/sh` for a `.sh`, the running `pwsh` for a
`.ps1`).

The `.NET` commands need the `[full]` extra (else a clear `FeatureUnavailableError`). `run` executes
an arbitrary script; `cmdlet NAME -p KEY=VALUE` runs one cmdlet with safely-bound params; and the
module shortcuts `get_ad_user` / `get_ad_group` / `get_ad_computer` / `get_mailbox` / `get_az_vm` /
`get_az_resource_group` are fixed-cmdlet `cmdlet`s (same `-p` params, `--streams` for all six streams)
for the AD / Exchange / Azure long tail. Anything else is `pwshpy cmdlet Verb-Noun -p KEY=VALUE`.

## Exit codes

The `pwshpy` command and the packed runners share one set of exit codes, so a caller can tell a
refusal, a timeout and a missing tool from an ordinary failure without parsing stderr. Each error
also writes one `ErrorName: message` line (or a `pwshpy: ...` line from a runner) to stderr.

| Code | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | Raised by                                                                                                                                                                                                                                                                                                                                                                   |
|------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 0    | success                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | every command                                                                                                                                                                                                                                                                                                                                                               |
| 1    | any other failure: a `PwshPyError` (`NativeCallError`, `PlatformUnsupportedError`, `ConfigurationError`, ...), including a missing `sudo` behind `pwshpy --elevate`                                                                                                                                                                                                                                                                                                                                                                            | every command                                                                                                                                                                                                                                                                                                                                                               |
| 2    | a usage error: an unknown option, a value out of range (`--index 0`), an offset-less `--at` time                                                                                                                                                                                                                                                                                                                                                                                                                                               | every command (click)                                                                                                                                                                                                                                                                                                                                                       |
| 13   | a refused native call (`NativeAccessDeniedError`, also a `PermissionError`; 5 on Windows), whatever the failed call reported (`EACCES`, `EPERM`, a pywin32 or COM `E_ACCESSDENIED`, a psutil `AccessDenied`, a systemd D-Bus `AccessDenied` or `InteractiveAuthorizationRequired`, a Secret Service D-Bus `AccessDenied`), e.g. `get_acl` on a file in a directory the caller cannot enter, or `stop_service` as a non-root user                                                                                                               | native commands; a shadow-utils refusal exits 1                                                                                                                                                                                                                                                                                                                             |
| 22   | a value the command accepted but the call refused (`ValueError`; 87 on Windows), e.g. `get_windows_update --timeout nan`                                                                                                                                                                                                                                                                                                                                                                                                                       | every command                                                                                                                                                                                                                                                                                                                                                               |
| 64   | a relative `-PwshPyUv` / `--pwshpy-uv` path                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | packed runners                                                                                                                                                                                                                                                                                                                                                              |
| 70   | the runner failed before it ran the script: the elevated `.ps1` child (relayed with a `pwshpy: the elevated child failed before running the script` line), an elevated run that cannot return to the caller's working directory (`pwshpy pack: cannot return to the working directory`), or a root `.sh` run that cannot read the account's home from the passwd database                                                                                                                                                                      | packed runners                                                                                                                                                                                                                                                                                                                                                              |
| 77   | an elevation was refused or declined (`ElevationDeclinedError`), with a `pwshpy: elevation declined` line on stderr; on POSIX also a sudo that caches no credential (`timestamp_timeout=0`)                                                                                                                                                                                                                                                                                                                                                    | `pwshpy --elevate`, `-PwshPyElevate`, `--pwshpy-elevate`                                                                                                                                                                                                                                                                                                                    |
| 110  | a timeout (`OperationTimeoutError`, ETIMEDOUT): `exec --timeout`, a service start, stop or restart that did not settle, `get_windows_update --timeout`, `wait_process --timeout`, `resolve_dns_name --timeout`, a web request or download whose connect or read stalls past `--timeout`, a Linux local-account command whose shadow-utils commands together outlast 30 s                                                                                                                                                                       | `exec`, `start_service`, `stop_service`, `restart_service`, `get_windows_update`, `wait_process`, `resolve_dns_name`, `invoke_web_request`, `invoke_rest_method`, `download_file`, `new_local_user`, `remove_local_user`, `enable_local_user`, `disable_local_user`, `new_local_group`, `remove_local_group`, `add_local_group_member`, `remove_local_group_member` (Linux) |
| 127  | a missing or untrusted tool or directory: no `uv` and `-PwshPyNoInstallUv` / `--pwshpy-no-install-uv`, no `uv` an elevated run can trust, a refused `-PwshPyUv` pin, a managed Python an elevated run cannot trust (`refusing the Python uv found at`), a pack cache an elevated run cannot trust (`refusing the cache directory`), a git older than 2.32 (or of no readable version) for an elevated `.ps1` or root `.sh` run of a script with a git source (`refusing git`), or no `sudo` for `-PwshPyElevate` / `--pwshpy-elevate` on POSIX | packed runners                                                                                                                                                                                                                                                                                                                                                              |

A refusal systemd sends over D-Bus (`org.freedesktop.DBus.Error.AccessDenied`, or
`InteractiveAuthorizationRequired`, which a non-root caller gets where polkit is installed) is a
`NativeAccessDeniedError` and exits 13, like any other refused native call; any other D-Bus error
reply from systemd is a plain `NativeCallError` naming that error and exits 1. The Secret Service
(credential vault) maps its D-Bus error replies the same way: `AccessDenied` exits 13, any other
error 1. A refusal pwshpy reads only from a message or an exit status is a plain `NativeCallError`
and exits 1: a non-zero exit of a shadow-utils command.

Any other exception that reaches the CLI keeps `lib_cli_exit_tools`' mapping, which reads the codes
an `OSError` carries before its type: its `winerror` on Windows, else its `errno` (a raw
`PermissionError` with `EPERM` exits 1, with `EACCES` 13), and only a code-less one exits by type
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
