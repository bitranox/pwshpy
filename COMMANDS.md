# pwshpy Command Reference - porting PowerShell to pwshpy

A translation table for rewriting existing `.ps1` scripts. Find the PowerShell cmdlet you use,
read across to its pwshpy equivalent. Two target columns, kept **separate**:

- **Python (API)** - `from pwshpy import ps` then call the method. This is the full surface.
- **CLI** - the `pwshpy` command, for shell scripts / one-liners. A subset of the API (read + mutating
  native verbs). Cells marked *library only* have no CLI - use the Python API.

Each section notes portability: **portable** (Linux/macOS/Windows), **Windows** (win32/wmi), or
**.NET** (needs the `[full]` extra + the .NET 10 runtime / PowerShell 7.6).

native read commands return typed records through the lazy pipeline (`.where` / `.select` /
`.sort_by` / `.take` / `.first` / `.to_list`); the CLI shares `--json` / `--jsonl` / a human table.

---

## Processes & system  *(portable)*

| PowerShell                           | pwshpy - Python (API)                                                    | pwshpy - CLI                                            |
|--------------------------------------|--------------------------------------------------------------------------|---------------------------------------------------------|
| `Get-Process`                        | `ps.get_process()`                                                       | `pwshpy get_process`                                    |
| `Get-Uptime`                         | `ps.get_uptime()`                                                        | `pwshpy get_uptime`                                     |
| `Get-ChildItem Env:` / `$env:X`      | `ps.environment()`                                                       | `pwshpy env`                                            |
| `Get-ComputerInfo`                   | `ps.get_computer_info()`                                                 | `pwshpy get_computer_info`                              |
| `Get-Hotfix`                         | `ps.get_hotfix()`  *(Windows)*                                           | `pwshpy get_hotfix`                                     |
| `Stop-Process -Id N`                 | `ps.stop_process(N, force=False)`  *(mutating)*                          | `pwshpy stop_process N [-f]`                            |
| `Wait-Process -Id N`                 | `ps.wait_process(N, timeout=None)`                                       | `pwshpy wait_process N`                                 |
| `Restart-Computer` / `Stop-Computer` | `ps.restart_computer()` / `ps.stop_computer()`  *(Windows, destructive)* | `pwshpy restart_computer --yes` / `stop_computer --yes` |

Filtering: in Python use `.where(predicate)` (or a plain `for`/`if`); on the CLI pipe `--jsonl` to `jq`.
`Get-EventLog` (the classic API) is covered by `ps.get_win_event` (Get-WinEvent). `restart_computer` /
`stop_computer` are library-only by design - too destructive for a casual CLI reboot.

## Networking  *(portable)*

| PowerShell                                   | pwshpy - Python (API)                            | pwshpy - CLI                                    |
|----------------------------------------------|--------------------------------------------------|-------------------------------------------------|
| `Get-NetTCPConnection`                       | `ps.get_net_tcp_connection()`                    | `pwshpy get_net_tcp_connection`                 |
| `Resolve-DnsName -Name X`                    | `ps.resolve_dns_name("X", timeout=None)`         | `pwshpy resolve_dns_name X --timeout T`         |
| `Test-NetConnection -ComputerName H -Port P` | `ps.test_connection("H", port=443, timeout=5.0)` | `pwshpy test_connection H --port P --timeout T` |
| `Get-NetAdapter`                             | `ps.get_net_adapter()`                           | `pwshpy get_net_adapter`                        |
| `Get-NetIPAddress`                           | `ps.get_net_ip_address()`                        | `pwshpy get_net_ip_address`                     |
| `Get-NetUDPEndpoint`                         | `ps.get_net_udp_endpoint()`                      | `pwshpy get_net_udp_endpoint`                   |

## Storage  *(portable)*

