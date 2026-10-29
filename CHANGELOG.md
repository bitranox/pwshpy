# Changelog

All notable changes to this project will be documented in this file following
the [Keep a Changelog](https://keepachangelog.com/) format.


## [Unreleased]

### Changed

- Every public method of the `ps` facade documents each parameter (`Args:`) and its result
  (`Returns:`, or `Yields:` for a generator), and, where traced, the errors it raises (`Raises:`),
  so `help(ps.<method>)` says what a value means and what can fail. A test fails when a method
  lacks an `Args:` entry, documents a parameter it does not have, or has an empty or missing result
  section.
- `get_net_tcp_connection` is documented as psutil's `inet` listing: it includes UDP sockets as
  well as TCP. Filter on `protocol` for TCP only. The behaviour is unchanged.

### Fixed

- `ps.elevate`, `pwshpy --elevate`, `--pwshpy-elevate` and `-PwshPyElevate` on Linux and macOS: a
  user who needs no password for the relaunch (a `NOPASSWD` sudoers rule) is no longer asked for one
  or refused. Before, `sudo -v` ran first, and under sudo's default `verifypw=all` it wants a password
  unless every sudoers rule of the user is `NOPASSWD`: beside Ubuntu's `%sudo` rule the user was
  asked for a password the command does not need, and without a terminal (or without any password,
  as on stock cloud images) elevation was declined with exit code 77. `sudo -n` is now first asked,
  silently, to run the program the relaunch runs (the current Python, `/bin/sh`, or the running
  `pwsh`); when it may, the relaunch runs under `sudo -n` without a prompt.
- Packed runners (`pwshpy pack`): an elevated run (`.ps1` with an administrator token or as
  SYSTEM, either runner as root) now judges its pack cache before it removes or writes anything
  there, and the unpacked tree again: the cache directory, every directory above it and every
  entry below it must be unmodifiable by other accounts, or the run stops with
  `pwshpy: refusing the cache directory <dir>: <reason>` and exit code 127. Before, a cache
  another account could write (a `PWSHPY_PACK_CACHE`, `LOCALAPPDATA`, `XDG_CACHE_HOME` or `HOME`
  the caller chose) was verified by hash and then run, leaving that account a window to swap a file.
- Packed `.ps1` runner, elevated on Windows: the account's data directory is judged before the
  runner creates `pwshpy\empty.gitconfig` below it, so a link on the way no longer gets a directory
  created at its target, and a create that fails (a file named `pwshpy` in the way) is refused with
  exit code 127 and the usual remedy instead of stopping with exit code 1 (70 in an elevated child).
- Packed `.ps1` runner, elevated (administrator token, SYSTEM, or root): a script with a git source
  now refuses a git older than 2.32 (or one whose `git --version` it cannot read) with exit code 127,
  as the `.sh` root run already did. Such a git ignores `GIT_CONFIG_GLOBAL` and read the caller's
  global git configuration, whose `core.fsmonitor` or `credential.helper` ran a command elevated.
  `-PwshPyInfo` shows whether the pack recorded a git source.
- Packed `.ps1` runner, elevated under Windows PowerShell 5.1: a `PATH` directory spelled in its
  extended-length form (`\\?\C:\...`) is judged by its ACL like `C:\...` and kept on uv's `PATH`
  when it is private. Before, 5.1 could not walk that spelling and left every such directory off.
- Packed `.ps1` runner, elevated on Windows: a drive letter mapped to a network share (`net use`, a
  logon script) is now refused like a UNC path (`it is a network path, and an elevated run trusts
  only local paths`), for a `uv`, a `PATH` directory and the directories the run judges; before, its
  ACL, which the share's server reports, was trusted. A drive whose type cannot be read is refused.
- Packed `.ps1` runner, elevated on Windows: a symlink or junction below uv's managed-Python
  directory (or the pack cache) whose target leaves that directory, or is a network path, now stops
  the run with exit code 127 (`the link <link> points to <target>, outside <dir>`). Before, a link
  was judged only as the link object, so a `.pth` there linking to a file nobody judged ran elevated.
- Packed runners, `--pwshpy-elevate` / `-PwshPyElevate` on Linux and macOS: sudo is asked for the
  password once (`sudo -v`), then `sudo -n -v` confirms a credential was cached and the relaunch
  runs under `sudo -n`, so sudo never prompts a second time. A sudo that caches nothing
  (`timestamp_timeout=0`) is declined with exit code 77 instead of asking again.
- Packed runners, root run on Linux and macOS (the `.sh` as root, the `.ps1` elevated): the runner
  sets the modes of what it unpacks (directories 0755, nothing group- or world-writable). Before, a
  caller's umask 002 made the unpacked tree group-writable, and the run then refused its own cache
  with exit code 127, on that run and every later one.
- Packed runners, root run on Linux and macOS with `PWSHPY_PACK_CACHE` unset: the pack cache is
  `<home>/.cache/pwshpy-pack` of the running account, the home read from the passwd database, as
  uv's cache already was. Before, it followed `XDG_CACHE_HOME` or `HOME`, which `sudo -E` or
  `env_keep` hand over from the caller, so the run unpacked where the caller chose, or refused its
  own default cache with exit code 127.
- Packed `.ps1` runner, elevated on Linux and macOS: the stderr of `uv python find` and of
  `git --version` is kept in a new file in a directory only root can enter, below the running
  account's own cache directory. Before, it went to a temporary file in `TMPDIR`, which `sudo -E`
  hands over from the caller, so an entry the caller swapped in at that name had root truncate and
  write the file it pointed to.
- Packed runners, root or elevated run of a script with a git source: the git uv would run is
  judged as a file, like `uv` (owner, write access, every directory and link on the way), and one
  another account can modify stops the run with `refusing git at <path>` and exit code 127 before
  it runs. Before, only the directories on uv's `PATH` were judged, so such a git ran as root.
- Packed `.ps1` runner, elevated on Windows: the gits that are judged are the ones uv chooses from.
  In the first `PATH` directory holding a git, every git file uv tries there is judged (the bare
  name, then each `PATHEXT` extension in order), and one that fails stops the run. So a `git.cmd`
  in an earlier directory is no longer passed over for a `git.exe` further down, and a bare `git`
  beside a `git.exe` is judged too. A git that is not a `.exe`, `.com`, `.cmd` or `.bat` is refused
  with exit code 127.
- Packed `.ps1` runner, elevated on Windows: a symlink or junction whose target is rooted on the
  current drive (`\Users\...`), a volume GUID name or a `GLOBALROOT` device path is refused (exit
  code 127, `points to <target>, which names no drive or share path`) in uv's managed-Python
  directory, the pack cache and on the way to a `uv`. Before, such a target counted as relative to
  the link's directory, so a link out of the tree passed.
- Packed runners, root run on Linux and macOS (the `.sh` as root, the `.ps1` elevated): uv and the
  git it runs get the running account's own `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and
  `XDG_DATA_HOME`, with `XDG_STATE_HOME`, `NETRC` and `UV_CREDENTIALS_DIR` unset. Before, they used
  the caller's, which `sudo -E` or `env_keep` hand over, so uv read the caller's netrc and
  credentials and created root-owned directories in the caller's home. The packed script still sees
  the caller's values; the shim gives back only these variables, whatever the hand-over names. A
  `.ps1` run that hands nothing over removes a hand-over marker the caller's environment preset (its
  shim runs every time); the `.sh` starts the shim only as root, so a preset marker reaches none.
