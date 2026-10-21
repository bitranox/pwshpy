# Backends & Platforms

[Back to the README](../README.md) | **Prev:** [CLI Reference](cli-reference.md)

pwshpy does NOT wrap `pwsh.exe`. It has two backends that marshal into the **same** typed records,
so the pipeline composes over either source.

## Native

win32 / wmi / winreg / psutil / stdlib socket bindings. Fast, no .NET. This is the default for
common cmdlets.

- The **portable subset** (psutil + stdlib socket / os.environ) works on every OS: processes,
  connections, disks, uptime, resolve, test-connection, env, network adapters, computer info.
- The **portable native subsystems** dispatch by OS - win32 on Windows, an honest Linux backend
  on Linux: services (systemd D-Bus), event log (journald), scheduled tasks (systemd timers),
  local accounts (pwd / grp / shadow-utils), ACLs (POSIX ACL xattr) - read and mutating verbs.
  Where the two OS models differ, the mapping is documented rather than faked; a field the Linux
  model lacks is dropped where nothing depends on it (a POSIX group has no description, so
  `new_local_group`'s is ignored). Scheduled tasks are the widest gap, and there an input with no
  analog raises `PlatformUnsupportedError` before anything is changed: a principal is a SID on
  Windows and a decimal uid on Linux, a task with no principal runs as the interactive caller on
  Windows and as root on Linux, and logon triggers, every-N-days daily triggers and logon types are
  Windows-only (the full table is in
  [Library & Pipeline](library-usage.md#scheduled-tasks-principal-run-level-and-triggers)).
- Only three are **Windows-only**, having no honest Linux analog: the registry (Linux config is
  files), CIM/WMI, and hotfixes.

## .NET (hosts PowerShell 7.6 in-process)

`Microsoft.PowerShell.SDK` on .NET 10, hosted **in-process** via pythonnet, behind the optional
`[full]` extra. It covers the long tail (AD / Exchange / Azure / any module-only cmdlet) and runs
existing `.ps1` at full fidelity - never as a subprocess. Without `[full]`, any .NET call raises a
clear `FeatureUnavailableError` and loads no .NET.

## Platform support

- **Linux / macOS:** the portable native subset, the portable native subsystems on their Linux
  backends (systemd, journald, pwd/grp, POSIX ACL, Secret Service, sudo), and .NET when `[full]`
  and the .NET 10 runtime are present.
- **Windows:** the same surface on win32 backends, plus the three Windows-only subsystems
  (registry, CIM/WMI, hotfixes), and .NET.
- **Every OS:** the portable power tools `ps.exec`, `ps.write_text` / `ps.write_text_stream` /
  `ps.write_records`, `ps.get_content_lines`, `ps.download_file`, `ps.is_elevated`, and
  `ps.get_credential`.

## Python baseline

- Targets **Python 3.10 and newer**.
- Base runtime deps: `psutil`, `rich-click`, `lib_cli_exit_tools`, `lib_log_rich`,
  `lib_layered_config`, `pydantic`, `orjson` (plus `pywin32` / `wmi` on Windows only). The `[full]`
  extra adds `pythonnet` + `clr-loader` for .NET and requires the .NET 10 runtime.
- CI exercises GitHub's rolling runner images (`ubuntu-latest`, `macos-latest`, `windows-latest`)
  across CPython 3.10 through 3.15.

## Status

The native surface, the CLI and the in-process .NET host are all implemented. Develop the portable
core on Linux for a fast loop; Windows remains the authoritative integration target for the win32
backends and real .NET, since neither installs on Linux.

Making services, event log, scheduled tasks, local accounts, ACLs, elevation relaunch and the
credential store portable to Linux is **done** - each dispatches by OS. Only the registry, CIM/WMI
and hotfixes remain Windows-only, deliberately: they have no honest Linux equivalent. The
[portability roadmap](portability-roadmap.md) tracks the detail.
