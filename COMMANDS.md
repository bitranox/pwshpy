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

| PowerShell                                                          | pwshpy - Python (API)                                                      | pwshpy - CLI                                             |
|---------------------------------------------------------------------|----------------------------------------------------------------------------|----------------------------------------------------------|
| `Get-Process`                                                       | `ps.get_process()`                                                         | `pwshpy get_process`                                     |
| `Get-Uptime`                                                        | `ps.get_uptime()`                                                          | `pwshpy get_uptime`                                      |
| `Get-ChildItem Env:` / `$env:X`                                     | `ps.environment()`                                                         | `pwshpy env`                                             |
| `Get-ComputerInfo`                                                  | `ps.get_computer_info()`                                                   | `pwshpy get_computer_info`                               |
| `whoami /user`                                                      | `ps.get_current_identity()`                                                | `pwshpy get_current_identity`                            |
| `Get-Hotfix`                                                        | `ps.get_hotfix()`  *(Windows)*                                             | `pwshpy get_hotfix`                                      |
| `Get-WindowsImage -ImagePath P [-Index N]`                          | `ps.get_windows_image("P", index=None)`                                    | `pwshpy get_windows_image P [--index N]`                 |
| `IUpdateSearcher.Search(C)` / `Get-WindowsUpdate` (PSWindowsUpdate) | `ps.get_windows_update(criteria=C, timeout=300.0)`  *(Windows, read-only)* | `pwshpy get_windows_update [--criteria C] [--timeout S]` |
| `Stop-Process -Id N`                                                | `ps.stop_process(N, force=False)`  *(mutating)*                            | `pwshpy stop_process N [-f]`                             |
| `Wait-Process -Id N`                                                | `ps.wait_process(N, timeout=None)`                                         | `pwshpy wait_process N`                                  |
| `Restart-Computer` / `Stop-Computer`                                | `ps.restart_computer()` / `ps.stop_computer()`  *(Windows, destructive)*   | `pwshpy restart_computer --yes` / `stop_computer --yes`  |

Filtering: in Python use `.where(predicate)` (or a plain `for`/`if`); on the CLI pipe `--jsonl` to `jq`.
`Get-EventLog` (the classic API) is covered by `ps.get_win_event` (Get-WinEvent). The CLI's
`restart_computer` / `stop_computer` act only with `--yes`, so a mistyped command never reboots.
`get_current_identity` reads the process token (the SID, integrity level and elevation flag) on Windows
and the effective uid on POSIX, never `USERNAME`, which names the computer account inside a SYSTEM task.
`get_windows_image` with an index the file does not hold raises `NativeCallError` (the CLI exits
non-zero with a message on stderr); an index below 1 is refused at the
call (`ValueError`, CLI usage error).
`get_windows_update` searches Windows Update in a child process killed after `timeout` seconds
(`OperationTimeoutError`, CLI exit 110); it never downloads or installs, `criteria` goes to the
agent verbatim, and the result arrives whole on the first iteration (`.take(n)` does not shorten the
search). A script calling it needs an `if __name__ == "__main__":` guard, because the search child
re-imports it.
`get_process` keeps psutil's names, so two Windows pseudo-processes differ from `Get-Process`:
`MemCompression` (`Get-Process`: `Memory Compression`) and PID 0 `System Idle Process`
(`Get-Process`: `Idle`).

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
unit to settle, which on Linux means its job is done: a `Type=oneshot` unit without
`RemainAfterExit` returns `Stopped` once its start job finishes. On Windows a service already in
`StopPending` (`StartPending`) is not sent a second stop (start), which the SCM would refuse; the
verb waits for that pending transition, or with `wait=False` returns `StopPending`. Mutation needs
admin (Windows) / polkit or root (Linux).

