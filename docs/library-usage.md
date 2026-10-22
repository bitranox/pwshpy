# Library & Pipeline

[Back to the README](../README.md) | **Prev:** [Quick Start](quickstart.md) | **Next:** [Power Tools](power-tools.md)

```python
from pwshpy import ps
```

That one import is the whole library. Every method on `ps` is the PowerShell cmdlet, lowercased
with `-` turned into `_`: `Get-Service` is `ps.get_service()`, `Get-WinEvent` is
`ps.get_win_event(...)`. Porting a script is close to mechanical (see [COMMANDS.md](../COMMANDS.md)).

## Typed records, not text

Read commands yield Pydantic records, not strings you have to re-parse:

```python
# filter and project with ordinary Python - no lambda required
def is_python(proc):
    return proc.name == "python"


for proc in ps.get_process().where(is_python).take(5):
    print(proc.pid, proc.name, proc.status.value)

# or skip the pipeline entirely and write a plain loop - exactly as lazy
for conn in ps.get_net_tcp_connection():
    if conn.status.value == "LISTEN":
        print(conn.local_address, conn.local_port)

# projecting a field is a plain comprehension
addresses = [record.address for record in ps.resolve_dns_name("example.com")]
```

Records include `ProcessInfo`, `NetConnection`, `NetAdapter`, `NetIpAddress`, `DiskUsage`,
`SystemUptime`, `DnsRecord`, `ConnectionTest`, `EnvVar`, `FileSystemItem`, `ComputerInfo`,
`CurrentIdentity`, `WindowsImage`, `ProcessResult` (from `ps.exec`), the portable admin set
(`ServiceInfo`, `EventLogEntry`, `ScheduledTaskInfo`, `LocalUser`, `LocalGroup`, `AclEntry`,
`Credential`) and the Windows set (`RegistryValue`, `RegistryKey`, `CimInstance`, `Hotfix`,
`WindowsUpdate`, ...). They serialize to JSON directly (`record.to_dict()`), and enum fields carry
canonical values regardless of the host locale.

Compare an enum field with its member, never with a display string. All are exported from `pwshpy`:
`ProcessStatus` (psutil's lowercase values, `running`), `ConnectionState` (`LISTEN`),
`TransportProtocol`, `AddressFamily`, `ServiceState` (`Running`), `ServiceStartType`, `ServiceKind`,
`EventLevel` (`Error`), `TaskState`, `TaskRunLevel`, `TaskLogonType`, `RegistryHive`,
`RegistryValueType`, `AceType`, `WellKnownSid` (the SID strings, `S-1-5-18`), `IntegrityLevel`,
`ImageArchitecture` and `RunnerFormat`. In JSON an enum field is its value, so a `jq` filter on
`--jsonl` output compares `.status == "Running"` for a service and `.status == "running"` for a
process.

### Process names

`ps.get_process()` names each process as psutil does. On Windows two pseudo-processes are named
differently from `Get-Process`: the kernel's memory compression store is `MemCompression`
(`Get-Process`: `Memory Compression`) and PID 0 is `System Idle Process` (`Get-Process`: `Idle`).
Every other name matches `Get-Process`'s `ProcessName` (psutil's `.exe` is stripped). Match by
`pid` where it matters.

## The pipeline

Every read command returns a lazy `Pipeline`:

| Operator            | Kind      | What it does                                            |
|---------------------|-----------|---------------------------------------------------------|
| `.where(pred)`      | streaming | keep matching records                                   |
| `.select(fn)`       | streaming | project each record                                     |
| `.take(n)`          | streaming | first `n` records                                       |
| `.first(pred=None)` | terminal  | first match (or `default`); **stops early**             |
| `.sort_by(key)`     | terminal  | sorted; **buffers every item** (sorting needs them all) |
| `.to_list()`        | terminal  | the whole thing as a list; **pulls the entire source**  |

## Memory model (read this once)

Nothing is read from the source until a terminal operator pulls it, and the operators split by
memory cost:

- **Streaming, O(1) extra memory:** `where` / `select` / `take` pull one item at a time; `first`
  pulls until the first match and then stops. `ps.get_win_event("System").where(is_error).take(10)`
  touches at most a handful of events no matter how large the log.
- **Iterating terminal, O(1):** `for x in pipeline` consumes one item at a time.
- **Materializing terminal, O(n):** `to_list()` pulls the entire source into a list, and `sort_by()`
  must buffer every item first. On an unbounded source (millions of event-log rows, a giant
  directory tree) these hold everything in memory at once.

