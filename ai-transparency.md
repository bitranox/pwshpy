# AI transparency

The author and owner of this project is the human, [@bitranox](https://github.com/bitranox).
Every design and engineering decision is theirs, and they answer for everything published here.
An AI assistant (Claude, run through the Claude Code CLI) was used as a tool along the way, mostly
for the typing and the legwork under that direction. This page says where, plainly, so you can weigh
the work on its merits. The reasoning behind working this way is in [ai-stance.md](ai-stance.md).

## The human's work

The shape of this software is the human's, start to finish. They set the problem, made every call,
and own the result.

- The idea is theirs: a genuinely Pythonic PowerShell - typed records and a lazy, fluent pipeline
  over native OS bindings, never a `pwsh.exe` subprocess and never text parsing - so the obvious code
  is the correct code.
- Every design and architecture decision was the human's: the clean-architecture layering
  (domain / application / adapters / composition) with the dependency direction enforced by
  import-linter; the two-backend split (native win32 / wmi / winreg / psutil / socket / systemd / pwd
  by default, the real PowerShell 7 SDK hosted in-process behind the optional `[full]` extra for the
  AD / Exchange / Azure long tail); Pydantic `PSRecord`s built with `model_construct` on the trusted
  native hot path; memory-bounded streaming as the default, not an afterthought; locale-invariant
  identity (principals keyed by SID / uid, status and level marshaled to canonical English enums, not
  a translated OS string); the push to make the Windows-only subsystems genuinely cross-OS on their
  *honest* Linux mechanisms (systemd / D-Bus, journald, `pwd`/`grp`, shadow-utils, the Secret Service
  keyring, POSIX ACLs) rather than a shelled-out impersonation; and the safety policy that mutating
  verbs are exercised on a disposable VM or a throwaway Linux box, never a machine anyone cares about.
  Where there were options, the human picked.
- Shipping an AI-agent skill was the human's call: that the repo should carry a `using-pwsh` Claude
  Code skill so an agent reaches for pwshpy instead of writing or debugging PowerShell, how it should
  be delivered (the repo as its own plugin/marketplace, and mirrored into the separate bitranox-skills
  marketplace), and the skill's name and framing. The AI wrote the skill and the manifests to that
  brief.
- The human reviewed and corrected the work at each step; what ships is what they signed off on.
- Every commit went out under the human's name and authority, with no AI co-author line. The human is
  responsible for what is published.

## Where the AI was used

As a tool, under the human's direction, it did the mechanical parts: typing the domain records, the
lazy pipeline, the enums and the error hierarchy; the native adapters (win32 / wmi / winreg / psutil /
socket, and the Linux systemd / journald / `pwd`-`grp` / shadow-utils / Secret Service / POSIX-ACL
backends) and the in-process .NET host; the CLI, the tests and these docs - all to the human's design.
It probed the live OS interfaces before writing an adapter rather than guessing them (the systemd
D-Bus API over jeepney, the journald reader, the freedesktop Secret Service, the kernel POSIX-ACL
xattr cross-checked against `getfacl`, the shadow-utils argv), laid out the options at each fork for
the human to choose from, and ran the full gate. On the human's disposable Windows VM and a Linux
box, with their explicit go-ahead at each step, it ran the mutating and integration matrices (the
service, scheduled-task, local-account and ACL verbs on scratch objects, reverted afterwards). For the
agent skill, it wrote the self-contained `using-pwsh` runbook and the plugin/marketplace manifests,
mirrored a copy into the bitranox-skills marketplace, and pressure-tested the skill with throwaway
subagents (baseline without the skill, then with it) to confirm an agent following it reaches for
pwshpy and uses it correctly. None of the decisions, and none of the accountability, were the AI's -
the human directed and approved every action and owns the result.

## What's been checked, and what hasn't

The full gate is green on CI across ubuntu / macOS / windows and CPython 3.10 through 3.14: `ruff`,
`ruff format`, `pyright --strict` (zero errors), `import-linter` (the domain-is-pure and layer-order
contracts), `bandit`, `pip-audit`, and the `pytest` suite (well over 900 tests, with an 80% coverage
floor). Every native read command is pinned by an oracle test that runs the equivalent real cmdlet
(`Get-Service`, `Get-WinEvent`, `Get-CimInstance`, `Get-ScheduledTask`, `Get-LocalUser`, `Get-Acl`,
...) and compares pwshpy's typed records against it on locale-invariant fields.

The cross-OS work was verified live on a real Linux box, not asserted: the systemd service and timer
controllers on root scratch units, the credential round-trip against gnome-keyring, the POSIX ACL read
cross-checked entry-for-entry against `getfacl` and its mutation exercised on a temp file, and local
account creation/removal via shadow-utils - each cleaned up afterward.

What has not been checked: the .NET backend needs the `[full]` extra plus a .NET 10 runtime present;
the registry and CIM/WMI stay Windows-only because there is no honest Linux analog; and the mutating
Windows verbs are exercised on a disposable VM, not on every host or Windows build.

## Checking it yourself

The architecture is meant to be legible. The domain layer is pure (no I/O, no frameworks); adapters
marshal foreign values into the typed records at a single seam; and `import-linter` proves the
dependencies only point inward - run `lint-imports` to see the two contracts hold. The tests live in
`tests/`; run `make test` (it runs the whole gate). The design is documented under [docs/](docs/) -
the architecture decisions in [docs/adr](docs/adr), and the two backends and the
one-script-every-machine portability story in
[docs/backends-and-platforms.md](docs/backends-and-platforms.md) - and summarized in the
[README](README.md).

## What this isn't

It isn't a Microsoft or PowerShell product, and neither Microsoft nor the PowerShell team has reviewed
or endorsed it. It is a young project. The .NET backend is optional and off by default (`[full]`), and
a couple of subsystems (the registry, CIM/WMI) are deliberately Windows-only. And it isn't a way to
avoid understanding the OS you are automating: pwshpy makes the honest call to the native API instead
of a text-scraped subprocess, but the call it makes is the one you would have had to make yourself.

## License and attribution

The text and code here are under the MIT License (see [`LICENSE`](LICENSE)). Anthropic's terms put
ownership of model output with the user, so the human owns this and answers for it.