| PowerShell                   | pwshpy - Python (API) | pwshpy - CLI        |
|------------------------------|-----------------------|---------------------|
| `Get-Volume` / `Get-PSDrive` | `ps.get_volume()`     | `pwshpy get_volume` |

## Filesystem  *(portable)*

| PowerShell                            | pwshpy - Python (API)                                   | pwshpy - CLI                      |
|---------------------------------------|---------------------------------------------------------|-----------------------------------|
| `Get-ChildItem -Path P [-Recurse]`    | `ps.get_child_item("P", recurse=False)`                 | `pwshpy get_child_item P [-r]`    |
| `Get-Item -Path P`                    | `ps.get_item("P")`                                      | `pwshpy get_item P`               |
| `Get-Content -Path P -Raw`            | `ps.get_content("P", encoding="utf-8")`  (whole file)   | *library only*                    |
| `Get-Content -Path P`                 | `ps.get_content_lines("P")`  (streamed, memory-bounded) | `pwshpy get_content P`  (streams) |
| `Test-Path -Path P`                   | `ps.test_path("P")`  -> `bool`                          | `pwshpy test_path P`  (exit 0/1)  |
| `New-Item -Path P -ItemType File/Dir` | `ps.new_item("P", item_type="file")`                    | `pwshpy new_item P --type file`   |
| `Copy-Item S D [-Recurse]`            | `ps.copy_item("S", "D", recurse=False)`                 | `pwshpy copy_item S D [-r]`       |
| `Move-Item S D`                       | `ps.move_item("S", "D")`                                | `pwshpy move_item S D`            |
| `Remove-Item P [-Recurse]`            | `ps.remove_item("P", recurse=False)`                    | `pwshpy remove_item P [-r]`       |

Returns typed `FileSystemItem` records (path/name/is_directory/size/modified); backed by
`pathlib`/`shutil`, so it works on every OS.

## Registry  *(Windows)*

| PowerShell                                          | pwshpy - Python (API)                                         | pwshpy - CLI                                   |
|-----------------------------------------------------|---------------------------------------------------------------|------------------------------------------------|
| `Get-ItemProperty -Path K`                          | `ps.get_item_property("K")`                                   | `pwshpy get_item_property K`                   |
| `Get-ChildItem -Path K`                             | `ps.registry_keys("K")`                                       | `pwshpy registry_keys K`                       |
| `Set-ItemProperty -Path K -Name N -Value V -Type T` | `ps.set_item_property("K", "N", V, RegistryValueType.REG_SZ)` | `pwshpy set_item_property K N V --type REG_SZ` |
| `Remove-ItemProperty -Path K -Name N`               | `ps.remove_item_property("K", "N")`                           | `pwshpy remove_item_property K N`              |
| `New-Item -Path K`  (a key)                         | `ps.new_registry_key("K")`                                    | `pwshpy new_registry_key K`                    |
| `Remove-Item -Path K -Recurse`                      | `ps.remove_registry_key("K", recursive=True)`                 | `pwshpy remove_registry_key K --recursive`     |

## Services  *(portable: read + mutation)*

The whole subsystem is **portable**: win32service on Windows, systemd over D-Bus on Linux (built
in - the D-Bus lib jeepney is a base Linux dependency). On Linux `name` is the unit name
(`"sshd.service"`) and `service_type` is `None`. `Start`/`Stop`/`Restart`/`Set-Service` map to
`StartUnit`/`StopUnit`/`RestartUnit` and `EnableUnitFiles`/`DisableUnitFiles`; each waits for the
unit to settle. Mutation needs admin (Windows) / polkit or root (Linux).

