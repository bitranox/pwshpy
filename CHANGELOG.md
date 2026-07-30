# Changelog

All notable changes to this project will be documented in this file following
the [Keep a Changelog](https://keepachangelog.com/) format.


## [Unreleased]

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