- Packed runners, root run on Linux and macOS (the `.sh` as root, the `.ps1` elevated): uv, git and
  the build backends uv runs keep their temporary files in `<home>/.cache/pwshpy-tmp` of the running
  account (mode 0700; `TMPDIR`, `TMP` and `TEMP` all name it, and the packed script gets the
  caller's values back). Before, they used the caller's `TMPDIR`, which `sudo -E` hands over, where
  the caller could swap a directory root created for a link.
- Packed runners, root run on Linux and macOS (the `.sh` as root, the `.ps1` elevated): with no uv
  found, uv's installer runs with only the running account's own `HOME` and the fixed helper `PATH`
  (umask 022), and the uv search looks below that home, not in the caller's `XDG_BIN_HOME` or
  `CARGO_HOME`. Before, the installer ran with the caller's `HOME` and the caller's `UV_INSTALL_DIR`,
  `XDG_BIN_HOME`, `CARGO_HOME`, download-URL, proxy and certificate variables, so it put a
  root-owned uv in the caller's `~/.local/bin` and edited the caller's shell files, then the run
  refused that uv with exit code 127. Behind a proxy a root run therefore needs uv installed for
  root beforehand, or the proxy in root's own `~/.curlrc`, `/etc/wgetrc` or `~/.wgetrc`.
- Packed runners on Linux and macOS: uv's installer is downloaded to a file before it runs (as root
  into the account's private temporary directory, otherwise into a new directory in an existing
  writable `TMPDIR`, default `/tmp`), with `curl` and then `wget` when `curl` fails, and a failed
  download stops the run with exit code 127 and `could not download uv's installer from ...
  (<downloader> exited <code>, ...)`; a failed installer stops it with its own exit code named. A run
  that is not root (not elevated) now needs an existing `TMPDIR` it can write (default `/tmp`) to
  install uv, and otherwise stops with exit code 70 and `cannot create a temporary directory for uv's
  installer in <TMPDIR>`, where before, the piped installer needed none.
  Before, the installer was piped into `sh`, whose status for the empty input of a failed download
  is 0, so `wget` was never tried and the run ended in the uv trust remedy.
- Packed `.sh` runner, `--pwshpy-elevate`: the sudo relaunch runs as a child of the runner's shell
  and exits with its status. Before, it replaced the shell (`exec`), so under sudo's
  `timestamp_type=ppid` the relaunch found no cached credential and exited 1.
- Packed `.sh` runner, as root: a relative managed-Python directory is refused (`is not an absolute
  path`, exit code 127) instead of looping forever while the runner walks up its parents.

## [1.11.0] 2026-10-29 00:00:55

### Added

- `NativeCallError.hresult`: the failure's status as an unsigned 32-bit HRESULT when the native
  error carried one (a pywin32 COM or Win32 error, or a Windows `OSError` whose Win32 code is mapped
  as `HRESULT_FROM_WIN32`), else `None`. Branch on it rather than on the localized message, for
  example `exc.hresult == 0x80070002` for a missing file.
- `NativeAccessDeniedError`, exported from `pwshpy`: a native call refused for lack of rights
  (HRESULT `E_ACCESSDENIED` 0x80070005, POSIX `EACCES` or `EPERM`, psutil `AccessDenied`). It is a
  `NativeCallError`, so existing handlers still catch it, and a `PermissionError`, so
  `except PermissionError` catches it too. `errno` holds the POSIX code when the failure carried
  one, and survives pickling, so the error crosses a `multiprocessing` pipe unchanged.

### Changed

- A refused native call raised as `NativeAccessDeniedError` exits the `pwshpy` CLI as a
  `PermissionError` instead of 1: `pwshpy get_acl /opt/locked/file`, where the caller cannot enter
  `/opt/locked`, exits 13 on Linux. Such a refusal exits 13 on POSIX and 5 on Windows, whatever the
  failed call reported (`EACCES`, `EPERM`, a pywin32 or COM `E_ACCESSDENIED`, a psutil
  `AccessDenied`).
- A refusal pwshpy reads only from a reply or an exit status, not from an exception carrying a
  code, is still a plain `NativeCallError` and exits 1: a D-Bus error reply from systemd (a polkit
  refusal) or from the Secret Service, and a non-zero exit of a shadow-utils command (`useradd`,
  `usermod`, `gpasswd`, ...).
- `NativeAccessDeniedError` is an `OSError` (a `PermissionError`), so a caller's `except OSError:`
  or `except PermissionError:` around a pwshpy call now also catches a refused native call.
- A registry read or write whose denied or failed `winreg` call used to escape lib_registry as a
  raw `OSError` or `PermissionError` now raises `NativeCallError`, or `NativeAccessDeniedError`
  when access was denied.

### Fixed

- A registry failure carries its Win32 code as an HRESULT on `NativeCallError.hresult`, and a
  denied registry key raises `NativeAccessDeniedError`.
- A failed Windows Update search keeps its HRESULT across the worker process: the error raised in
  the caller has `.hresult` set, and an `E_ACCESSDENIED` search raises `NativeAccessDeniedError`.

## [1.10.0] 2026-10-27 08:35:45

### Fixed

- `--json` and `--jsonl` output, and `ps.write_records`, no longer fail on text that is not valid
  UTF-8. On POSIX, an environment variable, file name, process name or account name holding such a
  byte reaches Python as a lone surrogate, which the JSON encoder refused: `env --jsonl` exited 22,
  and the record holding the byte was never written. Each undecodable byte is now written as U+FFFD,
  and one warning on stderr names the record and field, for example
  `warning: EnvVar record 3, field value: not valid UTF-8; each undecodable byte written as U+FFFD`
  (`fields name, value` when several are affected). `ps.write_records` sends the warning to the
  module logger instead. Commands that print a plain value as JSON (`get_known_folder --json`,
  `run`, `cmdlet`, `get_ad_user`, `get_ad_group`, `get_ad_computer`, `get_mailbox`, `get_az_vm`,
  `get_az_resource_group`, `invoke_rest_method`) follow the same rule, and warn
  `warning: output field <path>: ...`, or `warning: output: ...` for a bare string such as
  `get_known_folder`'s path. The warning never quotes the value. Its field path is plain ASCII: a
  non-ASCII or non-printable character in a key is written as `?`. Two keys that differ only in
  undecodable bytes both survive, the later one renamed `<key>~2`, `<key>~3`, and so on. Clean
  output is byte-for-byte unchanged. Not covered yet: `config --format json`, which still fails on
  such a value.
- A choice pwshpy accepts in any case is shown in its documented spelling again. Under click 8.5,
  `get_known_folder bogus` listed `'applicationdata', ... 'mydocuments'`, and `--help` showed
  `[limited|highest]`. The bad-value message, the missing-value message, the `--help` metavar and
  shell completion now show `MyDocuments`, `Highest`, `InteractiveToken`, `StopExisting` and the
  rest as documented. This covers the `get_known_folder` folder, `register_scheduled_task`'s
  `--run-level`, `--logon-type` and `--multiple-instances`, `config --format` and
  `config-deploy --target`. Matching is unchanged, so a lowercase value is still accepted.
- One decoder now turns a pywin32 COM or Win32 error into its HRESULT. It replaces three private
  copies that disagreed:
  - A Win32 error code is reported as its HRESULT (`5` becomes `0x80070005`).
  - A `DISP_E_EXCEPTION` (0x80020009) is unwrapped to the code the called object reported, when it
    reported one.
  - Only an exception from `pywintypes` counts as a COM error. `struct.error`, `zlib.error` and
    `re.error` were taken for one, because only the class name was checked.
  - A failed Windows Update search now also reports a Win32 `pywintypes.error`. Before, only a
    `com_error` was turned into a message. A failure that carries no code says `without an HRESULT`
    instead of `HRESULT 0x00000000`.
  - A failed CIM query now names its code: `(HRESULT 0x...)`, or `(callee HRESULT 0x...)` when the
    code was unwrapped.
- An elevated (root) `.sh` pack run of a script with a git source now refuses a `git` older than
  2.32, exiting 127. Such a git ignores `GIT_CONFIG_GLOBAL`, so as root it would read the caller's
  git configuration. The runner asks the git that uv will run for its version, before uv is called
  at all, and refuses that git when it is too old, when its output does not parse, or when it fails.
  A git source is decided at pack time and shown by `--pwshpy-info`
  (`git source : yes (a root run needs git 2.32 or later)`, or `no`). The
  check over-detects on purpose: `git+` anywhere in the PEP 723 block or a `--with` value counts,
  and so do a `git` key, a `\u`/`\U`/`\x` escape, or a line-ending backslash. A git source the
  packer cannot see is not detected: one inside a local wheel or a path dependency's own metadata,
  or a build backend that runs git itself. An elevated `.ps1` run has no such check yet.