A start, stop or restart that has not settled by `timeout` raises `OperationTimeoutError` (its
`.timeout` holds the limit; CLI exit 110): the control was issued and the service is still on its
way. A control the service or systemd refuses raises the plain `NativeCallError` (CLI exit 1).
`OperationTimeoutError` subclasses `NativeCallError`, so catch it first to tell the two apart.
`stop_service(name, wait=False)` (CLI `--no-wait`) issues the stop and returns the state right
after it, typically `StopPending`, without waiting - for a service that sits in Stop Pending
(Windows Update's `wuauserv`) where `Stop-Service` would never return.

| PowerShell                                       | pwshpy - Python (API)                             | pwshpy - CLI                      |
|--------------------------------------------------|---------------------------------------------------|-----------------------------------|
| `Get-Service`                                    | `ps.get_service()`  *(portable)*                  | `pwshpy get_service`              |
| *(fail on an unreadable service, never skip it)* | `ps.get_service(strict=True)`                     | `pwshpy get_service --strict`     |
| `Start-Service -Name N`                          | `ps.start_service("N", timeout=30.0)`             | `pwshpy start_service N`          |
| `Stop-Service -Name N`                           | `ps.stop_service("N", timeout=30.0)`              | `pwshpy stop_service N`           |
| `Stop-Service -Name N -NoWait`                   | `ps.stop_service("N", wait=False)`                | `pwshpy stop_service --no-wait N` |
| `Restart-Service -Name N`                        | `ps.restart_service("N", timeout=30.0)`           | `pwshpy restart_service N`        |
| `Set-Service -Name N -StartupType Automatic`     | `ps.set_service("N", ServiceStartType.AUTOMATIC)` | `pwshpy set_service N Automatic`  |

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
base Linux dep jeepney). On Linux `get_scheduled_task` lists the `.timer` units, disabled and
not-yet-started ones included (`task_name` is the unit base name, `task_path` is `/`); `register` writes a oneshot `.service` (the action, with
`User=` for a uid principal) and, when triggers are given, a `.timer` that starts it, so a task
without triggers runs only on demand; `run`/`stop` start/stop the service; `enable`/`disable`
arm/disarm a task's `.timer` and raise a clear error for an on-demand task (systemd cannot disable
a static oneshot). A principal is a SID on Windows and a decimal uid on Linux, never an account
name. Logon triggers, `every_days` above 1 and a logon type are Windows-only, and with no principal
a task runs as the interactive caller on Windows but as root on Linux; the full mapping is in
[Library & Pipeline](docs/library-usage.md#scheduled-tasks-principal-run-level-and-triggers).
Linux refuses the task settings systemd has no analog for (stopping a run on battery, any
multiple-instances policy but IgnoreNew, IgnoreNew with a daily trigger); see
[Task settings](docs/library-usage.md#task-settings).
Mutation needs admin (Windows) / root (Linux, system units). A listing skips, with a logged
WARNING, a folder, task or unit the caller cannot read; `strict=True` (CLI `--strict`) raises
`NativeCallError` naming it instead, for when "not listed" must mean "does not exist".

| PowerShell                                                                                              | pwshpy - Python (API)                                                                | pwshpy - CLI                                                      |
|---------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------|-------------------------------------------------------------------|
| `Get-ScheduledTask`                                                                                     | `ps.get_scheduled_task(folder_path="\\")`                                            | `pwshpy get_scheduled_task`                                       |
| *(fail on an unreadable task, never skip it)*                                                           | `ps.get_scheduled_task(strict=True)`                                                 | `pwshpy get_scheduled_task --strict`                              |
| `Register-ScheduledTask -TaskName T -Action (New-ScheduledTaskAction -Execute P -Argument A)`           | `ps.register_scheduled_task("\\T", program="P", arguments="A", description="D")`     | `pwshpy register_scheduled_task \T --program P --arguments A`     |
| `Register-ScheduledTask ... -Principal (New-ScheduledTaskPrincipal -UserId S-1-5-18 -RunLevel Highest)` | `principal=TaskPrincipal.system()`                                                   | `--run-as local_system --run-level highest`                       |
| `New-ScheduledTaskPrincipal -UserId SID -LogonType S4U`                                                 | `principal=TaskPrincipal(user_id="SID", logon_type=TaskLogonType.S4U)`               | `--run-as SID --logon-type s4u`                                   |
| `Register-ScheduledTask ... -Trigger (New-ScheduledTaskTrigger -Once -At T)`                            | `triggers=[OnceTrigger(at=T)]`                                                       | `--at T`                                                          |
| `New-ScheduledTaskTrigger -Daily -DaysInterval N -At T`                                                 | `triggers=[DailyTrigger(at=T, every_days=N)]`                                        | `--daily T --every-days N`                                        |
| `New-ScheduledTaskTrigger -AtStartup`                                                                   | `triggers=[BootTrigger(delay=timedelta(seconds=S))]`                                 | `--at-boot --boot-delay S`                                        |
| `New-ScheduledTaskTrigger -AtLogOn -User U`                                                             | `triggers=[LogonTrigger(user_id="SID")]`  *(Windows)*                                | `--at-logon --logon-user SID`                                     |
| `Register-ScheduledTask ... -Settings (New-ScheduledTaskSettingsSet ...)`                               | `settings=TaskSettings(...)` (unset fields keep the OS default)                      | the settings flags below                                          |
| `New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries` / `-DontStopIfGoingOnBatteries`                 | `TaskSettings(allow_start_if_on_batteries=True, stop_if_going_on_batteries=False)`   | `--allow-start-if-on-batteries --dont-stop-if-going-on-batteries` |
| `New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 72)`                             | `TaskSettings(execution_time_limit=timedelta(hours=72))` (`timedelta(0)` = no limit) | `--execution-time-limit 72:00:00` (or `259200`; `0` = no limit)   |
| `New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew`                                             | `TaskSettings(multiple_instances=TaskMultipleInstances.IGNORE_NEW)`                  | `--multiple-instances ignorenew`                                  |
| `New-ScheduledTaskSettingsSet -DisallowHardTerminate`                                                   | `TaskSettings(allow_hard_terminate=False)`                                           | `--disallow-hard-terminate`                                       |
| `New-ScheduledTaskSettingsSet -DisallowDemandStart`                                                     | `TaskSettings(allow_demand_start=False)`                                             | `--disallow-demand-start`                                         |
| `Get-ScheduledTaskInfo T` (`LastRunTime` / `LastTaskResult` / `NextRunTime`)                            | `task.last_run_time` / `task.last_task_result` / `task.next_run_time`                | `pwshpy get_scheduled_task --jsonl`                               |
| `Unregister-ScheduledTask -TaskName T`                                                                  | `ps.unregister_scheduled_task("\\T")`                                                | `pwshpy unregister_scheduled_task \T`                             |
| `Enable-ScheduledTask -TaskName T`                                                                      | `ps.enable_scheduled_task("\\T")`                                                    | `pwshpy enable_scheduled_task \T`                                 |
| `Disable-ScheduledTask -TaskName T`                                                                     | `ps.disable_scheduled_task("\\T")`                                                   | `pwshpy disable_scheduled_task \T`                                |
| `Start-ScheduledTask -TaskName T`                                                                       | `ps.start_scheduled_task("\\T")`                                                     | `pwshpy start_scheduled_task \T`                                  |
| `Stop-ScheduledTask -TaskName T`                                                                        | `ps.stop_scheduled_task("\\T")`                                                      | `pwshpy stop_scheduled_task \T`                                   |

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
Windows UAC or a POSIX `sudo` re-exec (argv + cwd forwarded, no-op when already elevated). A refused
prompt raises `ElevationDeclinedError` (a `NativeCallError` subclass); `pwshpy --elevate` and the
packed runners' `-PwshPyElevate` / `--pwshpy-elevate` exit 77 with a `pwshpy: elevation declined`
line on stderr. `ps.get_current_identity()` (`whoami /user`) reads who the process runs as from its
token.

| PowerShell                                                          | pwshpy - Python (API)                                                         | pwshpy - CLI                    |
|---------------------------------------------------------------------|-------------------------------------------------------------------------------|---------------------------------|
| `#Requires -RunAsAdministrator`                                     | `ps.require_elevation()`  (raises `ElevationRequiredError`)                   | *(raise / abort)*               |
| `([Security.Principal.WindowsPrincipal]..).IsInRole(Administrator)` | `ps.is_elevated()`  -> `bool` (portable)                                      | `pwshpy is-elevated [-q]`       |
| `Start-Process -Verb RunAs` / `sudo`                                | `ps.elevate(argv=None, executable=None, cwd=None, wait=True)`  *(UAC / sudo)* | `pwshpy --elevate <subcommand>` |

## Files & output  *(portable)*

| PowerShell                                        | pwshpy - Python (API)                                                     | pwshpy - CLI                                                                             |
|---------------------------------------------------|---------------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| `Out-File -Encoding utf8` / `Set-Content`         | `ps.write_text(path, text, encoding="utf-8", newline="\n", bom=False)`    | `pwshpy write_text PATH` (reads stdin, UTF-8 when piped; `--input-encoding` for another) |
| `.. \| ConvertTo-Json \| Out-File` / `Export-Csv` | `ps.write_records(path, records, jsonl=True)`  (streams record-by-record) | *library only*                                                                           |

Always UTF-8, no BOM, LF by default - the same bytes on every OS (no `Out-File` UTF-16/BOM surprise).

## Known folders & shortcuts  *(known folders: portable; shortcuts: Windows)*

| PowerShell                                                        | pwshpy - Python (API)                                                                        | pwshpy - CLI                                   |
|-------------------------------------------------------------------|----------------------------------------------------------------------------------------------|------------------------------------------------|
| `[Environment]::GetFolderPath('Desktop')`                         | `ps.get_known_folder(KnownFolder.DESKTOP)`  -> `Path` or `None`                              | `pwshpy get_known_folder Desktop [--json]`     |
| `$sc = $ws.CreateShortcut(P)`, set `TargetPath` ..., `$sc.Save()` | `ps.new_shortcut("P", target="T", arguments=["a", "b"], force=False)`  *(Windows, mutating)* | `pwshpy new_shortcut P --target T [-f] -- a b` |
| `$ws.CreateShortcut(P)`  (read its fields)                        | `ps.get_shortcut("P")`  -> `ShortcutInfo`  *(Windows)*                                       | `pwshpy get_shortcut P [--json \| --jsonl]`    |

`$ws` is `New-Object -ComObject WScript.Shell`.

`get_known_folder` answers what pwsh's `[Environment]::GetFolderPath` answers on the same machine.
The folder is a `KnownFolder` member or its value, the .NET `SpecialFolder` name: `Desktop`
(`DESKTOP`), `MyDocuments` (`DOCUMENTS`), `LocalApplicationData` (`LOCAL_APP_DATA`),
`ApplicationData` (`ROAMING_APP_DATA`), `CommonApplicationData` (`PROGRAM_DATA`) and `Programs`
(`PROGRAMS`). The library matches the value exactly, case included; the CLI takes it in any case.
On Windows the answer comes from the shell (`SHGetKnownFolderPath`), so a Desktop or Documents
folder redirected elsewhere (OneDrive, folder redirection) is returned where a `~/Desktop` guess
would miss it; Linux follows the XDG user directories and macOS the home's `Desktop`, `Documents`
and `Library/Application Support`. A folder that is missing or not readable is `None` (pwsh: `""`;
the CLI exits 1 with a line on stderr), and `Programs` off Windows raises `PlatformUnsupportedError`.
The full per-OS mapping is in [Backends & Platforms](docs/backends-and-platforms.md#known-folders).

`new_shortcut` writes the `.lnk` through `IShellLinkW`, so non-ASCII text survives in every field
and in the file name. `target` must be an absolute Windows path or start with an environment
reference (`%SystemRoot%\notepad.exe`); Windows reads it back expanded. `arguments` is an argv list,
stored as the one command line `subprocess.list2cmdline` quotes; a bare string is refused, never
split (the CLI takes the argv after `--`). A value Windows would silently cut, wrap or rename is
refused with `ConfigurationError` before anything is written: a target or `.lnk` path over 259
characters (the path made absolute first), arguments joining to over 32767, a description, working
directory or icon over 260, or NUL/CR/LF in any field. A target whose environment reference
*expands* past 259 characters passes those checks, but Windows refuses it with `NativeCallError`
(0x80004005) and writes nothing. An existing file is refused with `NativeCallError` unless
`force=True` (`-f`), and no directory is created. The call returns what Windows stored, as
`get_shortcut` reads it; the CLI prints one `created shortcut PATH -> TARGET` line, or the record
with `--json` / `--jsonl`. Off Windows both shortcut verbs raise `PlatformUnsupportedError`.

## Running an external program  *(portable)*

| PowerShell                                       | pwshpy - Python (API)                                              | pwshpy - CLI                           |
|--------------------------------------------------|--------------------------------------------------------------------|----------------------------------------|
| `& prog arg1 arg2`                               | `ps.exec(argv, cwd=None, timeout=None, env=None, input_text=None)` | `pwshpy exec -- prog args`             |
| `Start-Process -Wait` (waits for the whole tree) | `ps.exec(argv, wait_tree=True)`                                    | `pwshpy exec --wait-tree -- prog args` |

`argv` is a LIST (no shell, no quoting hell); `.exit_code` is always set, `.stderr` is data,
`.check()` raises `NativeCallError` on nonzero. A run that outlives `timeout` is killed and raises
`OperationTimeoutError` (CLI `--timeout`, exit 110); the output written so far is not returned. `wait_tree=True` also waits for every process the program started
(see [Power tools](docs/power-tools.md#psexec---run-a-program-without-the-quoting--lastexitcode-traps)).

## Shipping a script to a machine without Python  *(portable)*

| PowerShell                                     | pwshpy - Python (API)                                   | pwshpy - CLI                   |
|------------------------------------------------|---------------------------------------------------------|--------------------------------|
| *(no equivalent - hand-rolled base64 + `iex`)* | `ps.pack_script(entry, dest, options=PackOptions(...))` | `pwshpy pack ENTRY -o OUT.ps1` |
| *(no equivalent)*                              | `ps.unpack_script(source, dest)`                        | `pwshpy unpack OUT.ps1 -o DIR` |

The output is one file carrying the entry and every local module it imports. It unpacks itself into
a per-user cache, installs `uv` if the machine has none, runs the script, and exits with the
script's own exit code. Dependencies come from the entry's PEP 723 block or `--with`, so the target
machine needs neither Python nor a venv.

Pick the format for the target: a `.ps1` for Windows PowerShell 5.1 / pwsh 7, or a `.sh` for **any
POSIX `/bin/sh` (not only bash - dash, busybox ash, macOS `sh`, ...)**. The format auto-detects from
the `-o` suffix, or force it with `--format ps1|sh` (API: `PackOptions(format=RunnerFormat.SH)`).
Both pack and unpack are pure Python, so you can build a `.sh` on Windows and a `.ps1` on Linux, and
`unpack` reads either format on either OS. An elevated run (administrator, SYSTEM or root) only runs
a `uv` no other account can modify and only a uv-managed Python; `-PwshPyUv PATH` /
`--pwshpy-uv PATH` pins the `uv` to run. The runner's own exit codes (64, 70, 77, 127) are listed in
[CLI Reference](docs/cli-reference.md#exit-codes); the trust rule in
[Power Tools](docs/power-tools.md#ship-a-script-as-one-file---pspack_script).

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
  (see above); they delegate to the real module cmdlet and need the `[full]` extra plus that module
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
