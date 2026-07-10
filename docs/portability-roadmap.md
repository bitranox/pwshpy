# Portability Roadmap: making the Windows-only surface cross-OS

[Back to the README](../README.md) | see also [Backends & Platforms](backends-and-platforms.md)

**Goal:** one script that runs unchanged on Windows and Linux. A large part of pwshpy is already
portable (below); this roadmap tracks moving the Windows-only subsystems onto their *honest* Linux
mechanisms - systemd / D-Bus / journald / stdlib - never a shelled-out `systemctl | parse` fake,
because text-scraping a subprocess is the exact thing pwshpy exists to avoid.

## Ground rules (decided)

- **Native, not subprocess.** Talk to systemd/journald over D-Bus or their libraries, read accounts
  from `pwd`/`grp`. The one accepted exception is account *mutation* (`useradd` et al.), which has no
  stable native API - it goes through `ps.exec` (shell-free argv), documented as such.
- **D-Bus lib:** `jeepney` (pure-Python, zero external deps), imported Linux-only, exactly like
  `pywin32` is Windows-only. Add as `jeepney; sys_platform == "linux"`.
- **Platform dispatch** lives in composition/`build_ps`: pick the win32 adapter on Windows, the
  systemd/journald/posix adapter on Linux, else an adapter that raises `PlatformUnsupportedError`.
  Same pattern the psutil-portable vs win32-Windows split already uses.
- **Identity is locale-invariant on both.** Windows -> SID; Linux -> uid/gid. Never a localized name.
- **Lossy maps are documented, not hidden.** Where the Windows and Linux models differ, pick a
  canonical mapping and write down the edges (same discipline as `docs/locale-and-identity.md`).
- **Verification:** the pure mapping functions are `os_agnostic` unit tests; the live D-Bus / journald
  / sudo paths are `local_only` + Linux-only and must be run on a real Linux box (or CI's ubuntu lane)
  before they ship. No blind Linux code.

## Already portable today (the shipping core)

Processes, TCP connections, volumes, uptime, DNS resolve, TCP reachability, env vars, the whole
filesystem subsystem, `exec`, `write_text` / `write_text_stream` / `write_records`,
`get_content_lines`, `download_file`, `is_elevated`, `require_elevation`, `get_credential` (the
prompt), and `get_computer_info`. These are the same bytes on Windows, macOS, and Linux now.

## Build order

Ranked value / (1 - risk). Read sides need no root and verify in CI; mutating verbs need a root Linux
box (like the Windows mutating tests need throwaway VM 24000).

1. **Event log -> journald** (read) - **DONE** (`journald.py`, streaming, verified live on Linux).
2. **Local users/groups -> pwd/grp + shadow-utils** (read + mutating) - **DONE** (`posix_accounts.py`
   read; `posix_account_control.py` mutation via useradd/usermod/groupadd/gpasswd, verified live as root).
3. **Services -> systemd** (read + mutating) - **DONE** (`systemd_services.py`, D-Bus; read + start/stop/restart/enable/disable verified live on a root scratch unit).
4. **Scheduled tasks -> systemd timers** (read + mutating) - **DONE** (`systemd_timers.py`, D-Bus;
   read lists `.timer` units, register/run/unregister an on-demand oneshot service, enable/disable a
   `.timer`; verified live on root scratch units).
5. **`elevate()` relaunch -> sudo/pkexec** - **DONE** (sudo re-exec; verified on Linux).
6. **Credential store -> Secret Service** - **DONE** (`secret_service.py`, D-Bus, plain algorithm;
   save/load/delete verified live against gnome-keyring; locked/absent keyring gives a clear error).
7. **ACLs -> POSIX ACLs** (read + mutating) - **DONE** (`posix_acl.py`, kernel ACL xattr + chown;
   `AclEntry` redesigned with `kind`/`permissions`; read cross-checked against `getfacl`, mutate
   verified live).

---

## 1. Event log -> journald  `get_win_event`  [read: DONE]

- **Status:** implemented in `adapters/native/journald.py`, dispatched by platform in `build_ps`,
  verified live on Linux (streams the real System journal newest-first). Needs `systemd-python`.
- **Gotcha (recorded):** `lib_log_rich` registers an empty stub `systemd`/`systemd.journal` in
  `sys.modules` (its optional-journald handling) that has no `Reader` and shadows the real package;
  the adapter's `_load_journal` evicts the stub and imports the real one (lib_log_rich keeps its own
  reference, so it is unaffected).
