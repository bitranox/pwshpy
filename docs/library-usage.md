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
`WindowsUpdate`, `ShortcutInfo`, ...). They serialize to JSON directly (`record.to_dict()`), and enum fields carry
canonical values regardless of the host locale. `to_dict()` writes a mapping key holding a byte
that is not valid UTF-8 (in an open property bag such as `CimInstance.properties`, decoded with
`surrogateescape`) as one U+FFFD per such byte, keeping a key that then collides with another as
`<key>~2` (`~3`, ...), so `orjson.dumps(record.to_dict())` never fails over a key. A string
value holding such a byte is left as it is; `ps.write_records` and the CLI's JSON output repair
it (below).

Compare an enum field with its member, never with a display string. All are exported from `pwshpy`:
`ProcessStatus` (psutil's lowercase values, `running`), `ConnectionState` (`LISTEN`),
`TransportProtocol`, `AddressFamily`, `ServiceState` (`Running`), `ServiceStartType`, `ServiceKind`,
`EventLevel` (`Error`), `TaskState`, `TaskRunLevel`, `TaskLogonType`, `RegistryHive`,
`RegistryValueType`, `AceType`, `WellKnownSid` (the SID strings, `S-1-5-18`), `IntegrityLevel`,
`ImageArchitecture`, `KnownFolder` (the .NET `SpecialFolder` names, `MyDocuments`) and
`RunnerFormat`. In JSON an enum field is its value, so a `jq` filter on
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

`ps.write_records` writes a field holding a byte that is not valid UTF-8 with U+FFFD for each such
byte, so the file stays valid UTF-8 JSON, and logs one WARNING per affected record (logger
`pwshpy.adapters.native.fileio`) naming the record type, its 1-based position and the field paths.

## Scheduled tasks: principal, run level and triggers

`ps.register_scheduled_task(task_path, *, program, arguments="", description="", principal=None,
triggers=(), settings=None)` creates (or replaces) a task that runs `program` with `arguments`. It is Task
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
- `settings` - a `TaskSettings` for batteries, the time limit, overlapping runs, hard terminate and
  starts on demand; None (the default) keeps every OS default. See [Task settings](#task-settings).

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
break, NUL or any other control character (a tab, an escape, DEL; systemd allows none in a command
line) in any of them is a `ConfigurationError` and `%` is escaped. The `ExecStart=` line is
`program`, written as one double-quoted word (so a path with a space stays one path), followed by
`arguments`, which systemd reads with its own rules, not a shell's: it splits and unquotes them
itself and expands `$VAR` / `${VAR}` from the unit's environment (write `$$` for a literal dollar).
A `program` starting with `@`, `-`, `:`, `+`, `!` or `|` is a `ConfigurationError`: systemd reads
that character as an `ExecStart=` prefix even inside quotes, `+` would run it with full privileges,
ignoring `User=`, and `|` through a shell. So is a `program` that is a lone `;` (systemd's
command-line separator).

The task name must be a name systemd accepts for a unit, at most 247 characters (255 with
`.service`), or the call raises `ConfigurationError` before anything is written or sent. `register`
and `unregister`, which write and delete unit files, take ASCII letters, digits, `:`, `-`, `_` and
`.` only, so no `@` (a template or instance unit) and no backslash. `run`, `stop`, `enable` and
`disable` write no unit file (`enable` and `disable` only add or remove the `timers.target.wants`
link) and also take an instance name such as `zfs-scrub-monthly@rpool`, as
`ps.get_scheduled_task()` lists it: a non-empty part before the first `@`, then an instance of the
same characters plus `@`; either part may hold systemd's `\xNN` escapes
(`zfs-scrub-monthly@my\x2dpool`, as `systemd-escape` writes `my-pool`), which are part of the name. A
bare template (`scrub@`, nothing after the `@`) is refused. A task path that starts with a backslash is
the Windows form `\Folder\Name`: every backslash and `/` ends a folder, so `\Tasks\backup` names
`backup` and `\Maintenance\xfer_logs` names `xfer_logs`. In any other task path a folder ends at `/` or
at a backslash that does not start such an escape. Every unit
pwshpy writes carries `X-PwshPy=<version>` in `[Unit]`, which systemd ignores, and pwshpy only
replaces or deletes a unit that carries it: `register` raises `NativeCallError` when
`/etc/systemd/system` holds a unit of that name without the key, or systemd already has one from
anywhere else (a vendor unit under `/lib`, a runtime or masked one), and `unregister` stops,
disables and deletes only the marked files: it raises `NativeCallError` ("not written by pwshpy")
when a file of that name without the key is there, and ("no such task") when there is no file at
all. A unit registered by pwshpy 1.5 or earlier carries no key and is refused the same way; to take
such a task over, remove its `.service` / `.timer` files under `/etc/systemd/system` by hand
(`systemctl disable --now` the timer first), or add `X-PwshPy=` to their `[Unit]` section.
`ps.enable_scheduled_task` on a boot timer also only enables it, because systemd runs a started boot
timer at once when its offset has already passed.

`register_scheduled_task` returns the task read back as a `ScheduledTaskInfo`, the same record
`ps.get_scheduled_task()` yields. Its principal and run-information fields (its `settings` are
described under [Task settings](#task-settings)):

| Field              | Windows                                                                               | Linux                                                                                                |
|--------------------|---------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| `user_id`          | the principal's SID; `""` for a group principal or an account that no longer resolves | the service's `User=` as a decimal uid, `"0"` for root; `""` when it cannot be read or resolved      |
| `run_level`        | `Limited` / `Highest`; None when unreadable                                           | always None (no split token)                                                                         |
| `last_run_time`    | aware UTC; None when the task never ran                                               | the later of the timer's last trigger and the service's last start, so a `run` counts; None if never |
| `last_task_result` | the exit code or HRESULT, unsigned 32-bit; None when it never ran or is running now   | the exit status, `128 + signal` when killed; None when it never ran or is running now                |
| `next_run_time`    | aware UTC; None when nothing is scheduled                                             | the timer's next wall-clock elapse; None for an on-demand task or a boot-only timer                  |

`last_task_result` 0 means the last run succeeded. On Linux a registered calendar or boot task
reads `Ready`, a disabled one `Disabled`, and an on-demand task `Ready` (`Running` while it runs).

### Task settings

`settings=TaskSettings(...)` decides how a task behaves around batteries, a run that takes too long,
a start while a run is still active, and starts on demand. The field names follow PowerShell's
`New-ScheduledTaskSettingsSet`. `TaskSettings` is a frozen Pydantic model exported from `pwshpy`
that refuses unknown keys, and every field defaults to None. A None field writes nothing on either
OS, so the OS default applies: `settings=None`, `TaskSettings()` and any field you leave out change
nothing about the task pwshpy registered before this parameter existed.

| Field                         | What it decides                                                                                                       |
|-------------------------------|-----------------------------------------------------------------------------------------------------------------------|
| `allow_start_if_on_batteries` | whether a run may start while the machine is on battery                                                               |
| `stop_if_going_on_batteries`  | whether a running task is stopped when the machine switches to battery                                                |
| `execution_time_limit`        | how long a run may last before it is stopped (a `timedelta` of whole seconds; `timedelta(0)` means no limit)          |
| `multiple_instances`          | what a start does while a run is active (`TaskMultipleInstances`: `PARALLEL`, `QUEUE`, `IGNORE_NEW`, `STOP_EXISTING`) |
| `allow_hard_terminate`        | whether a run that ignores a stop request may be killed                                                               |
| `allow_demand_start`          | whether `ps.start_scheduled_task` may start the task besides its triggers (False is `-DisallowDemandStart`)           |

A long unattended job that runs as SYSTEM, has no trigger and is started on demand:

```python
from datetime import timedelta

from pwshpy import TaskMultipleInstances, TaskPrincipal, TaskSettings, ps

task = ps.register_scheduled_task(
    "\\Maintenance\\Repair",
    program="C:\\Tools\\repair.exe",
    principal=TaskPrincipal.system(),  # SYSTEM, elevated
    settings=TaskSettings(
        allow_start_if_on_batteries=True,  # a laptop on battery still starts it
        stop_if_going_on_batteries=False,  # pulling the charger does not stop it
        execution_time_limit=timedelta(hours=72),  # stopped after 72 hours; timedelta(0) = no limit
        multiple_instances=TaskMultipleInstances.IGNORE_NEW,  # a start while it runs does nothing
        allow_hard_terminate=True,  # a run that ignores the stop request is killed
        allow_demand_start=True,  # ps.start_scheduled_task may start it
    ),
)
ps.start_scheduled_task("\\Maintenance\\Repair")
print(task.settings)  # the settings as the scheduler reads them back
```

Linux accepts the same `TaskSettings` for a task without a daily trigger. It writes only
`TimeoutStartSec=259200` into the `.service`, because systemd already behaves the way the other
five fields ask.

How each field is written, and the Windows default it replaces (read from a fresh task, measured;
`register_scheduled_task` writes `AllowDemandStart` in any case, see below):

| Field                         | Windows (Task Scheduler)                                               | Windows default | Linux (systemd `.service`)                                                                                                   |
|-------------------------------|------------------------------------------------------------------------|-----------------|------------------------------------------------------------------------------------------------------------------------------|
| `allow_start_if_on_batteries` | `DisallowStartIfOnBatteries` = not the value                           | False           | False: `ConditionACPower=true` in `[Unit]`; True: nothing                                                                    |
| `stop_if_going_on_batteries`  | `StopIfGoingOnBatteries`                                               | True            | False: nothing; True: `PlatformUnsupportedError`                                                                             |
| `execution_time_limit`        | `ExecutionTimeLimit` as `PT<n>S` (0: `PT0S`, no limit)                 | 72 hours        | `TimeoutStartSec=<n>` in `[Service]` (0: `infinity`)                                                                         |
| `multiple_instances`          | `MultipleInstances` (Parallel 0, Queue 1, IgnoreNew 2, StopExisting 3) | IgnoreNew       | IgnoreNew without a `DailyTrigger`: nothing; IgnoreNew with a `DailyTrigger`, or any other value: `PlatformUnsupportedError` |
| `allow_hard_terminate`        | `AllowHardTerminate`                                                   | True            | False: `SendSIGKILL=no` in `[Service]`; True: nothing                                                                        |
| `allow_demand_start`          | `AllowDemandStart`                                                     | True            | False: `RefuseManualStart=yes` in `[Unit]`; True: nothing                                                                    |

On Windows `register_scheduled_task` always writes `Enabled=True` and `AllowDemandStart=True`, with
or without `settings` (`AllowDemandStart` is False only when `allow_demand_start=False`); both are
also Task Scheduler's defaults. `Enabled` is no `TaskSettings` field: it reads back as the record's
`enabled`.

`execution_time_limit` counts whole seconds. A negative value, a fraction of a second or more than
2147483647 seconds (about 68 years) raises `pydantic.ValidationError` before anything is written.
The cap comes from Task Scheduler, which refuses 2^31 to 2^32-1 seconds (0x80041318) and wraps a
larger value modulo 2^32 (`PT4294967296S` is stored as `PT0S`, no limit), so a bigger value would
silently mean something else; the same cap holds on Linux to keep one rule. Windows normalises what
it stores: `PT259200S` reads back as `PT72H` and 90 seconds as `PT1M30S`. pwshpy hands back a
`timedelta` either way, and the JSON and JSONL output (`--json`, `--jsonl`, `model_dump_json()`)
spells it in ISO 8601 form: 72 hours is `P3D` and no limit `PT0S`. Task Scheduler's own tools show
`PT72H` for the same value.

On Linux:

- `allow_start_if_on_batteries=False` writes `ConditionACPower=true`. systemd checks it when a run
  starts and skips the run while the machine is on battery (from the systemd.unit(5)
  documentation, not measured here). A machine that reports no power supply at all counts as on
  AC power, so the run starts there (measured in a container).
- `stop_if_going_on_batteries=True` is refused: systemd checks `ConditionACPower=` only at the start
  and never stops a running unit when the power source changes.
- `multiple_instances`: a start requested while the oneshot service runs joins the running start
  job, so one run happens, which is IgnoreNew. systemd cannot run instances in parallel, queue them
  or stop the running one, so `PARALLEL`, `QUEUE` and `STOP_EXISTING` are refused. IgnoreNew with a
  `DailyTrigger` is refused too: when a run outlasts its day, the timer fires one catch-up run as
  soon as it ends instead of skipping the missed day. Leave `multiple_instances` unset for a daily
  task on Linux.
- `execution_time_limit` becomes `TimeoutStartSec=`, because a oneshot service's start job lasts
  the whole run: a run over the limit is ended with SIGTERM and `Result=timeout` (measured).
- `allow_hard_terminate=False` writes `SendSIGKILL=no`. A process that ignores SIGTERM then survives
  the stop, and while it lives every new start of the unit is refused with
  `Result=resources` until it exits (measured). Use it only for a program that honours SIGTERM.

Every refusal is a `PlatformUnsupportedError` raised before any unit is written.

`allow_demand_start=False` leaves only the triggers to start the task. `ps.start_scheduled_task`
then raises `NativeCallError` and runs nothing, on both OSes: on Windows it wraps Task Scheduler's
`SCHED_E_START_ON_DEMAND` (0x80041328), on Linux systemd's refusal ("Operation refused, unit ...
may be requested by dependency only"); on Linux the timer still starts the task, and
`ps.stop_scheduled_task` still works. pwshpy does not refuse `allow_demand_start=False` without a
trigger, but such a task can never run.

The record's `settings` holds the effective values, OS defaults included; a field that cannot be
read is None. The two OSes read back differently:

| Field                        | Windows           | Linux                                                                                       |
|------------------------------|-------------------|---------------------------------------------------------------------------------------------|
| `multiple_instances`         | the stored policy | always None: a repeating timer's catch-up run after an overrun is none of the four policies |
| `stop_if_going_on_batteries` | the stored value  | always False (systemd never stops a run on battery), also when the service cannot be loaded |
| `execution_time_limit`       | the stored value  | read only for a `Type=oneshot` service (what pwshpy writes); None for any other type        |
| a task that cannot be read   | every field None  | `stop_if_going_on_batteries` False and every other field None                               |

A record's read-back `settings` can be passed to another `register_scheduled_task` call, and they
are validated again there: a value outside the `TaskSettings` rules (a fractional or over-cap
limit read from a hand-written unit or task) raises `ValidationError`, and Windows defaults passed
to a Linux register (`stop_if_going_on_batteries=True`, `multiple_instances=IgnoreNew` with a
daily trigger) raise `PlatformUnsupportedError`.

With `allow_start_if_on_batteries=False`, a start while the host runs on battery succeeds and runs
nothing: Windows does not start the run, and on Linux systemd skips the unit on its
`ConditionACPower` (cited from systemd.unit(5)).

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
before it have already been yielded. Systemd timers that are not-found or masked (and stopped),
aliases and templates are no tasks in either mode, while a running one is still listed; a single field that cannot be read (a run time, a principal)
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

## Known folders and desktop shortcuts

`ps.get_known_folder(folder)` returns a well-known folder as a `Path`, the one pwsh's
`[Environment]::GetFolderPath` names on the same machine, on every OS. On Windows it asks the shell,
so a Desktop redirected to OneDrive is found where `Path.home() / "Desktop"` would miss it. A folder
that is missing or not readable is `None`, never a guessed path.
`folder` is a `KnownFolder` member or its exact value: `DESKTOP` (`"Desktop"`), `DOCUMENTS`
(`"MyDocuments"`), `LOCAL_APP_DATA`, `ROAMING_APP_DATA`, `PROGRAM_DATA` and `PROGRAMS`, which exists
on Windows only (`PlatformUnsupportedError` elsewhere). The per-OS rules are in
[Backends & Platforms](backends-and-platforms.md#known-folders).

`ps.new_shortcut(path, *, target, ...)` writes a Windows shortcut (`.lnk`) and returns it as Windows
stored it; `ps.get_shortcut(path)` reads one. Together they let a tool install a desktop shortcut to
itself, for example one that starts it through `uv`:

```python
import shutil

from pwshpy import KnownFolder, ps

uv = shutil.which("uv")  # an absolute path: a bare "uv" target is refused
desktop = ps.get_known_folder(KnownFolder.DESKTOP)
if uv is None or desktop is None:
    raise SystemExit("no uv on PATH, or this account has no Desktop folder")

info = ps.new_shortcut(
    desktop / "My Tool.lnk",
    target=uv,
    arguments=["tool", "run", "--exclude-newer", "7 days", "mytool"],  # an argv list, never one string
    working_directory=str(desktop),
    description="Start My Tool",
    force=True,  # replace a shortcut an earlier install left behind
)
print(info.arguments)  # tool run --exclude-newer "7 days" mytool
```

The returned `ShortcutInfo` carries `path`, `target`, `arguments`, `working_directory`, `icon`,
`icon_index`, `description` and `run_as_administrator`. `arguments` is the one command-line string Windows stores, joined
with `subprocess.list2cmdline`, so the target's C runtime splits it back into the same argv; `target`
reads back with environment references expanded (`%SystemRoot%\notepad.exe` as
`C:\Windows\notepad.exe`), and an unset field is `""`. The write goes through `IShellLinkW`, so
non-ASCII text survives in every field and in the file name. A relative `path` resolves against the
working directory; no directory is created.

`run_as_administrator=True` makes the shortcut start its target elevated, with one UAC prompt and
one window (the "Run as administrator" box in the shortcut's Advanced properties). It is a bool
only; `get_shortcut` reports it for any shortcut, including one a pwsh script marked by patching
the file.

What each refusal raises, all before anything is written unless noted:

- `PlatformUnsupportedError`: not Windows.
- `ConfigurationError`: `target` is not an absolute Windows path and does not start with an
  environment reference (`%SystemRoot%\notepad.exe`); `arguments` is a bare `str`; a value Windows
  would silently cut, wrap or rename (`target` or the absolute `path` over 259 characters, the joined
  arguments over 32767, `description`, `working_directory` or `icon` over 260); NUL, CR or LF in any
  value; a `path` not ending in `.lnk`; or a `run_as_administrator` that is not a bool.
- `NativeCallError`: `path` exists and `force` is False, or the shell failed (a missing directory, a
  target whose environment reference expands past 259 characters, which Windows refuses with E_FAIL
  0x80004005 while writing nothing; the expansion depends on the host, so pwshpy cannot check it
  first). `get_shortcut` raises it for a missing file, an empty file or one that is no shortcut.

## Deadlines and long waits

Calls that can wait take a `timeout` in seconds. What running out of it raises depends on the call:

- `OperationTimeoutError` (its `.timeout` holds the limit; CLI exit 110): `exec`, `start_service`,
  `stop_service`, `restart_service`, `get_windows_update`, `wait_process`, `resolve_dns_name`,
  `download_file`, `invoke_web_request` and `invoke_rest_method` (whose `timeout`, default 30 s,
  bounds each connect and each read, not the whole transfer), and on Linux the local-account verbs
  (`new_local_user`, `remove_local_user`, `enable_local_user`, `disable_local_user`,
  `new_local_group`, `remove_local_group`, `add_local_group_member` and
  `remove_local_group_member`), whose shadow-utils commands it bounds together, 30 s by default:
  each command gets what is left, and the one running when it passes is killed (Windows accepts
  their `timeout` and does not use it: the calls are synchronous). When `new_local_user` already
  created the account and a later step fails or times out, `disabled=True` still locks it within a
  fixed 5 s grace, and the error names what was done (created, password set or not, locked or not).
- `PowerShellError`: the hosted .NET calls `ps.run`, `ps.cmdlet` and the wrappers over it
  (`get_ad_user`, `get_ad_group`, `get_ad_computer`, `get_mailbox`, `get_az_vm`,
  `get_az_resource_group`); the pipeline is stopped. Their `timeout` defaults to `None`, no limit.
- Nothing: `test_connection` returns a `ConnectionTest` with `reachable=False` and the reason in
  `error`.

`exec`, `wait_process` and `resolve_dns_name` wait without a limit when `timeout` is `None` (their
default), and `ps.elevate(wait=True)` takes no `timeout`: it waits for the elevated child without a
limit. `OperationTimeoutError` subclasses `NativeCallError`, so catch it first to tell "ran out of
time" from "refused" or "could not start":

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
  it, typically `StopPending`, for a service that takes long to stop. On Windows a start that meets
  `StopPending`, or a stop (also with `wait=False`) that meets `StartPending`, first waits for the
  service to settle within the same `timeout`; if it does not, `OperationTimeoutError` is raised
  without the control having been sent.
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

On the CLI an `OperationTimeoutError` exits 110 (see [CLI Reference](cli-reference.md#exit-codes)).
The other timeouts exit 1: `pwshpy run`, `pwshpy cmdlet` and the module shortcuts (`get_ad_user`,
...) with `--timeout` raise `PowerShellError`.

## Errors

Every failure across both backends raises a subclass of `PwshPyError`, all exported from `pwshpy`:

- `NativeCallError` - a wrapped native/OS failure, a `strict=True` listing that cannot read an item,
  or `ProcessResult.check()` on a nonzero exit. `.hresult` holds the failure's unsigned 32-bit
  HRESULT when the native error carried one (a pywin32 COM or Win32 error, or a Windows `OSError`
  mapped as `HRESULT_FROM_WIN32`), else `None`; branch on it, never on the localized message.
  - `NativeAccessDeniedError` - a call refused for lack of rights (`E_ACCESSDENIED`, `EACCES`,
    `EPERM`, psutil `AccessDenied`, a systemd D-Bus `AccessDenied` or
    `InteractiveAuthorizationRequired`, which a non-root caller gets for `stop_service` where polkit
    is installed, and a Secret Service D-Bus `AccessDenied` or `InteractiveAuthorizationRequired`).
    It is also a `PermissionError`, so `except PermissionError` (and `except OSError`) catches it;
    `errno` holds the POSIX code when the
    failure carried one. A refusal pwshpy reads only from a message or an exit status is a plain
    `NativeCallError`: a non-zero exit of a shadow-utils command.
  - `OperationTimeoutError` - a call ran out of its `timeout` (`.timeout` holds the limit).
  - On an HTTP error status: `invoke_web_request` and `invoke_rest_method` raise
    `NativeCallError` for any status urllib does not follow (a refused 3xx such as a redirect
    loop, 304, 4xx, 5xx), as `Invoke-WebRequest` does, naming the status and the first 200
    characters of the body; pass `skip_http_error_check=True` (pwsh's `-SkipHttpErrorCheck`) to get
    such an answer back (`invoke_rest_method` parses its body; an empty or whitespace-only body,
    such as a 304's, gives None, where `Invoke-RestMethod` returns the body text). `download_file`
    always raises for one. A redirect to a scheme other than `http`/`https` raises before anything
    connects to it, whatever `skip_http_error_check` says.
  - `ElevationDeclinedError` - the UAC prompt or sudo's password was refused by `ps.elevate()`,
    or sudo granted root but cached no credential for the command (sudoers `timestamp_timeout=0`):
    on POSIX `ps.elevate()` asks once with `sudo -v`, checks with `sudo -n -v` and runs the command
    under `sudo -n`, so sudo never prompts a second time. A user sudo lets run the current Python
    without a password (a `NOPASSWD` rule) is asked nothing: `ps.elevate()` first asks `sudo -n` to
    run that interpreter and, when it may, runs the command under `sudo -n` straight away (an
    explicit `executable=` other than the current interpreter is not probed).
- `ElevationRequiredError` - `ps.require_elevation()` in a process that is not elevated.
- `PowerShellError` - a .NET error record.
- `FeatureUnavailableError` - a .NET call without the `[full]` extra.
- `PlatformUnsupportedError` - a Windows-only call off Windows, a scheduled-task input the running
  OS has no analog for, or `KnownFolder.PROGRAMS` on Linux or macOS.
- `ConfigurationError` - invalid configuration, scheduled-task inputs that contradict each other, or
  a shortcut value a `.lnk` cannot store exactly.
- `PackError` - a script could not be packed or unpacked.

No terminating-vs-non-terminating distinction, no `-ErrorAction Stop` to remember, no silently
swallowed error. Catch a subclass before its parent (`NativeAccessDeniedError`,
`OperationTimeoutError` and `ElevationDeclinedError` before `NativeCallError`):

```python
from pwshpy import NativeAccessDeniedError, NativeCallError, ps

try:
    entries = ps.get_acl("/opt/locked/file").to_list()
except NativeAccessDeniedError:
    print("not allowed to read it; ask again elevated")
except NativeCallError as exc:
    print(f"failed: {exc} (HRESULT {exc.hresult})")
```

## Building your own facade

`ps` is `build_ps()`, built once at import: `build_ps()` returns a fresh `Ps` facade wired with the
same native adapters. The CLI's own helpers are exported too: `get_config()` returns the layered
configuration the CLI reads (see [CONFIG.md](../CONFIG.md)), and `print_info()` prints the package
metadata `pwshpy info` shows.
