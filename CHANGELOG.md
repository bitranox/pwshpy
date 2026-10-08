# Changelog

All notable changes to this project will be documented in this file following
the [Keep a Changelog](https://keepachangelog.com/) format.


## [Unreleased]

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