- The mutating test suites fail for the right reason. The systemd settings read-back checks
  `systemctl`'s exit code and the `ExecStart=` line, and the fixtures reset failed units. The
  scheduled-task tests take the task name from one constant, and every `ValidationError` test in
  `test_scheduling_domain.py` names the expected error.

## [1.9.0] 2026-10-26 22:23:51

### Added

- `new_shortcut(..., run_as_administrator=True)` / CLI `--run-as-administrator` (Windows): the
  shortcut starts its target elevated, with one UAC prompt and one window, like ticking "Run as
  administrator" in the shortcut's Advanced properties. pwshpy sets the link flag
  `SLDF_RUNAS_USER` (0x2000) through `IShellLinkDataList` before the file is saved.
  `ShortcutInfo.run_as_administrator` reports the flag for every shortcut `get_shortcut` reads,
  including one a pwsh script marked by patching byte 0x15 of the file. Only a bool is accepted;
  anything else raises `ConfigurationError` before anything is written. It defaults to False, so
  existing calls write the same shortcut as before.

### Fixed

- `logdemo` refuses a `LOG_*` variable lib_log_rich refuses (`LOG_CONSOLE_LEVEL=bogus`) with exit 78
  and lib_log_rich's own line, as `config` does. The demo reads the variables itself, so its
  `ValueError` reached `main()` and exited with the OS errno (22 on POSIX, 87 on Windows).
