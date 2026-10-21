# CLI Reference

[Back to the README](../README.md) | **Prev:** [Power Tools](power-tools.md) | **Next:** [Backends & Platforms](backends-and-platforms.md)

The `pwshpy` command mirrors the library: same names, same surface. For a full cmdlet-by-cmdlet
translation table (grouped by theme, Python API and CLI in separate columns), see
[COMMANDS.md](../COMMANDS.md).

## Portable record commands

Every native record command shares `--json` (a streamed array), `--jsonl` (one record per line,
lazy end to end), and a default human table; most take `--limit N`.

**Everywhere** - one binding on Windows, macOS and Linux:

| Command                         | Shows                         | Binding       |
|---------------------------------|-------------------------------|---------------|
| `pwshpy get_process`            | running processes             | psutil        |
| `pwshpy get_net_tcp_connection` | open network connections      | psutil        |
| `pwshpy get_volume`             | mounted filesystems and usage | psutil        |
| `pwshpy get_uptime`             | boot time and elapsed uptime  | psutil        |
| `pwshpy resolve_dns_name NAME`  | resolved DNS addresses        | stdlib socket |
| `pwshpy test_connection HOST`   | TCP reachability probe        | stdlib socket |
| `pwshpy env`                    | environment variables         | os.environ    |
| `pwshpy get_net_adapter`        | network interfaces            | psutil        |
| `pwshpy get_computer_info`      | machine summary               | stdlib/psutil |

**Windows + Linux** - one command, a native backend on each (never a subprocess, never text scraping):

| Command                     | Shows                       | Windows        | Linux           |
|-----------------------------|-----------------------------|----------------|-----------------|
| `pwshpy get_service`        | services                    | win32service   | systemd (D-Bus) |
| `pwshpy get_win_event LOG`  | event-log / journal entries | win32evtlog    | journald        |
| `pwshpy get_scheduled_task` | scheduled tasks             | Task Scheduler | systemd timers  |
| `pwshpy get_local_user`     | local user accounts         | win32net       | `pwd`           |
| `pwshpy get_local_group`    | local groups                | win32net       | `grp`           |
| `pwshpy get_acl PATH`       | ACL entries                 | win32security  | POSIX ACL xattr |

**Windows only** - no honest Linux analog: `pwshpy get_item_property KEY` / `pwshpy registry_keys KEY`,
`pwshpy get_cim_instance CLASS`, `pwshpy get_hotfix`.

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
`-PwshPyElevate` / `--pwshpy-elevate`).

The `.NET` commands need the `[full]` extra (else a clear `FeatureUnavailableError`). `run` executes
an arbitrary script; `cmdlet NAME -p KEY=VALUE` runs one cmdlet with safely-bound params; and the
module shortcuts `get_ad_user` / `get_ad_group` / `get_ad_computer` / `get_mailbox` / `get_az_vm` /
`get_az_resource_group` are fixed-cmdlet `cmdlet`s (same `-p` params, `--streams` for all six streams)
for the AD / Exchange / Azure long tail. Anything else is `pwshpy cmdlet Verb-Noun -p KEY=VALUE`.

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