| PowerShell                                   | pwshpy - Python (API)                             | pwshpy - CLI                     |
|----------------------------------------------|---------------------------------------------------|----------------------------------|
| `Get-Service`                                | `ps.get_service()`  *(portable)*                  | `pwshpy get_service`             |
| `Start-Service -Name N`                      | `ps.start_service("N", timeout=30.0)`             | `pwshpy start_service N`         |
| `Stop-Service -Name N`                       | `ps.stop_service("N", timeout=30.0)`              | `pwshpy stop_service N`          |
| `Restart-Service -Name N`                    | `ps.restart_service("N", timeout=30.0)`           | `pwshpy restart_service N`       |
| `Set-Service -Name N -StartupType Automatic` | `ps.set_service("N", ServiceStartType.AUTOMATIC)` | `pwshpy set_service N Automatic` |

## Event log  *(read: portable; clear: Windows)*

`get_win_event` is **portable**: win32evtlog on Windows, **journald** on Linux. It streams
newest-first (memory-bounded). `L` is a Windows channel (`"System"`) or, on Linux, a systemd unit
(`"sshd"`, or `""`/`"System"` for the whole journal); reading the system journal needs root or the
`systemd-journal` group. Clearing the log is Windows-only.

| PowerShell                                    | pwshpy - Python (API)                       | pwshpy - CLI                          |
|-----------------------------------------------|---------------------------------------------|---------------------------------------|
| `Get-WinEvent -LogName L`                     | `ps.get_win_event("L")`  *(portable)*       | `pwshpy get_win_event L`              |
| `Clear-EventLog -LogName L` / `wevtutil cl L` | `ps.clear_event_log("L", backup_path=None)` | `pwshpy clear_event_log L --backup P` |

## CIM / WMI  *(Windows)*

| PowerShell                                             | pwshpy - Python (API)                                         | pwshpy - CLI                                         |
|--------------------------------------------------------|---------------------------------------------------------------|------------------------------------------------------|
| `Get-CimInstance -ClassName C -Filter F -Namespace NS` | `ps.get_cim_instance("C", where="F", namespace="root/cimv2")` | `pwshpy get_cim_instance C --where F --namespace NS` |

## Scheduled tasks  *(portable: read + mutation)*

Portable: the Task Scheduler on Windows, systemd timers over D-Bus on Linux (built in, via the
base Linux dep jeepney). On Linux `get_scheduled_task` lists the `.timer` units (`task_name` is
the unit base name, `task_path` is `/`); `register` creates an on-demand oneshot `.service`
(matching the Windows on-demand shape) that `run`/`stop` start/stop; `enable`/`disable` arm/disarm
a task's `.timer` and raise a clear error for an on-demand task (systemd cannot disable a static
oneshot). Mutation needs admin (Windows) / root (Linux, system units).

| PowerShell                                                                                    | pwshpy - Python (API)                                                            | pwshpy - CLI                                                  |
|-----------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------|---------------------------------------------------------------|
| `Get-ScheduledTask`                                                                           | `ps.get_scheduled_task(folder_path="\\")`                                        | `pwshpy get_scheduled_task`                                   |
| `Register-ScheduledTask -TaskName T -Action (New-ScheduledTaskAction -Execute P -Argument A)` | `ps.register_scheduled_task("\\T", program="P", arguments="A", description="D")` | `pwshpy register_scheduled_task \T --program P --arguments A` |
| `Unregister-ScheduledTask -TaskName T`                                                        | `ps.unregister_scheduled_task("\\T")`                                            | `pwshpy unregister_scheduled_task \T`                         |
| `Enable-ScheduledTask -TaskName T`                                                            | `ps.enable_scheduled_task("\\T")`                                                | `pwshpy enable_scheduled_task \T`                             |
| `Disable-ScheduledTask -TaskName T`                                                           | `ps.disable_scheduled_task("\\T")`                                               | `pwshpy disable_scheduled_task \T`                            |
| `Start-ScheduledTask -TaskName T`                                                             | `ps.start_scheduled_task("\\T")`                                                 | `pwshpy start_scheduled_task \T`                              |
| `Stop-ScheduledTask -TaskName T`                                                              | `ps.stop_scheduled_task("\\T")`                                                  | `pwshpy stop_scheduled_task \T`                               |

