# Backends & Platforms

[Back to the README](../README.md) | **Prev:** [CLI Reference](cli-reference.md)

pwshpy does NOT wrap `pwsh.exe`. It has two backends that marshal into the **same** typed records,
so the pipeline composes over either source.

## Native

win32 / wmi / winreg / psutil / stdlib socket bindings. Fast, no .NET. This is the default for
common cmdlets.

- The **portable subset** (psutil + stdlib socket / os.environ) works on every OS: processes,
  connections, disks, uptime, resolve, test-connection, env, network adapters, computer info, the
  current identity, and the images of a WIM file (`get_windows_image` reads the file itself, no DISM).
- **Known folders** (`get_known_folder`) are portable too, with one resolver per OS that answers
  what pwsh's `[Environment]::GetFolderPath` answers there (see [Known folders](#known-folders)).
- The **portable native subsystems** dispatch by OS - win32 on Windows, an honest Linux backend
  on Linux: services (systemd D-Bus), event log (journald), scheduled tasks (systemd timers),
  local accounts (pwd / grp / shadow-utils), ACLs (POSIX ACL xattr; Linux only, so macOS raises
  `PlatformUnsupportedError`) - read and mutating verbs.
  Where the two OS models differ, the mapping is documented rather than faked; a field the Linux
  model lacks is dropped where nothing depends on it (a POSIX group has no description, so
  `new_local_group`'s is ignored). Scheduled tasks are the widest gap, and there an input with no
  analog raises `PlatformUnsupportedError` before anything is changed: a principal is a SID on
  Windows and a decimal uid on Linux, a task with no principal runs as the interactive caller on
  Windows and as root on Linux, and logon triggers, every-N-days daily triggers and logon types are
  Windows-only (the full table is in
  [Library & Pipeline](library-usage.md#scheduled-tasks-principal-run-level-and-triggers)).
- Only five are **Windows-only**, having no honest Linux analog: the registry (Linux config is
  files), CIM/WMI, hotfixes, the Windows Update search, and shortcuts (`.lnk`, a Windows shell
  file format: `get_shortcut` / `new_shortcut`).

## Known folders

`ps.get_known_folder(folder)` (CLI `pwshpy get_known_folder FOLDER`) returns the path pwsh's
`[Environment]::GetFolderPath(<SpecialFolder>)` returns on the same machine, or `None` where pwsh
returns `""` (the folder is missing or not readable). The rules are .NET 10's own, read from
dotnet/runtime at the commit pwsh 7.6 ships with:
[`Environment.Win32.cs`](https://github.com/dotnet/runtime/blob/6aa72daa24f66c01c3d7b6a32519e9d21396deec/src/libraries/System.Private.CoreLib/src/System/Environment.Win32.cs#L208-L387)
for Windows,
[`Environment.GetFolderPathCore.Unix.cs`](https://github.com/dotnet/runtime/blob/6aa72daa24f66c01c3d7b6a32519e9d21396deec/src/libraries/System.Private.CoreLib/src/System/Environment.GetFolderPathCore.Unix.cs#L20-L249)
for Linux and macOS, and
[`PersistedFiles.Unix.cs`](https://github.com/dotnet/runtime/blob/6aa72daa24f66c01c3d7b6a32519e9d21396deec/src/libraries/System.Private.CoreLib/src/System/IO/PersistedFiles.Unix.cs#L49-L85)
for the Linux home. An oracle test runs pwsh on each OS and compares its answers with pwshpy's.

| `KnownFolder` (value)                     | Windows (`SHGetKnownFolderPath`)               | Linux                                                                         | macOS                                             |
|-------------------------------------------|------------------------------------------------|-------------------------------------------------------------------------------|---------------------------------------------------|
| `DESKTOP` (`Desktop`)                     | `FOLDERID_Desktop`                             | `$XDG_DESKTOP_DIR`, else its `user-dirs.dirs` line, else `<home>/Desktop`     | Foundation's `<home>/Desktop`                     |
| `DOCUMENTS` (`MyDocuments`)               | `FOLDERID_Documents`                           | `$XDG_DOCUMENTS_DIR`, else its `user-dirs.dirs` line, else `<home>/Documents` | Foundation's `<home>/Documents`                   |
| `LOCAL_APP_DATA` (`LocalApplicationData`) | `FOLDERID_LocalAppData`, else `%LOCALAPPDATA%` | `$XDG_DATA_HOME`, else `<home>/.local/share`                                  | Foundation's `<home>/Library/Application Support` |
| `ROAMING_APP_DATA` (`ApplicationData`)    | `FOLDERID_RoamingAppData`, else `%APPDATA%`    | `$XDG_CONFIG_HOME`, else `<home>/.config`                                     | Foundation's `<home>/Library/Application Support` |
| `PROGRAM_DATA` (`CommonApplicationData`)  | `FOLDERID_ProgramData`, else `%ProgramData%`   | `/usr/share`                                                                  | `/usr/share`                                      |
| `PROGRAMS` (`Programs`)                   | `FOLDERID_Programs`                            | `PlatformUnsupportedError`                                                    | `PlatformUnsupportedError`                        |

- **Windows:** `SHGetKnownFolderPath(<FOLDERID>, 0, NULL)` verifies the folder exists, and returns a
  redirected folder (OneDrive, folder redirection) at its real location. When the shell reports the
  folder missing (HRESULT `0x80070002` or `0x80070003`), the three application-data members fall
  back to their environment variable as pwsh does, but only to a directory that exists; every other
  member is `None`. Any other HRESULT (an access denied, `0x80004005` for a folder with no
  file-system path) raises `NativeCallError`, where .NET would answer `""` silently.
- **Linux:** the home is `HOME` when non-empty, else the effective uid's passwd entry, else `/`. An
  `XDG_*` variable counts only when it is an absolute path. `user-dirs.dirs` is read from
  `XDG_CONFIG_HOME` (absolute) or `<home>/.config`, taking the first valid `KEY="$HOME/..."` or
  `KEY="/..."` line, as .NET parses it. `XDG_CONFIG_DIRS` is not consulted.
- **macOS:** .NET asks Foundation, whose home is CoreFoundation's: `CFFIXED_USER_HOME`, else the
  passwd entry of the effective uid (the real uid when the effective one is root), and `HOME` only
  when neither yields a directory. Foundation standardizes the path it returns: a leading
  `/private/var/automount`, `/var/automount` or `/private` component is dropped when the rest still
  exists (Apple's `stringByStandardizingPath` documentation), so a home under
  `/private/var/folders/xx/...` answers `/var/folders/xx/...`, as pwsh does.
- **Linux and macOS:** a path that fails `access(R_OK)` is `None`. `Programs` raises
  `PlatformUnsupportedError`, because .NET maps it to no folder there.

## .NET (hosts PowerShell 7.6 in-process)

`Microsoft.PowerShell.SDK` on .NET 10, hosted **in-process** via pythonnet, behind the optional
`[full]` extra. It covers the long tail (AD / Exchange / Azure / any module-only cmdlet) and runs
existing `.ps1` at full fidelity - never as a subprocess. Without `[full]`, any .NET call raises a
clear `FeatureUnavailableError` and loads no .NET.

## Platform support

- **Linux / macOS:** the portable native subset, the portable native subsystems on their Linux
  backends (systemd, journald, pwd/grp, POSIX ACL, Secret Service, sudo), and .NET when `[full]`
  and the .NET 10 runtime are present. The ACL commands are Linux only: on macOS they raise
  `PlatformUnsupportedError`.
- **Windows:** the same surface on win32 backends, plus the five Windows-only subsystems
  (registry, CIM/WMI, hotfixes, Windows Update search, shortcuts), and .NET.
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
credential store portable to Linux is **done** - each dispatches by OS. Only the registry, CIM/WMI,
hotfixes, the Windows Update search and shortcuts remain Windows-only, deliberately: they have no
honest Linux equivalent. The
[portability roadmap](portability-roadmap.md) tracks the detail.