Rule of thumb: keep it lazy. Narrow with `.where(...)`, cap with `.take(N)` or `.first()`, and only
then `.to_list()` or `.sort_by()`. On the CLI prefer `--jsonl` (streams) over the human table (which
materializes, soft-capped at 1000 rows). Calling `.to_list()` on a full unbounded source is the one
way to blow memory - and it is always your explicit choice, never a hidden default.

Everything with an unbounded source has a streaming path: `ps.get_content_lines(path)` (vs the
whole-file `ps.get_content`), `ps.write_text_stream` / `ps.write_records` (write record-by-record),
and `ps.download_file(url, dest)` (copies to disk in chunks, never buffering the body). See
[Power Tools](power-tools.md) for those.

## Scheduled tasks: principal, run level and triggers

`ps.register_scheduled_task(task_path, *, program, arguments="", description="", principal=None,
triggers=())` creates (or replaces) a task that runs `program` with `arguments`. It is Task
Scheduler on Windows and a systemd `.service` plus, when there are triggers, a `.timer` on Linux.
Registering needs admin on Windows and root on Linux. The inputs are frozen Pydantic models,
exported from `pwshpy`, that refuse unknown keys:

- `TaskPrincipal(user_id=None, logon_type=None, run_level=TaskRunLevel.LIMITED)` - who the task
  runs as. `user_id` is a SID (`"S-1-5-18"`, or a `WellKnownSid` member) or a decimal uid
  (`"1000"`), never an account name: names are localized (`Administrators` / `Administratoren`),
  so a name is refused rather than resolved (see [Locale & Identity](locale-and-identity.md)).
  `TaskPrincipal.system()` is SYSTEM at `TaskRunLevel.HIGHEST`. `logon_type` (`TaskLogonType`) is
  Windows-only; left at None it follows `user_id`: a service account SID (SYSTEM, LOCAL SERVICE,
  NETWORK SERVICE) logs on as `ServiceAccount`, any other SID as `S4U` (runs whether or not the
  user is logged on, with no stored password), and no `user_id` as `InteractiveToken`.
- `triggers` - a sequence of `OnceTrigger(at=...)`, `DailyTrigger(at=..., every_days=1)`
  (1 to 365), `BootTrigger(delay=timedelta(0))` and `LogonTrigger(user_id=None)`; a task fires on
  any of them. Every `at` must be timezone-aware; a naive datetime is refused. `delay` counts whole
  seconds. An empty sequence means the task runs only on demand (`ps.start_scheduled_task`).

A daily trigger is written with a fixed UTC offset on both OSes: Linux converts `at` to UTC, and
Windows stores it in the trigger's `StartBoundary` with the offset the `datetime` carries. On Linux
it therefore fires at the same UTC time of day all year, so a `03:00+01:00` trigger fires at `04:00`
local time once a `+01:00` zone switches to summer time. On Windows a fixed offset implies the
same; that follows from `StartBoundary`'s format and was not observed across a DST change on a
Windows host.

```python
from datetime import datetime, timedelta, timezone

from pwshpy import BootTrigger, DailyTrigger, TaskPrincipal, ps

cet = timezone(timedelta(hours=1))

# Windows: SYSTEM, elevated, every night at 03:00 +01:00 and five minutes after each boot
ps.register_scheduled_task(
    "\\Maintenance\\NightlyBackup",
    program="C:\\Tools\\backup.exe",
    arguments="/full",
    principal=TaskPrincipal.system(),
    triggers=[DailyTrigger(at=datetime(2026, 11, 1, 3, 0, tzinfo=cet)), BootTrigger(delay=timedelta(minutes=5))],
)

# Linux: uid 1000, every night at 03:00 +01:00 (written as 02:00 UTC)
ps.register_scheduled_task(
    "nightly-backup",
    program="/usr/local/bin/backup",
    arguments="--full",
    description="Nightly backup",
    principal=TaskPrincipal(user_id="1000"),
    triggers=[DailyTrigger(at=datetime(2026, 11, 1, 3, 0, tzinfo=cet))],
)
```

The Linux call writes these two units under `/etc/systemd/system`, enables the timer and starts it:

```ini
# nightly-backup.service
[Unit]
Description=Nightly backup
X-PwshPy=1.6.0

[Service]
Type=oneshot
User=1000
ExecStart="/usr/local/bin/backup" --full

# nightly-backup.timer
[Unit]
Description=Nightly backup
X-PwshPy=1.6.0

[Timer]
OnCalendar=*-*-* 02:00:00 UTC
AccuracySec=1s
Unit=nightly-backup.service

[Install]
WantedBy=timers.target
```