- **Mechanism:** `systemd.journal.Reader` (python-systemd) - a cursor you seek + iterate, so it fits
  the memory-bounded streaming discipline exactly (no read-all). Filter with `add_match(...)`.
- **Field map -> `EventLogEntry`:** `__REALTIME_TIMESTAMP` -> time; `PRIORITY` (0-7) -> level;
  `_SYSTEMD_UNIT` or `SYSLOG_IDENTIFIER` -> source; `MESSAGE` -> message; `_PID`/`_UID` as extras.
- **`PRIORITY` -> `EventLevel`:** 0-3 (emerg/alert/crit/err) -> ERROR; 4 (warning) -> WARNING;
  5-6 (notice/info) -> INFORMATION; 7 (debug) -> VERBOSE. (Decide the exact split when built.)
- **Log-name arg:** a Windows channel ("System") maps to a unit/identifier match or the whole journal;
  define the convention (e.g. `get_win_event("sshd")` -> `add_match(_SYSTEMD_UNIT="sshd.service")`).
- **Dep:** `systemd-python` (or read the journal via `jeepney` is not viable - use python-systemd).
  Linux-only. **Privilege:** reading the system journal needs the `systemd-journal` group or root.

## 2. Local users/groups -> pwd/grp + shadow-utils  `get_local_user` / `get_local_group` + verbs  [DONE]

- **Status:** read in `adapters/native/posix_accounts.py`, mutation in
  `adapters/native/posix_account_control.py`, both dispatched by platform in `build_ps`. Read verified
  live (58 users / 103 groups, root at uid 0); the mutating verbs verified live on a root scratch
  user + group (`tests/test_posix_account_controller.py`).
- **Read mechanism (stdlib, no dep):** `pwd.getpwall()` / `grp.getgrall()`.
- **`LocalUser` map:** `pw_name` -> name; `pw_uid` -> the canonical id (the Linux "SID"); `pw_gecos`
  -> full name/description; `pw_dir`/`pw_shell` as extras; "enabled" is inferred (shell not
  `/usr/sbin/nologin`, no `!`/`*` password lock).
- **`LocalGroup` map:** `gr_name` -> name; `gr_gid` -> id; `gr_mem` -> members.
- **Carry the uid/gid as the canonical id field** (mirrors carrying the SID on Windows) so scripts
  match on the number, not a name.
- **Mutation (shell-free argv, the documented exception - no stable native account API):**
  `useradd` (+ `chpasswd` for the password) / `userdel` / `usermod -L/-U` for enable-disable,
  `groupadd`/`groupdel`, `gpasswd --add/--delete` for membership. Needs root. The returned record is
  read back from `pwd`/`grp`, with `enabled` set to the value the verb applied (the real lock state
  is in root-only `/etc/shadow`). A POSIX group has no description, so `new_group`'s is ignored.

## 3. Services -> systemd  `get_service` + start/stop/restart/set  [DONE]

- **Status:** implemented in `adapters/native/systemd_services.py` (read `iter_services` +
  `SystemdServiceController`), dispatched by platform in `build_ps`. Verified live: the read path on
  Linux, the mutating verbs on a root scratch unit (`tests/test_systemd_controller.py`: start->Running,
  stop->Stopped, enable->Automatic, disable->Disabled over real D-Bus).
- **D-Bus:** object `/org/freedesktop/systemd1`, interface `org.freedesktop.systemd1.Manager`.
- **Read:** `ListUnits()` -> filter to names ending `.service`; per unit `GetUnitFileState(name)`
  for the start type. **Mutate:** `StartUnit(name,"replace")` / `StopUnit` / `RestartUnit`;
  `EnableUnitFiles([name],false,false)` / `DisableUnitFiles([name],false)`; then `Reload()`. Each verb
  polls `ActiveState` until the unit settles, then returns its `ServiceInfo`.
