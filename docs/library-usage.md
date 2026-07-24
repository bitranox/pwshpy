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

## Errors

Every failure across both backends raises a subclass of `PwshPyError`: `NativeCallError` (a wrapped
native/OS failure), `PowerShellError` (a .NET error record), `FeatureUnavailableError` (a .NET
call without the `[full]` extra), `PlatformUnsupportedError` (a Windows-only call off Windows), and
`ConfigurationError`. No terminating-vs-non-terminating distinction, no `-ErrorAction Stop` to
remember, no silently swallowed error.
