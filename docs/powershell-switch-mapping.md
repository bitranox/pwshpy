# PowerShell switch mapping - reasoning per command and parameter

pwshpy is a Python **library used inside a Python script**, not a shell. A
PowerShell cmdlet's parameters are therefore NOT ported mechanically: each
parameter is reasoned into one of four categories below, and the decision is
recorded here so every omission is justified, not accidental. When a concern is
"easier to solve in the Python script", that is a deliberate, documented choice.

## Decision categories

| Category  | Covers                                                                 | Why it wins in a Python script                                                                                                                                                             |
|-----------|------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PIPELINE  | filtering, sorting, selecting, shaping the result set                  | Python predicates over the lazy pipeline (`.where` / `.sort_by` / `.select` / `.take` / `.first`) are typed, composable, and more expressive than cmdlet params                            |
| PYTHON    | scripting / host concerns (errors, streams, capture, format)           | Python already owns these: `try/except` + the `PwshPyError` tree, our `lib_log_rich` logging, plain variables, `--json` / `--jsonl` / orjson; some results are trivially derived in Python |
| INTEGRATE | a genuine input, scope, or DATA capability the pipeline cannot emulate | the data must be fetched differently and has no Python-side substitute, so it becomes a method arg, a CLI option, or a record field                                                        |
| N/A       | interactive or PowerShell-host-only behaviour                          | no meaning for an in-process library: pipeline object binding, `-Confirm` / `-WhatIf` on a read verb, the PS formatting engine                                                             |

The Windows oracle tests the INTEGRATE decisions and the default record fields
against real PowerShell; this document justifies every PIPELINE / PYTHON / N/A
omission - the "does it make sense?" rigor, per switch.

## Common parameters (shared by every cmdlet)

Decided once here; per-command sections list only cmdlet-specific parameters.

| Parameter                                                                     | Category | Reasoning                                                                                 |
|-------------------------------------------------------------------------------|----------|-------------------------------------------------------------------------------------------|
| `-Verbose`, `-Debug`                                                          | PYTHON   | pwshpy emits structured logs via lib_log_rich; verbosity is a logging level, not a switch |
| `-ErrorAction`, `-ErrorVariable`                                              | PYTHON   | errors are typed exceptions in the `PwshPyError` tree; handle with `try/except`           |
| `-WarningAction/-Variable`, `-InformationAction/-Variable`, `-ProgressAction` | PYTHON   | stream routing is a logging concern                                                       |
| `-OutVariable`, `-OutBuffer`, `-PipelineVariable`                             | PYTHON   | capture is a Python variable; the pwshpy pipeline is already lazy                         |

## Get-Service  ->  `pwshpy services` / `ps.services()`

Parameter sets (PS 7.x): Default (`-Name`), DisplayName, InputObject. PS 7 has
**no `-ComputerName`** (removed for cross-platform; use remoting).

| Parameter            | Type                | Category          | Decision / reasoning                                                                                                                                                                                                                                                                 |
|----------------------|---------------------|-------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `-Name`              | string[]            | PIPELINE          | filter by service name: `ps.services().where(lambda s: s.name in {...})`; a wildcard is a Python predicate / `fnmatch`                                                                                                                                                               |
| `-DisplayName`       | string[]            | PIPELINE          | filter by display name via `.where(...)`                                                                                                                                                                                                                                             |
| `-Include`           | string[]            | PIPELINE          | wildcard post-filter over the retrieved set -> `.where(...)`                                                                                                                                                                                                                         |
| `-Exclude`           | string[]            | PIPELINE          | inverse wildcard post-filter -> `.where(...)`                                                                                                                                                                                                                                        |
| `-RequiredServices`  | switch              | INTEGRATE (field) | the services THIS one depends on come from its own config (`QueryServiceConfig`) and are not derivable otherwise -> `ServiceInfo.required_services: list[str]`                                                                                                                       |
| `-DependentServices` | switch              | PYTHON (derive)   | the services that depend on X are the INVERSE of every service's required list, so a caller computes them in Python - `[s.name for s in ps.services() if "X" in s.required_services]` - instead of paying a per-service `EnumDependentServices` Win32 call. Documented, not fetched. |
| `-InputObject`       | ServiceController[] | N/A               | PowerShell pipeline object binding; a Python caller passes/iterates records directly                                                                                                                                                                                                 |

### Resulting `ServiceInfo` record

INTEGRATE decisions plus the ServiceController fields worth carrying by default
(the oracle compares these against `Get-Service | Select-Object *`):

| Field                | Source (win32service)                     | Enum / note                                                   |
|----------------------|-------------------------------------------|---------------------------------------------------------------|
| `name`               | service key name (`EnumServicesStatusEx`) | the short name                                                |
| `display_name`       | display name                              |                                                               |
| `status`             | current state                             | `ServiceState` IntEnum (Stopped/Running/Paused/...)           |
| `start_type`         | `QueryServiceConfig` start type           | `ServiceStartType` IntEnum (Boot/System/Auto/Manual/Disabled) |
| `service_type`       | service type flags                        | `ServiceKind` IntEnum (KernelDriver/Win32OwnProcess/...)      |
| `pid`                | `QueryServiceStatusEx` dwProcessId        | `None`/0 when not running                                     |
| `can_stop`           | controls-accepted flags                   |                                                               |
| `can_pause_continue` | controls-accepted flags                   |                                                               |
| `required_services`  | `QueryServiceConfig` dependencies         | `list[str]` of service names                                  |

Mutating verbs (`Start`/`Stop`/`Restart`/`Set`/`New`/`Remove-Service`) belong to
the destructive-action design (handover section 3), NOT this read subsystem.

## To document as each command is built or retrofitted

Read commands whose full parameter analysis is still TODO (same table format):

- Portable, already shipped: Get-Process, Get-Volume / Get-PSDrive,
  `Get-ChildItem env:`, Resolve-DnsName (note `-Type` is INTEGRATE - a query
  input), Get-NetTCPConnection, Get-Uptime, Test-Connection.
- Registry: Get-ItemProperty / Get-ChildItem (note `-Recurse` is INTEGRATE - a
  scope the flat pipeline cannot emulate).
- Future Windows-only: Get-WinEvent, Get-CimInstance, scheduled tasks, local
  accounts, ACLs.