- **`ActiveState` -> `ServiceState`:** active->RUNNING, inactive/failed->STOPPED,
  activating->START_PENDING, deactivating->STOP_PENDING, reloading->CONTINUE_PENDING.
- **`UnitFileState` -> `ServiceStartType`:** enabled/enabled-runtime->AUTOMATIC, disabled/masked->
  DISABLED, static/indirect/generated->MANUAL (documented fuzz).
- **`ServiceInfo` gaps:** `service_type` is Windows-shaped and left `None` on Linux (Optional);
  `can_pause_continue` is `False`. **Scope:** system units (system bus); user units later.
- **Gotcha (recorded):** a stopped unit *unloads*, so `GetUnit` then errors "not loaded" - the
  post-stop `ServiceInfo` reads `inactive` from that. And jeepney's `Properties.Get` returns the
  D-Bus variant as a `(signature, value)` tuple that must be unwrapped, and names an error reply's
  message-type `error` (lowercase) - both cost a live-test round to catch.
- **Privilege:** start/stop/enable need polkit/root (like Windows needs admin).

## 4. Scheduled tasks -> systemd timers  `get_scheduled_task` + verbs  [DONE]

- **Status:** implemented in `adapters/native/systemd_timers.py`, dispatched by platform in
  `build_ps`, verified live (read unprivileged; the mutating verbs on root scratch units in
  `tests/test_systemd_task_controller.py`).
- **Model:** a scheduled task = a `.timer` unit (the schedule) triggering a `.service` unit (the work).
- **Read** (`iter_timers`): `ListUnits()` filter to `.timer`; state from ActiveState + enablement from
  `GetUnitFileState`; description from the unit. Marshals to the same `ScheduledTaskInfo`.
- **Mutate:** `register` writes an on-demand oneshot `.service` under `/etc/systemd/system` (matching
  the Windows on-demand `Register-ScheduledTask` pwshpy exposes) + `Reload`; `run`/`stop` =
  `StartUnit`/`StopUnit` of the service; `enable`/`disable` = `EnableUnitFiles`/`DisableUnitFiles` of a
  task's `.timer`; `unregister` stops + removes the files pwshpy wrote (never a vendor unit under /lib).
- **The gotcha (recorded):** an on-demand oneshot **cannot be "disabled"** on systemd - a mask symlink
  cannot shadow the unit file we wrote in `/etc` (`/etc` outranks `/run`, and a persistent mask
  path-collides). So enable/disable are timer-only and raise a clear error for an on-demand task;
  "runnable/not" is not a systemd concept for a static unit.
- **`TaskState` map:** armed timer -> READY, activating -> QUEUED, inactive/failed -> DISABLED; an
  on-demand service is RUNNING while executing else READY.
- **Privilege:** system units need root (like the Windows verbs need admin); user timers are future work.

## 5. `elevate()` relaunch -> sudo / pkexec  [DONE]

- **Status:** implemented in `adapters/native/elevation.py` (`_sudo_relaunch`), dispatched inside
  `elevate` by platform; verified on Linux (root -> no-op returns 0; the sudo argv build is unit-tested).
- `is_elevated()` / `require_elevation()` were **already portable** (euid 0). Only the *relaunch* was
  Windows-only (UAC).
- **Linux relaunch:** re-exec via `sudo` (or `pkexec` for a GUI polkit prompt), preserving argv + cwd:
  roughly `os.execvp("sudo", ["sudo", executable, *argv])`. Decide sudo-vs-pkexec (or try pkexec then
  fall back to sudo). **Testable logic:** building the argv is `os_agnostic`; the actual re-exec needs
  a Linux TTY.

## 6. Credential store -> Secret Service  `save`/`load`/`delete_credential`  [DONE]

- **Status:** implemented in `adapters/native/secret_service.py`, dispatched by platform in
  `build_ps`, verified live against gnome-keyring (save/load/delete round-trip; locked and absent
  keyrings give a clear error, not a crash).