How each input maps, and what each OS refuses before touching the scheduler:

| Input                                  | Windows (Task Scheduler)                                                          | Linux (systemd)                                                                                |
|----------------------------------------|-----------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------|
| `task_path`                            | `\Folder\Name` (`\Name` at the root)                                              | the unit base name; any folder and a `.timer` / `.service` suffix are dropped                  |
| no principal                           | the interactive caller (`InteractiveToken`: runs while that user is logged on)    | root (a system unit with no `User=`)                                                           |
| SYSTEM (`S-1-5-18`)                    | SYSTEM, `ServiceAccount`                                                          | root                                                                                           |
| decimal uid                            | `PlatformUnsupportedError`                                                        | `User=<uid>`                                                                                   |
| any other SID                          | that account (`S4U` unless `logon_type` says otherwise)                           | `PlatformUnsupportedError`                                                                     |
| well-known group SID                   | `ConfigurationError` (Everyone, Authenticated Users, `S-1-5-32-*`)                | `PlatformUnsupportedError` (a SID)                                                             |
| `logon_type`                           | `ServiceAccount` exactly for `S-1-5-18/19/20`; a mismatch is `ConfigurationError` | `PlatformUnsupportedError` (systemd has no logon types)                                        |
| `run_level=HIGHEST`                    | the elevated (unfiltered) token                                                   | root only; a non-root uid is `PlatformUnsupportedError`                                        |
| `OnceTrigger`                          | a time trigger at `at`                                                            | `OnCalendar=YYYY-MM-DD HH:MM:SS UTC`                                                           |
| `DailyTrigger`, `every_days=1`         | a daily trigger starting at `at`                                                  | `OnCalendar=*-*-* HH:MM:SS UTC`; runs from today, an `at` date in the future is not honoured   |
| `DailyTrigger`, `every_days>1`         | a daily trigger every N days                                                      | `PlatformUnsupportedError` (no every-N-days calendar)                                          |
| `BootTrigger`                          | a boot trigger with `delay`                                                       | `OnBootSec=<seconds>s`; the timer is enabled but not started, so it waits for the next boot    |
| `LogonTrigger`                         | a logon trigger for `user_id` (a SID; None = any user, a group SID refused)       | `PlatformUnsupportedError` (a user unit fires when the user manager starts, not at each logon) |
| boot and calendar triggers in one task | allowed                                                                           | `PlatformUnsupportedError`; register them as two tasks                                         |
| no triggers                            | on demand only                                                                    | a `.service` only, on demand                                                                   |

On Linux every timer pwshpy writes carries `AccuracySec=1s` (systemd's default window would let it
fire up to a minute late). `program`, `arguments` and `description` go into the unit file, so a line
break or NUL in any of them is a `ConfigurationError` and `%` is escaped. The `ExecStart=` line is
`program`, written as one double-quoted word (so a path with a space stays one path), followed by
`arguments`, which systemd reads with its own rules, not a shell's: it splits and unquotes them
itself and expands `$VAR` / `${VAR}` from the unit's environment (write `$$` for a literal dollar).
A `program` starting with `+`, `!`, `-`, `@` or `:` is a `ConfigurationError`: systemd reads that
character as an `ExecStart=` prefix even inside quotes, and `+` would run it with full privileges,
ignoring `User=`.