## Local accounts  *(portable: read + mutation)*

Portable: win32net on Windows, stdlib `pwd` / `grp` (read) and shadow-utils (`useradd` / `usermod` /
`groupadd` / `gpasswd`, mutation) on Linux. The identity field (`sid`) carries a Windows SID or the
POSIX uid/gid as a string, so scripts match on the stable id, not a localized name. On Linux the
mutating verbs need root; a POSIX group has no description (so `new_local_group`'s is ignored), and
`full_name` maps to the single GECOS comment. Account mutation is the one place pwshpy drives external
commands (shell-free argv) rather than a native API - there is no stable native account API.

| PowerShell                                                      | pwshpy - Python (API)                                                                   | pwshpy - CLI                                                       |
|-----------------------------------------------------------------|-----------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| `Get-LocalUser`                                                 | `ps.get_local_user()`  *(portable)*                                                     | `pwshpy get_local_user`                                            |
| `Get-LocalGroup`                                                | `ps.get_local_group()`  *(portable)*                                                    | `pwshpy get_local_group`                                           |
| `New-LocalUser -Name N -Password .. -FullName F -Description D` | `ps.new_local_user("N", password="..", full_name="F", description="D", disabled=False)` | `pwshpy new_local_user N --password .. --full-name F [--disabled]` |
| `Remove-LocalUser -Name N`                                      | `ps.remove_local_user("N")`                                                             | `pwshpy remove_local_user N`                                       |
| `Enable-LocalUser -Name N`                                      | `ps.enable_local_user("N")`                                                             | `pwshpy enable_local_user N`                                       |
| `Disable-LocalUser -Name N`                                     | `ps.disable_local_user("N")`                                                            | `pwshpy disable_local_user N`                                      |
| `New-LocalGroup -Name G -Description D`                         | `ps.new_local_group("G", description="D")`                                              | `pwshpy new_local_group G --description D`                         |
| `Remove-LocalGroup -Name G`                                     | `ps.remove_local_group("G")`                                                            | `pwshpy remove_local_group G`                                      |
| `Add-LocalGroupMember -Group G -Member M`                       | `ps.add_local_group_member("G", "M")`                                                   | `pwshpy add_local_group_member G M`                                |
| `Remove-LocalGroupMember -Group G -Member M`                    | `ps.remove_local_group_member("G", "M")`                                                | `pwshpy remove_local_group_member G M`                             |

Principals are identified by **SID** (locale-independent); the localized name is a display field.

## ACLs / permissions  *(portable: read + mutation)*

Portable: win32security DACLs on Windows, POSIX ACLs on Linux (read + write the kernel ACL xattr
directly, plus `chown` for the owner - no `setfacl` subprocess). On Linux each entry carries a
`kind` (owner / user / owning-group / group / mask / other) and a `permissions` `rwx` string;
`trustee` is `user:NAME` / `group:NAME` (bare = user), `rights` uses the low 3 bits as `rwx`, and
POSIX ACLs are allow-only (a `--deny` ACE is rejected). Editing an ACL needs to own the file;
`set_owner` to another user needs root.

| PowerShell                           | pwshpy - Python (API)                                             | pwshpy - CLI                                   |
|--------------------------------------|-------------------------------------------------------------------|------------------------------------------------|
| `Get-Acl -Path P`                    | `ps.get_acl("P")`                                                 | `pwshpy get_acl P`                             |
| `Set-Acl`  (add an allow/deny ACE)   | `ps.add_acl_ace("P", trustee, rights, access_type=AceType.ALLOW)` | `pwshpy add_acl_ace P TRUSTEE RIGHTS [--deny]` |
| `Set-Acl`  (remove a trustee's ACEs) | `ps.remove_acl_ace("P", trustee, access_type=None)`               | `pwshpy remove_acl_ace P TRUSTEE`              |
| `Set-Acl`  (set the owner)           | `ps.set_owner("P", owner)`                                        | `pwshpy set_owner P OWNER`                     |

`trustee` / `owner` are a SID string or a resolvable name; `rights` is a Win32 access mask (int).

## Credentials  *(portable vault + prompt)*

The vault is portable: Windows Credential Manager on Windows, the desktop keyring (freedesktop
Secret Service, via gnome-keyring / kwallet) on Linux. The Linux side needs a running, unlocked
keyring; a locked or absent keyring gives a clear error rather than silently dropping the secret.
The `get_credential` prompt is portable everywhere.

| PowerShell                             | pwshpy - Python (API)                                              | pwshpy - CLI                                         |
|----------------------------------------|--------------------------------------------------------------------|------------------------------------------------------|
| `Get-Credential`                       | `ps.get_credential(username=None, prompt="Password: ", target="")` | *library only*                                       |
| `cmdkey /add` / `New-StoredCredential` | `ps.save_credential(target, username, secret)`                     | `pwshpy save_credential TARGET USER` (hidden prompt) |
| *(read a stored credential)*           | `ps.load_credential(target)`  -> `Credential \| None`              | `pwshpy load_credential TARGET` (secret masked)      |
| `cmdkey /delete`                       | `ps.delete_credential(target)`                                     | `pwshpy delete_credential TARGET`                    |

The secret is a `SecretStr`: masked in repr / JSON / logs; read it with `.secret.get_secret_value()`.

## Elevation  *(portable)*

All three are portable: the admin check is the Windows token / POSIX euid; the relaunch is
Windows UAC or a POSIX `sudo` re-exec (argv + cwd forwarded, no-op when already elevated).

| PowerShell                                                          | pwshpy - Python (API)                                                         | pwshpy - CLI                    |
|---------------------------------------------------------------------|-------------------------------------------------------------------------------|---------------------------------|
| `#Requires -RunAsAdministrator`                                     | `ps.require_elevation()`  (raises `ElevationRequiredError`)                   | *(raise / abort)*               |
| `([Security.Principal.WindowsPrincipal]..).IsInRole(Administrator)` | `ps.is_elevated()`  -> `bool` (portable)                                      | `pwshpy is-elevated [-q]`       |
| `Start-Process -Verb RunAs` / `sudo`                                | `ps.elevate(argv=None, executable=None, cwd=None, wait=True)`  *(UAC / sudo)* | `pwshpy --elevate <subcommand>` |

## Files & output  *(portable)*

| PowerShell                                        | pwshpy - Python (API)                                                     | pwshpy - CLI                           |
|---------------------------------------------------|---------------------------------------------------------------------------|----------------------------------------|
| `Out-File -Encoding utf8` / `Set-Content`         | `ps.write_text(path, text, encoding="utf-8", newline="\n", bom=False)`    | `pwshpy write_text PATH` (reads stdin) |
| `.. \| ConvertTo-Json \| Out-File` / `Export-Csv` | `ps.write_records(path, records, jsonl=True)`  (streams record-by-record) | *library only*                         |

Always UTF-8, no BOM, LF by default - the same bytes on every OS (no `Out-File` UTF-16/BOM surprise).

## Running an external program  *(portable)*

| PowerShell                                 | pwshpy - Python (API)                                              | pwshpy - CLI               |
|--------------------------------------------|--------------------------------------------------------------------|----------------------------|
| `& prog arg1 arg2` / `Start-Process -Wait` | `ps.exec(argv, cwd=None, timeout=None, env=None, input_text=None)` | `pwshpy exec -- prog args` |

`argv` is a LIST (no shell, no quoting hell); `.exit_code` is always set, `.stderr` is data,
`.check()` raises on nonzero.

## Shipping a script to a machine without Python  *(portable)*

| PowerShell                                     | pwshpy - Python (API)                              | pwshpy - CLI                   |
|------------------------------------------------|----------------------------------------------------|--------------------------------|
| *(no equivalent - hand-rolled base64 + `iex`)* | `ps.pack_script(entry, dest, with_packages=[...])` | `pwshpy pack ENTRY -o OUT.ps1` |
| *(no equivalent)*                              | `ps.unpack_script(source, dest)`                   | `pwshpy unpack OUT.ps1 -o DIR` |

The output is one `.ps1` carrying the entry and every local module it imports. It unpacks itself
into a per-user cache, installs `uv` if the machine has none, runs the script, and exits with the
script's own exit code - on Windows PowerShell 5.1 and pwsh 7 alike. Dependencies come from the
entry's PEP 723 block or `--with`, so the target machine needs neither Python nor a venv.

## Web requests  *(portable)*

| PowerShell                            | pwshpy - Python (API)                                             | pwshpy - CLI                                              |
|---------------------------------------|-------------------------------------------------------------------|-----------------------------------------------------------|
| `Invoke-WebRequest -Uri U`            | `ps.invoke_web_request("U", method="GET", headers=..., body=...)` | `pwshpy invoke_web_request U [-X M] [-H 'K: V'] [--json]` |
| `Invoke-RestMethod -Uri U -Body B`    | `ps.invoke_rest_method("U", method="GET", json_body=B)`  -> JSON  | `pwshpy invoke_rest_method U [-X M] [--body-json J]`      |
| `Invoke-WebRequest -Uri U -OutFile F` | `ps.download_file("U", "F", chunk_size=65536)`  (streamed)        | `pwshpy download_file U F`                                |

`invoke_web_request` returns a typed `WebResponse` (url/status_code/text/headers); a 4xx/5xx is
returned, not raised. `invoke_rest_method` sends/receives JSON. `download_file` **streams** a big
download straight to disk in fixed-size chunks (memory-bounded - never buffers the whole body),
raising on a 4xx/5xx and leaving no partial file. Built on stdlib `urllib` + `orjson` (no extra
dependency); the URL scheme is restricted to http/https.

## Running PowerShell itself  *(.NET - needs `[full]`)*

| PowerShell                           | pwshpy - Python (API)                                            | pwshpy - CLI                            |
|--------------------------------------|------------------------------------------------------------------|-----------------------------------------|
| `Invoke-Expression` / `& { script }` | `ps.run(script, timeout=None)`  -> marshaled objects             | `pwshpy run SCRIPT` (JSON out)          |
| `<Any-Cmdlet> -Param V`              | `ps.cmdlet("Any-Cmdlet", Param=V, timeout=None)`  (safe binding) | `pwshpy cmdlet NAME -p K=V [--streams]` |
| `Get-Command X`                      | `ps.get_command("X")`  -> typed `CommandInfo`                    | `pwshpy get_command X`                  |

`ps.cmdlet` binds every parameter as data (never string-interpolated) and returns a
`PSInvocationResult` with all six streams (`.output` / `.errors` / `.warnings` / `.verbose` /
`.debug` / `.information`).

## Module cmdlets: ActiveDirectory / Exchange / Azure  *(.NET - needs `[full]` + the module)*

AD/Exchange/Azure cmdlets have no native binding (they live in their PowerShell modules). They need
the `[full]` extra **and** the respective module installed; without `[full]` a .NET call raises
`FeatureUnavailableError` (the same guard as `ps.run`).

**These modules are huge - the wrappers below are NOT the coverage.** ActiveDirectory ships ~147
cmdlets, Exchange **800+**, and Az **4,000+** across ~80 sub-modules. pwshpy does **not** (and could
not sensibly) wrap them all - they change per module version and most take dozens of parameters. The
**complete** surface of every module is already reachable, with the same safe parameter binding,
through the universal escape hatch:

```python
from pwshpy import ps

ps.cmdlet("Set-ADUser", Identity="jdoe", Enabled=True)  # ANY cmdlet, any module - safe binding
ps.run("Get-ADUser -Filter 'Department -eq \"IT\"' | Select Name")  # full-fidelity script
```

The wrappers below are **illustrative, typed shortcuts** for a few of the most-reached-for cmdlets
(autocomplete + a named entry point, in the library and the CLI). Each is one line over `ps.cmdlet`
and returns the full `PSInvocationResult` (all six streams). On the CLI they take the same
`-p KEY=VALUE` params as `cmdlet` (`--streams` for all six streams). Adding more is trivial - open an
issue for the ones you use.

| PowerShell (illustrative) | pwshpy - Python (API)                | pwshpy - CLI                          | Module          |
|---------------------------|--------------------------------------|---------------------------------------|-----------------|
| `Get-ADUser -Filter *`    | `ps.get_ad_user(Filter="*")`         | `pwshpy get_ad_user -p Filter=*`      | ActiveDirectory |
| `Get-ADGroup`             | `ps.get_ad_group(**params)`          | `pwshpy get_ad_group -p KEY=VALUE`    | ActiveDirectory |
| `Get-ADComputer`          | `ps.get_ad_computer(**params)`       | `pwshpy get_ad_computer -p KEY=VALUE` | ActiveDirectory |
| `Get-Mailbox`             | `ps.get_mailbox(**params)`           | `pwshpy get_mailbox -p KEY=VALUE`     | Exchange        |
| `Get-AzVM`                | `ps.get_az_vm(**params)`             | `pwshpy get_az_vm -p KEY=VALUE`       | Az.Compute      |
| `Get-AzResourceGroup`     | `ps.get_az_resource_group(**params)` | `pwshpy get_az_resource_group -p K=V` | Az.Resources    |

Anything not in that short list - `Set-ADUser`, `New-Mailbox`, `Start-AzVM`, all ~5,000 others - is
`ps.cmdlet("Verb-Noun", **params)` (CLI `pwshpy cmdlet Verb-Noun -p KEY=VALUE`), no wrapper required.

---

## Not (yet) supported natively - demand-gated

These have **no purpose-built typed native command** in pwshpy yet:

- **Networking:** `Get-NetIPConfiguration` (a composite - use `get_net_adapter` +
  `get_net_ip_address`), `Get-NetRoute`, `Get-DnsClientServerAddress`.
- **Module-only cmdlets:** ActiveDirectory (`Get-ADUser`, ...), Exchange, Azure (`Az.*`), and any
  other module's cmdlets - pwshpy ships typed **.NET convenience wrappers** for the common ones
  (see below); they delegate to the real module cmdlet and need the `[full]` extra plus that module
  installed.

**You are not blocked, though.** With the `[full]` extra, **every one of these runs at full
fidelity through .NET** - `ps.cmdlet("Get-NetAdapter")` or `ps.run("Get-ADUser -Filter *")` - you
just get a generic `PSObjectRecord` back instead of a purpose-built typed record.

Want a **native, typed** native command for one of these (fast, no .NET, first-class records)?
**[Open an issue](https://github.com/bitranox/pwshpy/issues)** and say which cmdlet and which fields
you need - that is how the native surface grows.

---

## pwshpy-only - no PowerShell equivalent

A few things have no cmdlet to translate *from* - they are pwshpy's own:

- **The lazy pipeline** - `.where` / `.select` / `.sort_by` / `.take` / `.first` / `.to_list` over
  typed records, streaming and memory-bounded end to end (see the README).
- **`ps.exec(...).check()`** - a shell-free runner whose `exit_code` is always set (unlike
  `$LASTEXITCODE` in a pipeline).
- **`ps.elevate()`** returning the elevated child's exit code, and `ps.is_elevated()` working
  cross-platform.
- **Errors as one exception tree** (`PwshPyError`) - no terminating-vs-non-terminating trap.