- **Mechanism:** freedesktop.org Secret Service over D-Bus (gnome-keyring / kwallet) on the **session
  bus**, driven directly with **jeepney** (no `secretstorage`, no `cryptography`). An item's secret,
  label and attributes map onto the `Credential` / `SecretStr` model; items are scoped by the
  attributes `{application: pwshpy, target: <target>}` and carry `username` as an attribute.
- **Plain algorithm, no crypto:** the session (`OpenSession("plain", ...)`) sends the secret
  unencrypted over the per-user unix socket - same trust boundary as the process - so no DH key
  exchange and no `cryptography` dependency. gnome-keyring accepts it.
- **Headless / locked caveat (handled):** no keyring daemon, no session bus, or a locked default
  collection that would need an interactive unlock prompt -> a clear `NativeCallError`, never a
  silently dropped secret. (A `keyctl` kernel-keyring fallback could come later.)
- `get_credential` (the prompt) is already portable.

## 7. ACLs -> POSIX ACLs  `get_acl` + verbs  [DONE]

- **Status:** implemented in `adapters/native/posix_acl.py`, dispatched by platform in `build_ps`.
  Read cross-checked entry-for-entry against `getfacl`; add/remove ACE + set_owner verified live on
  a temp file (hermetic, so the tests also run in CI's ubuntu lane, skipping if the fs lacks ACLs).
- **Mechanism (stdlib, no dep):** the kernel's binary `system.posix_acl_access` extended attribute
  via `os.getxattr` / `os.setxattr`, plus the mode bits via `os.stat` and `os.chown` for the owner -
  the same structures `getfacl`/`setfacl` use, parsed/packed directly (no `pylibacl`, no subprocess).
- **The redesign (no lie):** `AclEntry` gained `kind` (:class:`AclEntryKind`: TRUSTEE on Windows;
  OWNER / USER / OWNING_GROUP / GROUP / MASK / OTHER on POSIX) and `permissions` (the POSIX `rwx`
  string; empty on Windows, where the raw `rights` mask is authoritative because Windows rights do
  not reduce to `rwx`). POSIX ACLs are allow-only, so `access_type` is always ALLOW there.
- **Mutation semantics:** `trustee` is `user:NAME` / `group:NAME` (bare = user), `rights` uses the
  low 3 bits as `rwx`, a deny ACE is rejected, and the mask is recomputed on every change (dropped
  when the last named entry is removed). A file with no extended ACL reads back its three base
  entries synthesized from the mode.
- **Privilege:** editing a file's ACL needs to own it (or CAP_FOWNER); `set_owner` to another user
  needs root - like the Windows verbs need admin / SeRestorePrivilege.

---

## Not portable (no honest Linux analog) - stay Windows-only

- **Registry** (`get_item_property`, `registry_keys`, the mutating verbs) - Linux config is files,
  not a hive. No port.
- **CIM / WMI** (`get_cim_instance`) - no WMI on Linux. The classes people actually query (CPU, memory,
  disks, net) are already covered by the portable `get_process` / `get_volume` / `get_net_*` commands.
  A general port would be a per-class scavenger hunt across `/proc`, `/sys`, and D-Bus system services;
  not worth faking.
- **Hotfixes** (`get_hotfix`) - `dpkg -l` / `rpm -qa` is "installed packages", not "OS patches". Weak
  analog; if wanted, expose a separate `get_package` rather than pretend it is Get-Hotfix.

## Testing model

| Layer                                                            | Marker                                                | Where it runs                           |
|------------------------------------------------------------------|-------------------------------------------------------|-----------------------------------------|
| Pure mapping (priority->level, ActiveState->state, uid handling) | `os_agnostic`                                         | anywhere, including the Windows dev box |
| Live read (journald / pwd-grp / ListUnits)                       | new `os_linux` (+ `local_only` for anything mutating) | a Linux box or CI ubuntu                |
| Mutating (start/stop, useradd, register timer)                   | `local_only` + `os_linux`                             | a throwaway root Linux box              |

Isolate the D-Bus / journald / sudo I/O behind the pure mapping functions so the untested-on-Windows
surface is as small as possible.