- An elevated `.ps1` pack run no longer trusts a network (UNC) path. A share's ACL is whatever its
  server reports, so a `uv` or a `PATH` directory on `\\server\share\...` passed the trust rule
  whenever the server claimed administrator ownership, while the server's operator controlled the
  content. Now `\\server\share\...` (either separator), `\\?\UNC\...` and `\\.\UNC\...` are refused
  for the `uv` search, a `-PwshPyUv` pin, the `PATH` handed to uv, uv's managed-Python directory and
  every directory above it, the Python uv finds there and the empty git configuration file, and so
  is a link met on the way to one of those paths whose target is one of them (only the walk of the
  managed-Python directory's contents judges a link it meets as the link it is and never follows
  it), named with `it is a network path, and an elevated run trusts only local paths`. Other
  device-namespace paths (`\\.\...`, `\\?\` other than `\\?\C:\...`) are refused as device paths. A
  drive letter mapped to a share is not detected. Elevated `.ps1` runs are unavailable while the
  running account's RoamingAppData is redirected to a share: the empty git configuration file and
  uv's managed Pythons live below it, so such a run stops with exit code 127, and its refusal names
  no remedy but says `the running account's AppData is on a network share; an elevated run cannot
  use it`. That is said only when the data directory itself is on a share; a link to a share below a
  local one is refused the same way, its reason naming the link and where it points, and keeps the
  usual remedy, since removing the link works. Runs that are not elevated are unchanged.
- The test suite no longer reads the developer's own user configuration layer. A new autouse fixture,
  `user_layer_in_tmp_path`, points the user layer (`XDG_CONFIG_HOME`, `LIB_LAYERED_CONFIG_MAC_HOME_ROOT`,
  `_APPDATA`, `_LOCALAPPDATA`) at an empty directory under `tmp_path`; `HOME` is left alone, because
  git and uv read it.
- The doctests under `src/` no longer run against the machine's configuration layers: a fixture in
  `tests/conftest.py` never reaches them. The two layer redirects now live in a rootdir `conftest.py`,
  their variable lists in `tests/layer_roots.py`; pyproject sets `testpaths` to include `src` and
  `tests` (which keeps the root `conftest.py` out of doctest collection), adds `tests` to `pythonpath`
  and pyright's `extraPaths`, and gives pyright an execution environment rooted at `tests/`. A test
  checks that every doctest collected from `src/` gets both redirects.
- An elevated or root pack run no longer lets uv run a `git` (or another tool) planted in a `PATH`
  directory another account can write. uv runs git for a git dependency by searching `PATH`, and it
  got the caller's whole `PATH`: measured, a root run reached with the caller's `PATH`
  (`sudo env PATH=...`) ran a planted `git` six times as uid 0, and in an already-elevated Windows
  session uv ran a planted `git.exe`. A Linux `--pwshpy-elevate` relaunch was not affected, because
  sudo's `secure_path` replaced the `PATH`. Both runners now give uv only the caller's `PATH`
  directories that pass the trust rule already applied to a `uv` (root alone on POSIX; on Windows
  the running account, SYSTEM, Administrators, LOCAL SERVICE, NETWORK SERVICE and TrustedInstaller)
  and name each one left out on stderr (`pwshpy: leaving <dir> off uv's PATH: ...`). When the
  caller's `PATH` has a `git` but none is left, one line (`pwshpy: no git on uv's PATH: ...`) names
  the cause: a git dependency then needs a `git` in a directory no other account can modify, such
  as a system-wide install (on POSIX one only root can modify). The packed script still gets the
  caller's whole `PATH`, behind the entry uv puts in front, as in an unelevated run; only an elevated
  `.ps1` run under Windows PowerShell 5.1 in which no `PATH` directory passes gives it the caller's
  `PATH` without uv's entry, because 5.1 deletes the empty variable that hands uv's `PATH` to the
  shim (pwsh 7 keeps it). An unelevated run is unchanged.


## [1.8.1] 2026-10-26 13:51:55

Never tagged or published to PyPI; these changes first ship in 1.9.0.

### Fixed

- `pwshpy logdemo` works with lib_log_rich 7, whose `logdemo()` takes a `ConsoleTheme`: the
  command passed the theme as a string and failed with `'str' object has no attribute 'value'`.
  `--theme` now offers the theme values (`classic`, `dark`, `neon`, `pastel`) and refuses any other
  with a usage error.
- A piped or redirected human table no longer fails on a value the console code page cannot hold:
  `pwshpy env | more` with a CJK value exited 22 on Linux under `PYTHONIOENCODING=cp1252` and 87 on
  German Windows after the work was done. The same crash hit the status lines naming a path
  (`wrote`, `created`, `copied`, `moved`, `removed`, ...), `config` and `config --format json`, and
  error lines turned such a value into backslash escapes. The CLI now switches a piped stdout and
  stderr to UTF-8 with LF line ends when it starts, so every piped line has the same bytes on every
  OS, as the JSON output already had. A terminal keeps its own encoding, and library use of `ps`
  leaves the host program's streams alone.
- `write_text` reads a piped stdin as UTF-8 on every OS instead of the locale code page. On German
  Windows it decoded as cp1252, so UTF-8 from pwsh 7 (whose `$OutputEncoding` is utf-8) or from
  pwshpy itself landed as mojibake, and some values failed the command outright (a
  `UnicodeDecodeError` for the UTF-8 of an I-acute: exit 87 on Windows, 22 on Linux under a cp1252
  `PYTHONIOENCODING`). A leading UTF-8 BOM in the input (Notepad, PowerShell 5.1 `Out-File`) is
  dropped, so it never lands in a file written without `--bom`. The new `--input-encoding NAME`
  option reads a piped source in another encoding; a name that is no text encoding (unknown, or
  `base64`) is a usage error, and bytes that are not valid in it stop the command with an error
  naming that option. A terminal stdin keeps its own encoding.
- `write_text` and `write_text_stream` (and the `write_text` command's `--encoding`) no longer write
  a BOM unasked for a codec spelled other than its canonical hyphenated name: `utf_16`, `utf16` or
  `utf_8_sig` wrote one BOM per streamed chunk without `bom=True`. Every spelling now maps to the
  BOM-less codec. Naming UTF-8 with `--input-encoding` drops a leading input BOM like the default.
- `write_text --encoding` with a name that is no text encoding (unknown, `base64`, `rot13`) is a
  usage error (exit 2) before PATH is opened. It used to fail with a raw `LookupError` after
  truncating PATH, and with an empty stdin it reported success. `write_text_stream` raises its
  `LookupError` before opening PATH too.
- `write_text_stream` (and so `write_text`) no longer writes one line end twice when a CRLF is split
  between two chunks: POSIX stdin does no newline translation, so a CRLF falling on the 65536-character
  block boundary became an extra blank line.
- A piped or redirected human table is drawn with an ASCII frame (`+`, `-`, `|`) on every OS. rich's
  box-drawing frames differ by OS (heavy on Linux, square on Windows) and turn into mojibake for a
  reader that decodes the pipe in a legacy code page. A terminal keeps its box-drawing frame. Linux
  pipes, which had the heavy box, now get the ASCII frame too.

- The test suite no longer reads the app and host configuration layers of the machine it runs on.
  An autouse fixture points lib_layered_config's three app-root variables (`LIB_LAYERED_CONFIG_ETC`,
  `_MAC_APP_ROOT`, `_PROGRAMDATA`) at an empty directory under `tmp_path`, so a system-wide deploy
  (for example a `/etc/xdg/<slug>/config.d/99-*.toml`) can no longer fail tests locally while CI stays
  green. The subprocess tests in `test_logging_dotenv_isolation.py` dropped every
  `LIB_LAYERED_CONFIG*` variable and so read the real system layers; they now keep the redirect.

### Changed

- The `using-pwsh` skill says how to run a one-shot job as a SYSTEM scheduled task: unregister it
  once it has started, stop and unregister it on a failure or Ctrl-C (never in `finally`), and that
  `Stop` ends the whole process tree. It adds a parse check to run before shipping a `.ps1` and the
  `"$t: done"` parse trap.

## [1.8.0] 2026-10-26 09:42:09

### Added

- `register_scheduled_task(..., settings=TaskSettings(...))` / CLI settings flags (Windows and
  Linux): set how a task behaves around batteries, a run that takes too long, a start while a run
  is active, and starts on demand. `TaskSettings` has six fields named after
  `New-ScheduledTaskSettingsSet`: `allow_start_if_on_batteries`, `stop_if_going_on_batteries`,
  `execution_time_limit` (a `timedelta` of whole seconds, `timedelta(0)` = no limit, at most
  2147483647 seconds), `multiple_instances` (`TaskMultipleInstances`: `PARALLEL`, `QUEUE`,
  `IGNORE_NEW`, `STOP_EXISTING`), `allow_hard_terminate` and `allow_demand_start`. Every field
  defaults to None, which writes nothing and keeps the OS default, so existing calls register the
  same task as before. CLI: `--allow-start-if-on-batteries` / `--disallow-start-if-on-batteries`,
  `--stop-if-going-on-batteries` / `--dont-stop-if-going-on-batteries`,
  `--execution-time-limit` (seconds or `HH:MM:SS`), `--multiple-instances`,
  `--allow-hard-terminate` / `--disallow-hard-terminate`, `--allow-demand-start` /
  `--disallow-demand-start`. On Windows they become the task's Task Scheduler settings; on Linux
  `ConditionACPower=true`, `TimeoutStartSec=`, `SendSIGKILL=no` and `RefuseManualStart=yes` in the
  `.service`. Linux raises `PlatformUnsupportedError`, before writing anything, for what systemd
  cannot do: stopping a run when the machine goes on battery, any `multiple_instances` but
  `IGNORE_NEW`, and `IGNORE_NEW` together with a daily trigger. `ScheduledTaskInfo.settings` reads
  the effective values back on both systems. With `allow_demand_start=False`,
  `start_scheduled_task` raises `NativeCallError` and only the triggers start the task.

## [1.7.0] 2026-10-24 23:57:13

### Added

- `ps.get_known_folder(folder)` / `pwshpy get_known_folder FOLDER [--json]` (portable): a
  well-known folder (`KnownFolder`: `DESKTOP`, `DOCUMENTS`, `LOCAL_APP_DATA`, `ROAMING_APP_DATA`,
  `PROGRAM_DATA`, `PROGRAMS`; the values are the .NET `SpecialFolder` names, `Desktop`,
  `MyDocuments`, ...) as the `Path` pwsh's `[Environment]::GetFolderPath` returns on the same OS:
  `SHGetKnownFolderPath` on Windows (a redirected Desktop included), the XDG user directories on
  Linux, the home's `Desktop` / `Documents` / `Library/Application Support` on macOS (spelled as
  Foundation standardizes them, so a leading `/private` whose remainder exists is dropped). A
  folder that is missing or not readable is `None` (CLI: exit 1 with a stderr line) where pwsh
  returns `""`. On Windows the three application-data folders fall back to `LOCALAPPDATA` /
  `APPDATA` / `ProgramData` like pwsh, but only to an existing directory, and any shell failure
  other than "not found" raises `NativeCallError`. `PROGRAMS` on Linux or macOS raises
  `PlatformUnsupportedError`.
- `ps.new_shortcut(path, *, target, arguments=(), working_directory=None, icon=None, icon_index=0,
  description=None, force=False)` / `pwshpy new_shortcut PATH --target T [-f] [-- ARGS...]` and
  `ps.get_shortcut(path)` / `pwshpy get_shortcut PATH` (Windows): write and read a shortcut
  (`.lnk`) through `IShellLinkW`, so non-ASCII text survives. Both return a `ShortcutInfo` record
  (what Windows stored; `target` with environment references expanded). `target` must be an
  absolute Windows path or start with `%NAME%\`; `arguments` is an argv sequence stored as the
  command line `subprocess.list2cmdline` builds (a bare `str` is refused). Values Windows would
  silently cut or rename raise `ConfigurationError` before anything is written; an existing file
  needs `force=True`. A target whose environment reference expands past 259 characters is refused
  by Windows with `NativeCallError` (0x80004005) and nothing is written. Off Windows both raise
  `PlatformUnsupportedError`.
- `get_windows_update` records carry `category_details`: one `UpdateCategory` (`category_id`, the
  lower-case GUID; `type`, such as `UpdateClassification` or `Product`; `name`) per category, so a
  caller can select Security or Definition updates without matching a localized name. The
  `categories` name list is unchanged; the classification GUIDs are listed in `docs/power-tools.md`.
- `get_scheduled_task(task_name=...)` / `--task-name` looks up one task without walking the folder
  tree; an unreadable task elsewhere no longer affects it. On Linux it also finds an on-demand
  (trigger-less) task pwshpy registered, which the listing leaves out. A task or folder that does
  not exist is an empty result (Windows: HRESULT 0x80070002 from opening the folder or the task),
  under `strict=True` too; any other failure, and any failure reading a task that was found, keeps
  the listing's rule (a WARNING, or `NativeCallError` under `strict`). On Windows `folder_path` may
  end in a backslash, the form a record's `task_path` carries, the name matches without regard to
  case, and the record carries the caller's spelling (the scheduler echoes it). On Linux the answer
  is the task's `.timer`, else its `.service` when pwshpy wrote it (the unit file carries
  `X-PwshPy=` in `[Unit]`; a system daemon such as `ssh` is no task), else nothing; a unit file
  whose marker cannot be read keeps the listing's rule. A masked unit counts as absent unless it is
  running, and an alias as absent, as in the listing; a `.timer`/`.service` suffix is optional. An
  empty name or a folder part (Windows: a backslash; Linux: `/`, or a backslash that starts no
  systemd `\xNN` escape), or on Linux an invalid unit name, raises `ConfigurationError`.

### Changed

- `exec(..., wait_tree=True)` on POSIX: a status the process-tree helper wrote in a shape it never
  writes (a field of the wrong type, a document that is not an object) now raises
  `NativeCallError("... ended without a status: {}")` instead of a bare `TypeError` or
  `ValueError`. Every status the helper does write, and every refusal of a bad request, is reported
  as before.
- `get_acl` / `add_acl_ace` / `remove_acl_ace` on Linux: an ACL entry with a tag the kernel does not
  define (the kernel validates ACL xattrs, so no real file carries one) still raises `KeyError` at
  the same point, but its text is now the tag's enum repr (`<_AclTag.UNKNOWN_0x40: 64>`) instead of
  the bare number (`64`).
- When piped or redirected, `exec` (the program's stdout and stderr), `invoke_web_request` (the
  body), `get_content` (the lines) and `download_file` (its `downloaded URL -> DEST` line) write
  UTF-8 bytes, like `--json`. A character the console code page cannot hold no longer fails the
  command with `UnicodeEncodeError` after the work was done. Piped `exec` output has LF line ends
  and keeps ANSI sequences. On Windows, piped `get_content` lines and the `download_file` line
  also change from CRLF to LF, and the `invoke_web_request` body is written byte-exact (the old
  text layer turned `\r\n` into `\r\r\n`). At a terminal nothing changes.
- An elevated packed run also drops the caller's `GCM_*` (Git Credential Manager) variables and
  `PASSWORD_STORE_DIR`, in both runners, like the `GIT_*` variables.
- An elevated Windows run points `GIT_CONFIG_GLOBAL` at an empty file of its own
  (`<data home>\pwshpy\empty.gitconfig`, judged by the same trust rule as uv) instead of `NUL`,
  which some git builds read as a file in the current directory. A file that is not empty, a link
  or writable by another account is refused with exit 127. POSIX keeps `/dev/null`.
- `get_windows_image` documents the measured ESD support: the two `install.esd` files measured from
  Microsoft's catalog (2026-10-23, build 26100.4349 en-us, x64 and arm64) store their XML resource
  uncompressed and are read like a `.wim`; the CLI was run on the x64 one, and other ESD files are
  unverified. An image file whose XML resource is compressed is still refused. An image with no
  `WINDOWS` element in its XML stays `Unknown` with an empty version, where `Get-WindowsImage`
  shows architecture 0 (x86) and `0.0.0.0` (documented in `docs/cli-reference.md`).

### Fixed

- Windows `start_service`, `stop_service` and `restart_service` wait for a service that is still
  starting or stopping to settle before they send the control (within the same `timeout`), instead
  of failing with error 1052, 1056 or 1061 after `stop_service(wait=False)`.
- `run_bounded` (used by `get_windows_update`) called inside a daemonic `multiprocessing` worker
  raises `NativeCallError` saying why, instead of a bare `AssertionError`; under `python -O`, where
  that assert is stripped, it is now refused too instead of starting a child the worker cannot
  reap.
- The packer's `requires-python` scan of a PEP 723 block skips the contents of TOML multi-line
  strings, so a `[` line inside one no longer ends the scan early.

## [1.6.0] 2026-10-23 12:21:55

### Added

- `get_windows_image PATH [--index N]` (`ps.get_windows_image`): the images in a `.wim` or split
  `.swm` file, read from its header and XML on any OS without DISM (record `WindowsImage`, enum
  `ImageArchitecture`). A compressed XML resource is refused; ESD files are not verified yet.
- `get_windows_update [--criteria C] [--timeout S]` (`ps.get_windows_update`, Windows): a Windows Update
  search in a child process that is killed after the timeout (record `WindowsUpdate`). A script that
  calls it needs an `if __name__ == "__main__":` guard.
- `get_current_identity` (`ps.get_current_identity`, like `whoami /user`): the account the process
  runs as, read from its token, never from `USERNAME` (record `CurrentIdentity`, enum
  `IntegrityLevel`).
- `register_scheduled_task` takes a principal (`TaskPrincipal`: a SID on Windows, a uid on Linux), a
  run level and once/daily/boot/logon triggers, on Task Scheduler and on systemd; CLI `--run-as`,
  `--run-level`, `--logon-type`, `--at`, `--daily`, `--every-days`, `--at-boot`, `--boot-delay`,
  `--at-logon`, `--logon-user`. `ScheduledTaskInfo` reads back `user_id`, `run_level`,
  `last_run_time`, `last_task_result` and `next_run_time`.
- `get_scheduled_task --strict` and `get_service --strict` raise `NativeCallError` for an item the
  caller cannot read; without it, each skipped item is logged as a WARNING.
- `stop_service --no-wait` (`wait=False`) issues the stop and returns at once.
- `exec --wait-tree` (`wait_tree=True`) waits, like `Start-Process -Wait`, until every process the
  program started has exited, and `--timeout` kills the whole tree.
- `OperationTimeoutError`, a `NativeCallError` subclass with `.timeout`.
- Packed runners take `-PwshPyUv PATH` / `--pwshpy-uv PATH` to pin the `uv` they run.
- An exit-code table in `docs/cli-reference.md`.

### Changed

- Every `OperationTimeoutError` exits 110 on the CLI. It is now raised by the service start, stop and
  restart waits (on Linux they returned silently), by `exec --timeout` (exit 1 before), by
  `wait_process --timeout` and `resolve_dns_name --timeout` (exit 1 before: a script checking for 1
  there must check for 110), and by the Linux local-account commands after 30 s. The .NET and web
  `--timeout` still exit 1.
- `exec --timeout` accepts `(0, 2147483]` seconds; anything else raises `ValueError` before the
  program starts.
- A refused elevation exits 77 (`pwshpy --elevate`, `-PwshPyElevate`, `--pwshpy-elevate`); an elevated
  `.ps1` child that fails before the script runs relays its error and exits 70.
- `--json` / `--jsonl` are written as UTF-8 with LF line ends on every OS, whatever the console code
  page, and `--json` writes nothing until the first record exists.
- A piped or redirected table is as wide as its content (up to 8192 columns) and prints cells
  literally, never as rich markup.
- Linux `start_service` / `restart_service` raise `NativeCallError` naming systemd's `Service.Result`
  when the unit fails; they returned `Stopped`. A `Type=oneshot` unit returns `Stopped` once its start
  job succeeded.
- Linux `get_service` and `get_scheduled_task` list stopped and unloaded units.
- Linux scheduled tasks: every unit pwshpy writes carries `X-PwshPy=<version>`. `register` refuses to
  overwrite or shadow a unit without it, and `unregister` touches only marked units ("not written by
  pwshpy"). Units written by pwshpy 1.0 to 1.5 carry no marker: remove their files under
  `/etc/systemd/system` by hand, or add `X-PwshPy=` to their `[Unit]` section.
- Linux scheduled tasks: names systemd would not accept are refused before anything is written.
  run/stop/enable/disable take a listed instance or escaped name such as
  `zfs-scrub-monthly@my\x2dpool` (1.5.2 sent it to `x2dpool`); a path starting with a backslash is the
  Windows `\Folder\Name` form. A program starting with an `ExecStart=` prefix (`@ - : + ! |`), a lone
  `;`, and any control character in the program, arguments or description raise `ConfigurationError`.
- `enable_scheduled_task` on a boot timer only enables it.
- Windows event-log messages are rendered through the provider's publisher metadata, as
  `Get-WinEvent` does.
- The `[full]` extra pins pythonnet per Python: `>=3.1.0` on 3.10, `>=3.2.1` from 3.11.

### Security

Elevated packed runs (administrator, SYSTEM, root) no longer let another account, or the caller's
environment, choose code that runs with those rights. Details in `docs/power-tools.md`.

- Only a `uv` that no other account can modify runs, judged on the file and every directory above it;
  as root the `.sh` runner takes its helpers from the system directories only.
- Only a uv-managed Python from the running account's own data directory runs. That directory and
  everything below it is checked before uv searches it, the Python is named to `uv run --python`, and
  no `.venv` in or above the starting directory is run. The first elevated run on a machine without
  one installs a CPython build (about 36 MiB), so it fails offline when none is installed. The check
  costs an elevated `.ps1` run about 2 to 3.5 s per installed Python on Windows.
- uv starts in an admin-only directory and reads no `uv.toml` (`--no-config`) and no `.env`
  (`--no-env-file`); its cache is named from the account.
- The caller's interpreter, library, package-index, cache, proxy, certificate, keyring and
  Python-mirror variables are dropped, one `pwshpy: ignoring <NAME>` stderr line each, and the user's
  site-packages is off. In a network reachable only through a proxy, an elevated run therefore
  downloads nothing.
- Every `GIT_*` variable and `SSH_ASKPASS` is dropped, and `GIT_CONFIG_GLOBAL` is set to `/dev/null`
  (`NUL` on Windows), so git 2.32 or later reads no global config. The packed script inherits that: a
  git it runs no longer sees `user.name` or a `credential.helper` from a global `.gitconfig`; pass them
  with `git -c ...`.
- Native-call arguments in the `.ps1` reach their program literally (pwsh on Linux and macOS no
  longer globs them).

### Fixed

- Entry names and `--with` values holding quotes, `$`, a backtick or a space pack and run in both
  runners; a name with a line break is refused at pack time.
- Every `PwshPyError` survives pickle and deepcopy.
- A refused UAC prompt is recognised by its error code on every locale.
- `get_windows_image` with an index the file does not hold raises `NativeCallError`.
- WARNING lines that look like rich markup print literally (lib_log_rich >= 6.5.7).
- `enable_scheduled_task` / `disable_scheduled_task` on a static systemd timer raise
  `NativeCallError`; they changed nothing silently.
- Windows: a failing status query during a service wait raises `NativeCallError` instead of a raw
  `pywintypes.error`, and `exec(wait_tree=True)` no longer hangs on a program with no thread.
- Tests never touch the desktop keyring unless `PWSHPY_TEST_SECRET_SERVICE` is set in the process
  environment.

### Known limitations

- On Windows, `get_process` names two pseudo-processes as psutil does: `MemCompression` (Get-Process:
  `Memory Compression`) and `System Idle Process` (Get-Process: `Idle`).
- `exec` drops the output a program wrote before a timeout.
- A git older than 2.32 ignores `GIT_CONFIG_GLOBAL` and still reads the global config in an elevated
  run.

## [1.5.2] 2026-10-19 14:48:04

### Fixed

- `.gitignore`: four patterns (`*$py.class`, `*.egg-info/`, `*.manifest`, `*.spec`) carried an inline
  comment, which gitignore reads as part of the pattern, so they ignored nothing. Each comment now has
  its own line and the patterns apply.

## [1.5.1] 2026-10-18 22:49:59

### Documentation

- The `using-pwsh` skill lists six `.ps1` traps for when a script is still the deliverable, each with
  the shell it occurs in (Windows PowerShell 5.1, pwsh 7) and what to do instead: an advanced script
  binding a forwarded `-v`, a function returning its narration with its value, the `curl` alias, native
  stderr as error records under `Stop`, `pwsh -File` splitting `--out=C:\data` at the colon, and an
  unguarded read probe before a lock.

## [1.5.0] 2026-10-18 11:40:29

### Fixed

- `ps.elevate()` on Linux and macOS tells a refused sudo password from the command's own exit code. It
  ran `sudo <command>` and returned sudo's exit code, but sudo exits 1 when authentication fails, so a
  wrong password read as the command having exited 1, and a Ctrl+C at the prompt escaped as
  `KeyboardInterrupt`. It now asks with `sudo -v` first and raises `ElevationDeclinedError` when sudo
  does not grant root or the prompt is interrupted, as a declined UAC prompt does on Windows, and only
  then runs the command. A Ctrl+C once the command runs interrupts as before.

## [1.4.0] 2026-10-16 23:24:25

### Added

- `ElevationDeclinedError`, a `NativeCallError` subclass, raised by `ps.elevate()` when the person
  refuses the Administrator prompt (Windows error 1223). A caller can now carry on unelevated after a
  refusal and still report a real failure as one; code catching `NativeCallError` is unaffected.

### Fixed

- `ps.elevate()` relaunches a console script installed by uv on Windows. Its launcher reports
  `sys.argv[0]` without `.exe`, so the relaunch fell through to `python <argv0>`, which cannot open
  that path; the `.exe` beside it is now what runs.
- `ps.elevate()` passes `SEE_MASK_FLAG_NO_UI` to `ShellExecuteEx`. Without it a request Windows could
  not satisfy waited on an error box, and where no desktop shows one the call never returned; it now
  fails at once with the Windows error. The real `ShellExecuteEx` call now runs in the Windows test
  lane, where before every test replaced it.

## [1.3.3] 2026-10-15 17:16:31

### Fixed

- The source distribution ships only the package, its tests, its docs, `README.md`, `LICENSE`,
  `CHANGELOG.md` and `pyproject.toml` (an include list). Earlier sdists also carried every other
  root file and, in a local build, anything hidden only through `.git/info/exclude` or a nested
  `.gitignore`. `tests/test_packaging.py` builds the sdist and wheel and checks both; `hatchling`
  joins the dev extra for it.

### Changed

- Ruff selects `C90` (mccabe), so a function over cyclomatic complexity 10, the house hard limit,
  now fails `make test`; before, nothing enforced it.

### Tests

- The `os_windows`, `os_macos`, `os_linux` and `os_posix` markers now skip a test off its platform
  (`pytest_runtest_setup` in `tests/conftest.py`); registered in `pyproject.toml` alone they skipped
  nothing, so a test trusting its marker ran everywhere. `tests/test_platform_markers.py` pins it.

## [1.3.2] 2026-10-15 13:23:00

### Added
- Python 3.15 is a supported version: declared in the classifiers, so CI and `make test-all` run it.
- `uv.lock` is committed, and `[tool.uv] exclude-newer` sets a cooldown that keeps releases younger
  than it out of the lock. CI installs exactly the locked tree on push and pull request; the nightly
  scheduled run still resolves afresh to catch upstream breakage early.

### Changed
- The install section explains why installing with uv protects against supply-chain attacks and why
  a plain `pip install` from PyPI does not.

### Fixed
- **Two cold starts of a packed `.sh` runner no longer extract at the same time.** The extraction
  lock was `mkdir "$lock"`, and the uutils coreutils `mkdir` that Ubuntu 26.04 ships lets two
  racing calls on one path both succeed (measured: 660 of 2000 races; GNU mkdir: 0), so one start
  died with "the embedded payload is corrupt" or a tar "File exists". The lock is now a file the
  shell itself creates under noclobber (O_EXCL). Runners packed by earlier versions keep the old
  lock until they are packed again.
- A packed `.sh` runner that fails while holding the extraction lock (a corrupt payload, a failing
  `tar`) now releases it; before, every later start waited ten minutes for it.

## [1.3.1] 2026-10-12 19:21:08

### Fixed
- **A digit-only `.env` value no longer stops logging setup with `Input should be a valid string`.**
  lib_layered_config 7.1 reads an unquoted `.env` or environment value that spells a number
  losslessly as an int, so `LIB_LOG_RICH__SERVICE=4711` reached a text setting as a number and was
  refused. The `[lib_log_rich]` section now turns such a number back into the text that was
  written; numeric settings still read as numbers and a value that is not text is still refused.

## [1.3.0] 2026-10-08 10:36:51

### Changed
- **Requires lib_layered_config 7.0.1.** An unquoted `.env` value now converts like the
  environment layer, so `SECTION__FLAG=false` arrives as the boolean `false` instead of the
  (truthy) string `"false"`; a quoted value stays text. `defaultconfig.toml`'s header now says so,
  and lists the real per-OS config locations (its macOS lines still named the template's
  directory) with ASCII arrows.
- **`click` and `rich` are declared dependencies.** The package imports both directly (`main.py`,
  `commands/config.py`, `output.py`, `config/display.py`) but installed them only because
  rich-click pulls them in. A test scans the package's run-time imports (outside
  `TYPE_CHECKING`) and fails on any third-party module whose distribution is neither a dependency
  nor in a runtime extra.

### Fixed
- **`build_testing()` can run a command.** The in-memory logging initializer was a no-op while every
  command that logs binds job context onto the lib_log_rich runtime, so `info`, `config`,
  `get_uptime` and the other binding commands under the testing composition raised
  `RuntimeError('lib_log_rich.init() must be called before using the logging API')`. It now starts
  a quiet runtime (no journald, event log, Graylog or queue; console at ERROR; no `.env` loading).
- **Tests no longer pass or fail by order.** An autouse fixture shuts the lib_log_rich runtime down
  and restores the root logger's handlers, level and propagate flag after every test, and another
  pins rich-click's colour and width globals, so CI (GITHUB_ACTIONS colours the output, Windows
  runners are 79 columns wide) renders the same plain text as a developer machine. The conftest
  service fixtures use the quiet runtime, so a stderr assertion through them no longer races
  production logging's queued INFO lines.
- **Exit code change: commands that exit with a code now really do.** `main()` discarded the code
  rich_click's `main()` returns for a `ctx.exit(N)` and always returned 0, so through the `pwshpy`
  console script and `python -m pwshpy`, `test_path` of a missing path, `is-elevated` when not
  elevated, `exec` of a failing program and `--elevate` of a failing child all exited 0 although
  they document a non-zero code. They now exit 1, 1, the program's own code and the child's code.
  Scripts that relied on the old, always-0 exit status will see the documented one.
- **No more `SystemExit: N` on stderr.** The `config`, `config-deploy` and
  `config-generate-examples` errors raised a bare `SystemExit`, which `main()`'s catch-all branch
  printed as `SystemExit: 22` (or 13, 1) after the real error message. They now exit through
  click's context. `config-deploy` re-raises a click `Exit` before its catch-all, so a deliberate
  exit inside the deploy keeps its own code instead of becoming 1. `typed_click` gains a typed
  `get_current_context` wrapper for the helpers that have no `ctx` parameter.
- **Exit code change: a configuration that does not load no longer stops every command.** The root
  group loaded the layered configuration before any subcommand ran and let a load error escape, so
  one malformed `config.toml`, an unreadable file or a non-UTF-8 `--env-file` made every command
  fail (exit 1 with only a `LayerLoadError` line for a malformed file) - `info`, `--help`,
  `get_process`, `test_path` and the
  other native commands, and `config-deploy`, the command that replaces the broken file. The
  failure is now recorded: `config` (the one command that reads the configuration, also with its
  own `--profile`) exits 78 with one `Error:` line naming the file, `--traceback` adds the loader's
  traceback, `config-deploy` warns which failure it skipped and deploys, and every other command
  runs as before. `config --profile X` now keeps the root's `--env-file` instead of falling back
  to the upward `.env` search. A loader exception that is not a configuration failure still
  propagates as the bug it is.
- **Exit code change: command-line mistakes are usage errors (exit 2) for every command.** An
  invalid `--profile` name exited 22 with `ValueError: profile contains invalid characters`,
  conflicting overrides such as `--set a.b=1 --set a.b.c=2` exited 22 with a `TypeError` (the other
  order silently dropped the earlier one), and `config-deploy --profile ../x` exited 1 as "Failed
  to deploy configuration". All three are now checked before the configuration loads, so a broken
  file cannot hide them, and exit 2 naming the problem. The same key given twice still takes the
  last value. `overrides.nest_overrides` is the new function that parses and checks the `--set`
  values together.
- **Exit code change: `config-deploy` leaves every permission decision to lib_layered_config.**
  The command read `[lib_layered_config.default_permissions]` itself from the application's own
  configuration, so a `.env` in the working directory (or one found above it) decided the deployed
  modes: a `..._DEFAULT_PERMISSIONS__ENABLED=false` line there turned permission setting off, and
  a bare integer mode such as `444` from the environment was deployed as the decimal number it is
  (`0o674`, group-writable). The deploy now hands its command line
  (`--permissions`/`--no-permissions` as True/False/None, `--dir-mode`/`--file-mode` and any
  `--set` of that section) to ONE `deploy_config` call; the library reads the section from the
  bundled defaults, the files the deploy does not overwrite and the environment, never from
  `.env`, so a `.env` can neither change nor block a deploy and `--force` replaces a destination
  carrying a bad value. A refused setting exits 78 with one `Error:` line per problem naming the
  key and its source, plus a hint in the CLI's spelling when both mode options would get past it;
  `--set ...default_permissions=5` (not a table) is refused before the call. Before, such a
  setting never stopped the deploy: it exited 0 having deployed the value as read
  (`PWSHPY___LIB_LAYERED_CONFIG__DEFAULT_PERMISSIONS__USER_FILE=444` gave the file mode `0o674`)
  or a default in its place; it now exits 78 and writes nothing. The deploy port, the adapter and
  the in-memory double take `set_permissions: bool | None` (default None) and
  `permission_overrides`; `adapters/config/permissions.py` is removed.
- **Exit code change: an unsafe or malformed `--dir-mode`/`--file-mode` is a usage error (exit 2).**
  The options were parsed with a bare `int(value, 8)`, which accepts `-1`, `7_5_0` or fullwidth
  digits, and an unsafe mode such as `777` was only refused inside the deploy as "Failed to deploy
  configuration" (exit 1). They now go through lib_layered_config's `DeployMode` when the option is
  parsed, the same rule it applies to configured modes: a plain octal literal within
  0..0o7777, no setuid/setgid/sticky bit, no group or world write, no execute bit on a file, owner
  `rwx`/`rw` kept. `--no-permissions` together with a mode option is a usage error too.
- **Logging takes only `LOG_*` lines from a `.env`.** `init_logging` called lib_log_rich's
  `enable_dotenv()`, which copied every line of the nearest `.env` into the process environment,
  so a later configuration load (`config --profile`, the deploy's permission read) took an
  app-prefixed `.env` line (`PWSHPY___...`) for the environment layer, even under `--env-file`.
  Logging now copies only `LOG_*` lines, never over a variable already set, from the `--env-file`
  when given, otherwise from the nearest `.env` up to the project root, without `chdir` and passing
  over unreadable directories; a `.env` that is not UTF-8 no longer stops logging. `python-dotenv`
  is declared, and the `InitLogging` port takes `dotenv_path`.
- **Exit code change: an invalid `[lib_log_rich]` value no longer stops every command.** A value
  lib_log_rich refuses (a wrong type such as `rate_limit = "100:60"`, or its own range checks such
  as `queue_maxsize = 0`) exited 22 with pydantic's report from every command, `config-deploy`
  included. It is now recorded like a load failure: logging starts with its defaults, `config`
  exits 78 with one `Error:` line per problem naming the key (never the value), and the other
  commands run. The root group types the services factory instead of ignoring the type.
- **Exit code change: a refused `LOG_*` variable no longer disables every command.** After a
  refused logging setting the CLI started logging again with an empty configuration, but
  lib_log_rich reads every `LOG_*` variable from the environment on each start, so when the refused
  setting was one of them (`LOG_CONSOLE_LEVEL=bogus` in the environment or in the `--env-file`) it
  was refused a second time, uncaught, and every command - `info`, `config-deploy` and the native
  commands included - exited 1 with `InvalidLoggingConfigError: lib_log_rich: Unknown log level:
  'bogus'`. Logging now falls back to its defaults, first with the `LOG_*` variables and, only when
  that start is refused too, with every `LOG_*` variable hidden for that start (and put back
  afterwards). Only `config` exits 78; the other commands run and exit 0. The 78 carries
  lib_log_rich's own message, `Error: lib_log_rich: Unknown log level: 'bogus'`, which may name
  neither the variable nor where it was set. A refused `[lib_log_rich]` value still leaves every
  valid `LOG_*` variable in force for the fallback: only a refused variable hides them.
- **Documented list and table syntax that actually works in `.env` and the environment.**
  `.env.example` and `defaultconfig.d/90-logging.toml` showed comma-separated `LEVEL=style` and
  `field=regex` pairs for `console_styles` and `scrub_patterns` and `"host:port"` / `"100:60"`
  strings for `graylog_endpoint` and `rate_limit`. Each arrives as ONE string, which lib_log_rich
  refuses, so following the documentation stopped logging. They now show a JSON object or array
  (unquoted in a `.env`, shell-quoted in the environment) or one key per entry
  (`LIB_LOG_RICH__SCRUB_PATTERNS__API_KEY=.+`), and say how an unquoted value converts.
- **`config-deploy --force` says what lib_layered_config 7 does.** With nothing to write (every
  target file already identical) a forced deploy told the user to "Use --force"; it now says that
  every file is already identical. The `--force` help, the command help and CONFIG.md say that a
  differing file is replaced and kept as `<name>.bak`, and the command help no longer prints the
  literal `\b` markers rich-click does not interpret.
- **`--help` no longer prints Python code.** Every command's help text (the root group, `config`,
  `config-deploy`, `config-generate-examples` and all native commands) ran on into the docstring's
  developer `Example:` section, so `pwshpy config-deploy --help` ended with
  `>>> from click.testing import CliRunner` and the lines after it. Each docstring now stops the
  help text before that section, and a test checks the help of every registered command.

## [1.2.1] 2026-07-30 18:11:54

### Changed

- **The shipped skill's plugin version now tracks the package version.** bmk 3.14.0 raises
  `.claude-plugin/plugin.json` to the package version on bump, push and release, and never lowers
  it. An install re-fetches a skill only when that version changes, so the two numbers drifting
  apart meant a skill edit could ship to nobody. No functional change to the library.

## [1.2.0] 2026-07-24 14:48:10

### Added
- **Script packer: ship a Python tool as one self-extracting POSIX `.sh`, alongside the `.ps1`.**
  `pwshpy pack ENTRY -o tool.sh` (and `PackOptions(format=RunnerFormat.SH)` / `--format sh`) emits a
  strict-POSIX shell runner that runs under any `/bin/sh` - dash, busybox ash, macOS `sh`, bash -
  not only bash, and independent of the login shell. It carries a base64 `tar` payload, unpacks into
  a per-user cache with a `mkdir` lock, installs `uv` if the machine has none, runs the script and
  returns its exit code, with no argument shim because POSIX `sh` forwards `"$@"` intact. The format
  auto-detects from the `-o` suffix (`RunnerFormat.AUTO`); `--format ps1|sh` forces it. New public
  API: `RunnerFormat`, `PackOptions.format`. Pack and unpack are pure Python, so a `.sh` builds on
  Windows and a `.ps1` on Linux, and `pwshpy unpack` reads either format on either OS (it sniffs zip
  vs tar). The artefact's own switches are spelled `--pwshpy-help` / `--pwshpy-info` /
  `--pwshpy-clean` / `--pwshpy-no-install-uv` / `--pwshpy-elevate` on the `.sh`.

### Fixed
- The four Windows-only mutating controller tests (ACL, event log, local account, scheduled task)
  now carry `skipif(sys.platform != "win32")`, so they skip cleanly off Windows under
  `make test-all` (which does not exclude the `mutating` marker) instead of erroring.

## [1.1.0] 2026-07-24 12:48:10

### Added
- **Script packer: ship a Python tool as one self-extracting `.ps1`.** `pwshpy pack ENTRY`
  (and `ps.pack_script(...)`) embeds an entry script and every local module it imports into a
  single PowerShell file that unpacks itself into a per-user cache, installs `uv` if the machine
  has none, runs the script, and exits with the script's own exit code - on Windows PowerShell 5.1
  and pwsh 7 alike, on a machine with no Python. `pwshpy unpack` (and `ps.unpack_script(...)`)
  restores the sources to edit and repack. Third-party dependencies come from the entry's PEP 723
  block or `--with`, never guessed from an import name. New public API: `PackOptions`,
  `PackedScript`, `PackError`. Arguments (including empty strings, quotes, and unicode) and exit
  codes round-trip intact; the artefact carries `-PwshPyHelp` / `-PwshPyInfo` / `-PwshPyClean` /
  `-PwshPyNoInstallUv` / `-PwshPyElevate` switches, verifies its cache against per-file hashes
  before running, and serializes concurrent cold-start extraction with a lock.

## [1.0.2] 2026-07-19 22:30:08

### Fixed
- **`config-deploy` no longer crashes on a legacy Windows console codepage (cp1252).** It printed a
  Unicode checkmark per deployed path, which raised `UnicodeEncodeError` and exited 1 even though the
  files had already been written, misreporting a successful deploy as a failure. It now prints a
  plain ASCII marker.

## [1.0.1] 2026-07-15 15:35:59

### Added
- `pwshpy.adapters.powershell.is_runtime_available()` - reports whether .NET can actually
  start, i.e. all three requirements are met (the `[full]` extra AND the .NET 10 runtime
  AND the PowerShell 7.6 SDK). `is_available()` answers only the first of those, and is
  unchanged; a guard that needs to know whether a call would succeed should ask the new
  one. It delegates to the real init rather than re-deriving its preconditions, so it
  cannot drift from what a call actually does, and it still short-circuits on a spec
  lookup when the extra is absent.

### Fixed
- The .NET test guards asked `is_available()` ("is pythonnet importable") while their own
  docstrings promised "extra AND runtime AND SDK". On a machine with `[full]` installed but
  no .NET runtime the guard stayed True, so 16 tests ran and failed with
  `FeatureUnavailableError` instead of skipping. They now guard on `is_runtime_available()`
  and skip cleanly. Nothing about the shipped error path changed: a real call there already
  raised `FeatureUnavailableError` naming the missing .NET 10 runtime, which is correct.
- `ps.get_config(profile=...)` and the CLI's `--profile` raise the documented `ValueError`
  again for an invalid profile name. A newer `lib_layered_config` raises its own
  `ValidationError`, which is not a `ValueError`, so every caller's `except ValueError`
  guard silently stopped catching invalid profiles. The dependency's exception is now
  normalised back to the documented contract, with a fallback for older versions that
  have no such type.

### Documentation
- Corrected names that do not exist, and would fail for anyone who copied them: the registry
  commands are `get_item_property` / `registry_keys` (not `registry values` / `registry keys`),
  the facade method is `ps.get_acl()` (not `ps.acl()`) and `ps.get_service()` (not
  `ps.services()`), and `get_ad_user` has no `--jsonl` flag - it emits JSON only. Verified
  against the CLI, which rejects `--jsonl` with "No such option".
- `backends-and-platforms.md` and `powershell-switch-mapping.md` described services, event log,
  scheduled tasks, local accounts and ACLs as "Windows-only" or "future" work. They are portable
  and have been for some time - the facade dispatches each to a Linux backend (systemd D-Bus,
  journald, systemd timers, pwd/grp, POSIX ACL xattr) - as `portability-roadmap.md` already
  recorded and the README already said. Only the registry, CIM/WMI and hotfixes are Windows-only,
  deliberately: they have no honest Linux analog. The `mutating.py` module docstring claimed all
  its commands were Windows-only when most of them dispatch by OS.

### Changed
- `build` is floored at `>=1.5.0` rather than `>=1.5.1`. PyPI yanked 1.5.1 (upstream
  shipped unintended breaking changes), and a yanked release is invisible to a range
  resolve, so a `>=1.5.1` floor had zero candidates and made the whole `[dev]` extra
  unresolvable.
- Dependency floors raised: `lib_layered_config>=5.6.0`, `hypothesis>=6.156.6`,
  `httpx2>=2.7.0`, `virtualenv>=21.6.1`.

### Security
- `click` is floored at `>=8.4.2`, clearing CVE-2026-7246 (command injection in
  `click.edit()`). It was pinned back to the vulnerable 8.2.1 by `codecov-cli`, which
  requires `click<8.3.0`; `codecov-cli` is therefore commented out rather than deleted,
  with the reason and the re-enable condition recorded inline. Coverage upload is
  unaffected - CI uses `codecov/codecov-action`, not the CLI.
- `[tool.pip-audit].ignore-vulns` is now empty. All four entries (py, pip x2, pillow) were
  verified inert - the packages are either not installed or long since fixed, and the audit
  is scoped to this project's dependency tree, so environment-only packages are not audited
  at all. The project now suppresses no vulnerability findings.

## [1.0.0] 2026-07-10

### Added
- Initial release.
