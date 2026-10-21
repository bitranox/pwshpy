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

Records include `ProcessInfo`, `NetConnection`, `DiskUsage`, `SystemUptime`, `DnsRecord`,
`ConnectionTest`, `EnvVar`, and the Windows set (`ServiceInfo`, `RegistryValue`, `EventLogEntry`,
`CimInstance`, `ScheduledTaskInfo`, `LocalUser`, `LocalGroup`, `AclEntry`, `Hotfix`, ...). They
serialize to JSON directly (`record.to_dict()`), and enum fields carry canonical English values
regardless of the host locale.

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

A daily trigger fires at the same UTC time of day on both OSes: Windows keeps the offset the
`datetime` carries, Linux converts it to UTC. A `03:00+01:00` daily trigger therefore fires at
`04:00` local time once a `+01:00` zone switches to summer time.

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

[Service]
Type=oneshot
User=1000
ExecStart=/usr/local/bin/backup --full

# nightly-backup.timer
[Unit]
Description=Nightly backup

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
`program` followed by `arguments`, and systemd reads it with its own rules, not a shell's: it
splits and unquotes the arguments itself and expands `$VAR` / `${VAR}` from the unit's environment
(write `$$` for a literal dollar). `ps.enable_scheduled_task` on a boot timer also only enables it,
because systemd runs a started boot timer at once when its offset has already passed.

`register_scheduled_task` returns the task read back as a `ScheduledTaskInfo`, the same record
`ps.get_scheduled_task()` yields. Its principal and run-information fields:

| Field              | Windows                                                                               | Linux                                                                                           |
|--------------------|---------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------|
| `user_id`          | the principal's SID; `""` for a group principal or an account that no longer resolves | the service's `User=` as a decimal uid, `"0"` for root; `""` when it cannot be read or resolved |
| `run_level`        | `Limited` / `Highest`; None when unreadable                                           | always None (no split token)                                                                    |
| `last_run_time`    | aware UTC; None when the task never ran                                               | the timer's last trigger (the service's last start for an on-demand task); None if never        |
| `last_task_result` | the exit code or HRESULT, unsigned 32-bit; None when it never ran                     | the exit status, `128 + signal` when killed; None when it never ran or is running now           |
| `next_run_time`    | aware UTC; None when nothing is scheduled                                             | the timer's next wall-clock elapse; None for an on-demand task or a boot-only timer             |

`last_task_result` 0 means the last run succeeded. On Linux a registered calendar or boot task
reads `Ready`, a disabled one `Disabled`, and an on-demand task `Ready` (`Running` while it runs).

## Errors

Every failure across both backends raises a subclass of `PwshPyError`: `NativeCallError` (a wrapped
native/OS failure), `PowerShellError` (a .NET error record), `FeatureUnavailableError` (a .NET
call without the `[full]` extra), `PlatformUnsupportedError` (a Windows-only call off Windows, or an
input the running OS has no analog for), and `ConfigurationError`. No terminating-vs-non-terminating distinction, no `-ErrorAction Stop` to
remember, no silently swallowed error.
