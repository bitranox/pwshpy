# Backends & Platforms

[Back to the README](../README.md) | **Prev:** [CLI Reference](cli-reference.md)

pwshpy does NOT wrap `pwsh.exe`. It has two backends that marshal into the **same** typed records,
so the pipeline composes over either source.

## Native

win32 / wmi / winreg / psutil / stdlib socket bindings. Fast, no .NET. This is the default for
common cmdlets.

- The **portable subset** (psutil + stdlib socket / os.environ) works on every OS: processes,
  connections, disks, uptime, resolve, test-connection, env, network adapters, computer info.
- The **win32/wmi bulk** is Windows-only: services, registry, event log, CIM, scheduled tasks,
  local accounts, ACLs, hotfixes - read and mutating verbs.

## .NET (hosts PowerShell 7.6 in-process)

`Microsoft.PowerShell.SDK` on .NET 10, hosted **in-process** via pythonnet, behind the optional
`[full]` extra. It covers the long tail (AD / Exchange / Azure / any module-only cmdlet) and runs
existing `.ps1` at full fidelity - never as a subprocess. Without `[full]`, any .NET call raises a
clear `FeatureUnavailableError` and loads no .NET.

## Platform support

- **Linux / macOS:** the portable native subset, plus .NET when `[full]` and the .NET 10 runtime
  are present.
- **Windows:** the portable subset, plus the win32/wmi native bulk, the Windows-only power tools
  (self-elevation relaunch, credential vault), and .NET.
- **Every OS:** the portable power tools `ps.exec`, `ps.write_text` / `ps.write_text_stream` /
  `ps.write_records`, `ps.get_content_lines`, `ps.download_file`, `ps.is_elevated`, and
  `ps.get_credential`.

## Python baseline

- Targets **Python 3.10 and newer**.
- Base runtime deps: `psutil`, `rich-click`, `lib_cli_exit_tools`, `lib_log_rich`,
  `lib_layered_config`, `pydantic`, `orjson` (plus `pywin32` / `wmi` on Windows only). The `[full]`
  extra adds `pythonnet` + `clr-loader` for .NET and requires the .NET 10 runtime.
- CI exercises GitHub's rolling runner images (`ubuntu-latest`, `macos-latest`, `windows-latest`)
  across CPython 3.10 through 3.14.

## Status

The portable native surface and CLI are implemented; the win32/wmi native bulk and the real
in-process .NET host are the Windows/.NET integration targets. Develop the portable core on Linux
for a fast loop; Windows is the authoritative integration target for the win32/wmi bulk and real
.NET.

Making the Windows-only subsystems (services, event log, scheduled tasks, elevation relaunch,
credentials) portable to Linux via systemd / journald / D-Bus / stdlib is tracked in the
[portability roadmap](portability-roadmap.md).
