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
pwshpy pack tool.py -o dist/tool.ps1                         # ship a script as one self-extracting file
pwshpy unpack dist/tool.ps1 -o src/                          # get the sources back out
```

`pack` embeds the entry plus every local module it imports into a `.ps1` that unpacks itself,
installs `uv` if the target machine has none, runs the script and returns its exit code. Options:
`-o/--out`, `--include PATH` (repeatable, for files no import reveals), `--with PKG` (repeatable
extra dependency), `--root DIR`, `--force`. Third-party dependencies come from the entry's PEP 723
block or `--with`; they are never inferred from import names. `unpack` restores the packed sources
(`-o DIR`, `--force`) so a pack can be edited and packed again. See
[Power Tools](power-tools.md) for the artefact's own switches (`-PwshPyInfo`, `-PwshPyClean`,
`-PwshPyNoInstallUv`, `-PwshPyElevate`).

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
