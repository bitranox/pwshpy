# Locale independence and identity (SIDs)

pwshpy is developed on a German Windows box but ships to every locale, and it is
a Python library used to write portable scripts.  Two rules follow; pwshpy's
design and its test suite enforce both.

## 1. Locale-independent values

Windows and PowerShell surface many values in the OS UI language: a service
`Status` displays as `Ausgeführt` (de) / `Running` (en), an event `Level` as
`Informationen` / `Information`, and a service `DisplayName` or an event
`Message` is fully translated.  A script that keys off those strings passes on
one locale and breaks on another.

pwshpy therefore never exposes a localized string as a stable value:

- Every fixed-value field is a `str` enum keyed by the canonical **English** name
  (`ServiceState.RUNNING = "Running"`, `EventLevel.INFORMATION = "Information"`,
  `RegistryValueType.REG_SZ`, ...), marshaled from the win32 **numeric** constant
  in `adapters/native/marshal.py` - never parsed from the localized token.
- Genuinely free-text localized fields (a service `display_name`, an event
  `message`) are carried as-is for display, but are never a match key and are
  never asserted against a literal in a test.

The pwsh oracles compare on locale-invariant bases: a .NET enum's `.ToString()`
returns the **English** member name regardless of UI culture (`Get-Service`
`Status.ToString()` is `Running` on German Windows too), or the raw numeric value
(`Get-WinEvent .Level`).  They never compare a localized display name
(`LevelDisplayName`, a translated `DisplayName`).  A `local_only` + `os_windows`
**locale-invariance test** additionally asserts that, on this German box, pwshpy
still emits English enum values - a regression guard against ever marshaling a
localized string.

## 2. Identity by SID, not by name

Accounts and groups have localized well-known names (`Administrators` =
`Administratoren`, `Users` = `Benutzer`, `Everyone` = `Jeder`) but stable,
locale-independent SIDs:

| Principal           | SID          |
|---------------------|--------------|
| Administrators      | S-1-5-32-544 |
| Users               | S-1-5-32-545 |
| SYSTEM              | S-1-5-18     |
| LocalService        | S-1-5-19     |
| NetworkService      | S-1-5-20     |
| Everyone            | S-1-1-0      |
| Authenticated Users | S-1-5-11     |

So every pwshpy subsystem that exposes a security principal (ACLs / `Get-Acl`,
local accounts / `Get-LocalUser` + `Get-LocalGroup`, an event's `user_id`, a
service's log-on account) carries the **SID as the canonical field** and the
localized readable name (via `LookupAccountSid`) only as a secondary display
field.  Callers match and filter on the SID - or on a `WellKnownSid` enum keyed
by the English name that resolves to the SID - so a script written on English
Windows runs identically on German Windows:

```python
# Portable - works on any locale.
admins = ps.get_acl(path).where(lambda e: e.trustee_sid == WellKnownSid.ADMINISTRATORS)

# Fragile - matches only on the box's own language.
admins = ps.get_acl(path).where(lambda e: e.trustee_name == "Administrators")
```

`EventLogEntry.user_id` already follows this: it is the raw SID from the event
XML (`S-1-5-18`), not a resolved name.  The `WellKnownSid` enum and the ACL /
local-account records land with those subsystems.