The task name must be a name systemd accepts for a unit: ASCII letters, digits, `:`, `-`, `_` and
`.`, at most 247 characters (255 with `.service`), and no `@` (a template or instance unit), or
`register`, `unregister`, `run`, `stop`, `enable` and `disable` raise `ConfigurationError` before
anything is written or sent. Every unit pwshpy writes carries `X-PwshPy=<version>` in `[Unit]`,
which systemd ignores, and pwshpy only replaces or deletes a unit that carries it: `register` raises
`NativeCallError` when `/etc/systemd/system` holds a unit of that name without the key, or systemd
already has one from anywhere else (a vendor unit under `/lib`, a runtime or masked one), and
`unregister` stops, disables and deletes only the marked files, raising `NativeCallError` ("no such
task") when there are none. A unit registered by pwshpy 1.5 or earlier carries no key and is refused
the same way. `ps.enable_scheduled_task` on a boot timer also only enables it,
because systemd runs a started boot timer at once when its offset has already passed.

`register_scheduled_task` returns the task read back as a `ScheduledTaskInfo`, the same record
`ps.get_scheduled_task()` yields. Its principal and run-information fields:

| Field              | Windows                                                                               | Linux                                                                                                |
|--------------------|---------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| `user_id`          | the principal's SID; `""` for a group principal or an account that no longer resolves | the service's `User=` as a decimal uid, `"0"` for root; `""` when it cannot be read or resolved      |
| `run_level`        | `Limited` / `Highest`; None when unreadable                                           | always None (no split token)                                                                         |
| `last_run_time`    | aware UTC; None when the task never ran                                               | the later of the timer's last trigger and the service's last start, so a `run` counts; None if never |
| `last_task_result` | the exit code or HRESULT, unsigned 32-bit; None when it never ran or is running now   | the exit status, `128 + signal` when killed; None when it never ran or is running now                |
| `next_run_time`    | aware UTC; None when nothing is scheduled                                             | the timer's next wall-clock elapse; None for an on-demand task or a boot-only timer                  |

`last_task_result` 0 means the last run succeeded. On Linux a registered calendar or boot task
reads `Ready`, a disabled one `Disabled`, and an on-demand task `Ready` (`Running` while it runs).

### Listing tasks: skipped versus strict

`ps.get_scheduled_task(folder_path="\\", *, strict=False)` streams the tasks under a folder
(Windows) or every systemd timer (Linux). Something the caller cannot read - on Windows a folder
whose tasks or subfolders it may not list, or a protected or corrupt task; on Linux a refused
unit-file listing or a timer unit systemd will not load or read - is skipped by default, so a non-elevated
caller still gets everything it can read, and each skip logs a WARNING through the `logging`
module naming what was skipped and why. With `strict=True` the same item raises `NativeCallError`
naming the folder, task or unit. Pass it whenever an absent record must mean an absent task, such as
checking that a cleanup really deleted one:

```python
from pwshpy import NativeCallError, ps

try:
    leftover = ps.get_scheduled_task("\\Maintenance\\", strict=True).first(lambda t: t.task_name == "Old")
except NativeCallError as exc:
    print(f"cannot tell whether the task is gone: {exc}")
else:
    print("still there" if leftover else "deleted")
```

The pipeline is lazy, so the error is raised when iteration reaches the unreadable item: records
before it have already been yielded. Systemd timers that are not-found or masked, aliases and
templates are no tasks in either mode; a single field that cannot be read (a run time, a principal)
falls back to its documented default and the task stays listed.

## Listing services: skipped versus strict

`ps.get_service(*, strict=False)` streams the services. On Linux something systemd does not answer -
a refused unit-file listing (then no stopped, unloaded service can be listed) or a service unit it
will not load or whose load state it will not read - is skipped by default with a WARNING through
the `logging` module naming the unit and the error, so a non-root caller still gets every service it
can read. With `strict=True` the same item raises `NativeCallError` naming it, when iteration
reaches it. Pass it when an absent record must mean an absent service:

```python
from pwshpy import NativeCallError, ps

try:
    leftover = ps.get_service(strict=True).first(lambda s: s.name == "legacy-agent.service")
except NativeCallError as exc:
    print(f"cannot tell whether the service is gone: {exc}")
else:
    print("still there" if leftover else "removed")
```

On Windows the listing drops no service (`EnumServicesStatusEx` returns them all in one call), so
`strict` changes nothing there: a service whose configuration cannot be queried is still listed with
`start_type` None and no `required_services`. On both OSes a single field that cannot be read keeps
its default and the service stays listed; services systemd reports as not-found or masked (and
stopped), aliases, templates and stale unit files are no services in either mode.

## Windows images: `get_windows_image`

`ps.get_windows_image(path, *, index=None)` lists the images inside a `.wim` (or a split `.swm` part),
like `Get-WindowsImage -ImagePath`. It reads the file's header and XML resource itself, so it works
on any OS with no DISM and no mounting:

```python
from pwshpy import ImageArchitecture, ps

for image in ps.get_windows_image("install.wim"):
    print(image.index, image.name, image.architecture.value, image.version)

pro = ps.get_windows_image("install.wim", index=6).first()  # like -Index 6; stops reading after it
x64 = ps.get_windows_image("install.wim").where(lambda i: i.architecture is ImageArchitecture.X64).to_list()
```

Each record is a `WindowsImage`: `index`, `name`, `description`, `edition_id`, `installation_type`,
`architecture`, `version` (`MAJOR.MINOR.BUILD.SPBUILD`), `build`, `sp_build`, `languages`,
`default_language` and `size_bytes`. Nothing is opened until the pipeline is consumed, so an
unreadable file, a file that is not a WIM, a compressed XML resource (resource flag 0x04, refused
rather than guessed at) and an `index` the file does not hold all raise `NativeCallError` then. An
`index` below 1 raises `ValueError` at the call.

## Deadlines and long waits

Every call that waits takes a `timeout` in seconds, and running out of it raises
`OperationTimeoutError` (its `.timeout` holds the limit), with two exceptions: `ps.run` (.NET) has
no timeout and reports a failure as `PowerShellError`, and `ps.elevate(wait=True)` waits for the
elevated child without a limit. `OperationTimeoutError` subclasses `NativeCallError`, so catch it
first to tell "ran out of time" from "refused" or "could not start":

```python
from pwshpy import NativeCallError, OperationTimeoutError, ps

try:
    ps.stop_service("wuauserv", timeout=60)
except OperationTimeoutError as exc:
    print(f"still stopping after {exc.timeout}s")  # the stop WAS issued; the service is on its way
except NativeCallError as exc:
    print(f"refused: {exc}")
```

- **Services:** `start_service`, `stop_service` and `restart_service` wait until the service settles
  (default 30 s) and raise `OperationTimeoutError` after the control was issued, on Windows and
  Linux alike. On Linux a start or restart whose unit settles in systemd's `failed` state raises
  `NativeCallError` naming the unit's `Result` (`exit-code`, `signal`, `timeout`, ...); a
  `Type=oneshot` unit that ran and ended inactive has succeeded and reads `Stopped`. `stop_service(name, wait=False)` issues the stop and returns the state right after
  it, typically `StopPending`, for a service that takes long to stop.
- **Programs:** `exec(argv, timeout=...)` kills the program when the time is up; the output it wrote
  so far is not returned. `exec(argv, wait_tree=True)` waits, like `Start-Process -Wait`, until the
  program and every process it started have exited, and `timeout` then covers and kills the whole
  tree. What counts as the tree on each OS is in
  [Power Tools](power-tools.md#waiting-for-the-whole-process-tree-wait_treetrue-like-start-process--wait).
  On macOS (and any POSIX system other than Linux) `wait_tree=True` starts the program in a new
  session, so it has no controlling terminal.
- **Processes and names:** `wait_process(pid, timeout=...)` raises it when the process is still
  running at the deadline (the process is left running), and `resolve_dns_name(name, timeout=...)`
  when the lookup has not answered (the lookup is abandoned and ends when the OS resolver returns).
- **Windows Update:** `get_windows_update(criteria=..., timeout=300.0)` runs the search in a child
  process that is killed when the time is up. The search starts on the first iteration and
  returns its whole result before the first record, so `.take(n)` does not shorten it.

The Windows Update search child is started with the `spawn` method, which re-imports the script that
called it. A script that uses `get_windows_update` must therefore keep its top level under a
`__main__` guard; without one the child fails at bootstrap and the call raises `NativeCallError`
("worker exited without a result"):

```python
from pwshpy import ps


def main() -> None:
    for update in ps.get_windows_update(timeout=600):
        print(update.kb_article_ids, update.title)


if __name__ == "__main__":
    main()
```

On the CLI a timeout exits 110 (see [CLI Reference](cli-reference.md#exit-codes)).

## Errors

Every failure across both backends raises a subclass of `PwshPyError`, all exported from `pwshpy`:

- `NativeCallError` - a wrapped native/OS failure, a `strict=True` listing that cannot read an item,
  or `ProcessResult.check()` on a nonzero exit.
  - `OperationTimeoutError` - a call ran out of its `timeout` (`.timeout` holds the limit).
  - `ElevationDeclinedError` - the UAC prompt or sudo's password was refused by `ps.elevate()`.
- `ElevationRequiredError` - `ps.require_elevation()` in a process that is not elevated.
- `PowerShellError` - a .NET error record.
- `FeatureUnavailableError` - a .NET call without the `[full]` extra.
- `PlatformUnsupportedError` - a Windows-only call off Windows, or a scheduled-task input the running
  OS has no analog for.
- `ConfigurationError` - invalid configuration, or scheduled-task inputs that contradict each other.
- `PackError` - a script could not be packed or unpacked.

No terminating-vs-non-terminating distinction, no `-ErrorAction Stop` to remember, no silently
swallowed error. Catch a subclass before its parent (`OperationTimeoutError` and
`ElevationDeclinedError` before `NativeCallError`).

## Building your own facade

`ps` is `build_ps()`, built once at import: `build_ps()` returns a fresh `Ps` facade wired with the
same native adapters. The CLI's own helpers are exported too: `get_config()` returns the layered
configuration the CLI reads (see [CONFIG.md](../CONFIG.md)), and `print_info()` prints the package
metadata `pwshpy info` shows.
