#Requires -Version 5.1
<#
.SYNOPSIS
    Packed with pwshpy: the Python script '@@PWSHPY_ENTRY_NAME@@' and its local modules, in one file.

.DESCRIPTION
    This file carries '@@PWSHPY_ENTRY_NAME@@' and every local module it imports as a base64 zip.
    Running it unpacks them into a per-user cache, installs `uv` if this machine has none,
    runs the script, and exits with the script's own exit code. Nothing needs to be
    installed first, not even Python.

    Look inside without running any of it:

        pwsh -File <this file> -PwshPyInfo

    Unpack the Python sources to read or edit them (uvx installs nothing permanently):

        uvx pwshpy unpack <this file> -o src

    Re-pack after editing, replacing this file:

        uvx pwshpy pack src/@@PWSHPY_ENTRY_NAME@@ -o <this file> --force

    Switches this wrapper understands. Every other argument is passed to the packed script
    exactly as you typed it, so a script taking -v or --path still works:

        -PwshPyHelp         show this help and exit
        -PwshPyInfo         show the payload manifest and exit
        -PwshPyClean        discard the cached extraction and unpack again
        -PwshPyNoInstallUv  fail with exit code 127 rather than installing uv
        -PwshPyElevate      relaunch elevated first (UAC on Windows, sudo on POSIX);
                            exit code 77 if the elevation is refused
        -PwshPyUv PATH      run the uv at this absolute path instead of searching for one

    Run elevated (an administrator token, SYSTEM, or root), it only runs a uv that no other account
    can modify: neither the file nor the directory holding it may be owned by, or grant write access
    to, any account other than the running one, SYSTEM, Administrators, LOCAL SERVICE, NETWORK
    SERVICE or TrustedInstaller, and no directory above may let another account delete, rename or
    take it over (on POSIX: root must own all of them, and nobody else may write them; a directory
    above may be writable only with the sticky bit). Any other uv it finds is skipped with a line on
    stderr, and a -PwshPyUv path that fails the check is refused with exit code 127. As root on
    POSIX it runs its own helpers only from the fixed system directories. Elevated, the script runs
    only on a Python uv manages in the running account's own data directory (the RoamingAppData
    known folder on Windows, <home from the passwd database>/.local/share on POSIX; downloaded on
    first use unless UV_PYTHON_DOWNLOADS is never or manual), found with `uv python find --system`
    and handed to uv run as --python after the same check as uv itself, so no .venv anywhere is ever
    run; that directory, and every file and directory below it, passes the same check before uv
    searches it (uv runs every Python it finds there), or the run exits 127; uv starts in a
    directory only administrators can write (/ on POSIX, System32 on Windows), and the script itself
    still runs in your directory. uv's cache is the running account's own too (the LocalAppData
    known folder on Windows, <home from the passwd database>/.cache on POSIX; named on stderr).
    Every uv call an elevated run makes reads no uv.toml (--no-config). UV_PYTHON, VIRTUAL_ENV,
    CONDA_PREFIX, UV_MANAGED_PYTHON, UV_NO_MANAGED_PYTHON, UV_PYTHON_INSTALL_DIR, UV_CONFIG_FILE,
    UV_PYTHON_INSTALL_MIRROR, UV_PYTHON_DOWNLOADS_JSON_URL, UV_ASTRAL_MIRROR_URL,
    UV_PYPY_INSTALL_MIRROR, UV_GRAALPY_INSTALL_MIRROR, UV_PYODIDE_INSTALL_MIRROR, PYTHONHOME,
    PYTHONPATH, PYTHONUSERBASE, UV_INDEX_URL, UV_DEFAULT_INDEX, UV_INDEX, UV_EXTRA_INDEX_URL,
    UV_FIND_LINKS, UV_INSECURE_HOST, UV_CACHE_DIR, UV_PYTHON_CACHE_DIR, UV_ENV_FILE, HTTPS_PROXY,
    HTTP_PROXY, ALL_PROXY (and their lower-case forms), SSL_CERT_FILE, SSL_CERT_DIR,
    SSL_CLIENT_CERT, UV_NATIVE_TLS, UV_SYSTEM_CERTS and UV_KEYRING_PROVIDER are ignored, each with a
    line on stderr; the user site-packages is off (PYTHONNOUSERSITE=1) and uv run reads no .env file
    (--no-env-file). In a network that reaches the internet only through a proxy, an elevated run
    can download nothing: the managed Python and every package the script needs must already be in
    the account's directories named above.

    Set PWSHPY_PACK_CACHE to choose where the payload unpacks (default: a per-user cache dir).

    Home: https://github.com/bitranox/pwshpy
#>

# No param() block and no [CmdletBinding()] here, deliberately. An advanced script binds
# PowerShell's common parameters, so a packed script's own -v / -d / -ea would be captured
# by -Verbose / -Debug / -ErrorAction and never reach Python. Hand-parsing $args keeps
# every argument the caller typed.

$ErrorActionPreference = 'Stop'

$PwshPyEntry = @@PWSHPY_ENTRY@@
$PwshPyShim = '@@PWSHPY_SHIM@@'
$PwshPySha = '@@PWSHPY_PAYLOAD_SHA256@@'
$PwshPyArgvEnv = '@@PWSHPY_ARGV_ENV@@'
$PwshPyUvArgs = @(@@PWSHPY_UV_ARGS@@)
$PwshPyCwdEnv = '@@PWSHPY_CWD_ENV@@'
# Per uv arg, the relative path an elevated run (which starts uv in another directory) puts the
# caller's directory in front of: '' for a value left as it is. Parallel to $PwshPyUvArgs.
$PwshPyUvArgsRelative = @(@@PWSHPY_UV_ARGS_RELATIVE@@)
# The packed script's requires-python ('' for none): the request an elevated run hands `uv python find`.
$PwshPyPythonRequest = @@PWSHPY_PYTHON_REQUEST@@

$PwshPyFileHashes = @'
@@PWSHPY_FILE_HASHES@@
'@

$PwshPyPayload = @'
@@PWSHPY_PAYLOAD_B64@@
'@

# $IsWindows only exists in PowerShell 6+; on 5.1 the host is Windows by definition.
if ($null -eq $IsWindows) { $PwshPyOnWindows = $true } else { $PwshPyOnWindows = $IsWindows }

function Get-PwshPyCacheRoot {
    if ($env:PWSHPY_PACK_CACHE) { return $env:PWSHPY_PACK_CACHE }
    if ($PwshPyOnWindows -and $env:LOCALAPPDATA) { return (Join-Path $env:LOCALAPPDATA 'pwshpy-pack') }
    if ($env:XDG_CACHE_HOME) { return (Join-Path $env:XDG_CACHE_HOME 'pwshpy-pack') }
    return (Join-Path (Join-Path $HOME '.cache') 'pwshpy-pack')
}

function Get-PwshPyPayload {
    $bytes = [Convert]::FromBase64String(($PwshPyPayload -replace '\s', ''))
    # Verify the whole payload before it is ever written or extracted: a base64 blob damaged in
    # transit would otherwise reach ExtractToDirectory and fail with a cryptic zip error, or (if
    # it still unzips) be caught only by the slower per-file check afterwards.
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        # String comparison is case-insensitive, so the uppercase hex here matches the lowercase
        # digest the packer embedded.
        $actual = [BitConverter]::ToString($sha256.ComputeHash($bytes)).Replace('-', '')
    } finally {
        $sha256.Dispose()
    }
    if ($actual -ne $PwshPySha) {
        throw "the embedded payload is corrupt: sha256 $actual does not match the expected $PwshPySha"
    }
    return $bytes
}

function Expand-PwshPyPayload {
    param([string] $Destination)

    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null

    $archive = Join-Path $Destination '.payload.zip'
    [IO.File]::WriteAllBytes($archive, (Get-PwshPyPayload))
    # System.IO.Compression.FileSystem is preloaded on PowerShell 7 but not on 5.1.
    try { $null = [System.IO.Compression.ZipFile] } catch { Add-Type -AssemblyName System.IO.Compression.FileSystem }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($archive, $Destination)
    Remove-Item -LiteralPath $archive -Force
}

function Invoke-PwshPyUnpackLocked {
    param([string] $CacheDir)

    # Serialize extraction across processes with an exclusive lock file next to the cache dir, so
    # two cold-start runs of the same pack cannot stomp each other's extraction: without it, one
    # process's Remove-Item + re-extract can yank the directory another is already running from.
    # The warm path (a verified tree) never reaches here, so a normal run pays no lock cost.
    $parent = Split-Path -Parent $CacheDir
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    # New-Item -Force does not complain when a FILE sits at that path, and opening the lock under
    # it then raises the same IOException as a held lock, so the loop below would wait out its
    # 60 s and blame another process. Say what is actually wrong instead.
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw "the cache root $parent is not a directory (check PWSHPY_PACK_CACHE)"
    }
    $lockPath = "$CacheDir.lock"
    for ($attempt = 0; $attempt -lt 600; $attempt++) {
        $lock = $null
        try {
            $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::Write, [IO.FileShare]::None)
        } catch [IO.IOException] {
            Start-Sleep -Milliseconds 100
            continue
        }
        try {
            # Double-checked: another process may have finished extracting while we waited.
            if (Test-PwshPyTree -Root $CacheDir) { return }
            Expand-PwshPyPayload -Destination $CacheDir
            if (-not (Test-PwshPyTree -Root $CacheDir)) {
                throw "the unpacked payload in $CacheDir does not match its recorded hashes"
            }
            return
        } finally {
            $lock.Close()
        }
    }
    throw "timed out after 60s waiting to unpack $CacheDir (another process holds $lockPath)"
}

function Test-PwshPyTree {
    param([string] $Root)

    # Re-verify on every run, not just after extraction: the cache is a user-writable
    # directory whose contents may later be executed elevated, so a tampered file has to
    # be caught before it runs, not merely when it is unpacked.
    #
    # The whole check is wrapped so ANY failure - a mismatch, a missing file, or a transient
    # IO error from reading a file another process is mid-extraction on (this runs before the
    # lock, so two cold starts DO overlap here) - resolves to "not verified" and re-extraction
    # under the lock, never a terminating Get-FileHash error that would kill the run under
    # $ErrorActionPreference = 'Stop'.
    try {
        foreach ($line in ($PwshPyFileHashes -split "`n")) {
            $entry = $line.Trim()
            if (-not $entry) { continue }
            $parts = $entry -split ' \*', 2
            if ($parts.Count -ne 2) { return $false }
            $target = Join-Path $Root $parts[1]
            if (-not (Test-Path -LiteralPath $target)) { return $false }
            # PowerShell string comparison is case-insensitive, so Get-FileHash's uppercase
            # digest compares equal to the lowercase hex the packer embedded.
            if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $parts[0]) { return $false }
        }
    } catch {
        return $false
    }
    return $true
}

# The accounts whose write access to uv changes nothing, because an elevated run already acts with
# their rights: SYSTEM, LOCAL SERVICE, NETWORK SERVICE, BUILTIN\Administrators, TrustedInstaller,
# plus the running account itself (added in Get-PwshPyTrustedSid). By SID, never by name: the names
# are localised ('Administratoren' on a German Windows).
$PwshPyTrustedSids = @(
    'S-1-5-18',
    'S-1-5-19',
    'S-1-5-20',
    'S-1-5-32-544',
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
)
# The rights that let their holder change what runs. On a file: rewrite it (WriteData, AppendData),
# delete it, or take it over (WriteDAC, WriteOwner); on a directory also add or remove an entry
# (AddFile = WriteData, AddSubdirectory = AppendData, DeleteSubdirectoriesAndFiles). GenericAll and
# GenericWrite are the unmapped forms an ACE may still carry. Read and execute rights are harmless.
$PwshPyFileWriteRights = 0x2 -bor 0x4 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000
$PwshPyDirectoryWriteRights = $PwshPyFileWriteRights -bor 0x40
# Above the directory holding uv, only the rights that let their holder rename an entry away and put
# another in its place: delete the directory itself or its children (Delete, DeleteSubdirectoriesAndFiles),
# or take it over (WriteDAC, WriteOwner, GenericAll). Adding entries is harmless there, which matters:
# C:\ grants Authenticated Users AppendData (create folders) on itself.
$PwshPyAncestorWriteRights = 0x40 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000
# POSIX: the accounts trusted to own what root runs, by uid: root alone.
$PwshPyTrustedUids = @('0')
# POSIX: the PATH a root run executes its own helpers (ls, id, the uv installer's curl and sh) from;
# the caller's PATH may hold a directory another account can write. System directories alone (user
# decision), identical to the .sh runner: not /usr/local, which local installs populate.
$PwshPyFixedPath = '/usr/bin:/bin:/usr/sbin:/sbin:/run/current-system/sw/bin:/run/current-system/profile/bin'
# POSIX: the id executables in those same directories, the only ones a root run ever asks.
$PwshPyFixedIds = @('/usr/bin/id', '/bin/id', '/usr/sbin/id', '/sbin/id', '/run/current-system/sw/bin/id', '/run/current-system/profile/bin/id')
# The variables through which a caller names the interpreter itself, past --python-preference
# only-managed (measured with uv 0.13: an explicit UV_PYTHON path and an active virtual or conda
# environment each run their own python), or that uv refuses beside that flag; then the ones that
# move where uv's managed Pythons live or come from (another install directory, config file, download
# mirror or download list; uv 0.13 also fetches CPython through UV_ASTRAL_MIRROR_URL and PyPy, GraalPy
# and Pyodide through their own mirror variables, measured), which would let the caller's environment
# supply the interpreter after all (user decision on [59]). An elevated run drops them all; identical
# to the .sh runner.
$PwshPyInterpreterVariables = @(
    'UV_PYTHON', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'UV_MANAGED_PYTHON', 'UV_NO_MANAGED_PYTHON',
    'UV_PYTHON_INSTALL_DIR', 'UV_CONFIG_FILE', 'UV_PYTHON_INSTALL_MIRROR', 'UV_PYTHON_DOWNLOADS_JSON_URL',
    'UV_ASTRAL_MIRROR_URL', 'UV_PYPY_INSTALL_MIRROR', 'UV_GRAALPY_INSTALL_MIRROR', 'UV_PYODIDE_INSTALL_MIRROR'
)
# The variables the interpreter reads itself, whichever one runs: PYTHONHOME replaces its standard
# library, PYTHONPATH puts the caller's directories ahead of it and PYTHONUSERBASE names the user
# site-packages, so each would run code the caller chose with elevated rights. An elevated run drops
# them too, and turns the user site-packages off (PYTHONNOUSERSITE); identical to the .sh runner.
$PwshPyLibraryVariables = @('PYTHONHOME', 'PYTHONPATH', 'PYTHONUSERBASE')
# Where uv takes packages (an index, a find-links location, a host it skips TLS checks for), its caches
# and an env file from: each is the caller's to choose, and an env file can set PYTHONPATH again (uv
# run loads UV_ENV_FILE despite --no-config, measured with uv 0.13). An elevated run drops them and uses
# uv's defaults (user decision, X21); its uv run also passes --no-env-file, which no variable undoes.
# Identical to the .sh runner.
$PwshPyUvSourceVariables = @(
    'UV_INDEX_URL', 'UV_DEFAULT_INDEX', 'UV_INDEX', 'UV_EXTRA_INDEX_URL', 'UV_FIND_LINKS', 'UV_INSECURE_HOST',
    'UV_CACHE_DIR', 'UV_PYTHON_CACHE_DIR', 'UV_ENV_FILE'
)
# How uv connects: a proxy (uv 0.13 sends https through HTTPS_PROXY, http through HTTP_PROXY and both
# through ALL_PROXY, each also in lower case, measured; on Windows the two spellings are one variable),
# the certificate roots it trusts (SSL_CERT_FILE and SSL_CERT_DIR replace them, measured; UV_NATIVE_TLS
# and UV_SYSTEM_CERTS switch to the platform store), the client certificate it presents, and a keyring
# helper it runs for credentials (UV_KEYRING_PROVIDER=subprocess runs `keyring` from PATH). Each lets
# the caller decide what the default index serves to an elevated run, or what runs elevated; NO_PROXY
# only exempts hosts from a proxy and changes nothing once these are gone. The packed script runs
# without them too. Identical to the .sh runner.
$PwshPyNetworkVariables = @(
    'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
    'SSL_CLIENT_CERT', 'UV_NATIVE_TLS', 'UV_SYSTEM_CERTS', 'UV_KEYRING_PROVIDER'
)

function Get-PwshPyRunningUserSid {
    # The account this process runs as, from its token: USERNAME can be set by anyone, and under
    # SYSTEM it names the machine account rather than S-1-5-18.
    return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}

function Get-PwshPyTrustedSid {
    # The running account may write its own uv: whatever that uv could do elevated, this account
    # could already do by swapping it before elevating (the accepted residual risk, user decision on
    # [56]). Under SYSTEM it adds nothing, so a scheduled task keeps the strict rule; another user,
    # Users, Authenticated Users or Everyone holding write access still refuses the uv.
    return @($PwshPyTrustedSids) + @(Get-PwshPyRunningUserSid)
}

function Test-PwshPyAbsolutePath {
    param([string] $Path)

    # [IO.Path]::IsPathRooted calls 'C:uv.exe' and '\uv.exe' rooted, though both depend on the
    # current drive or directory; only a drive-qualified or UNC path names one file.
    if ($PwshPyOnWindows) { return ($Path -match '^[A-Za-z]:[\\/]' -or $Path -match '^[\\/]{2}[^\\/]') }
    return $Path.StartsWith('/')
}

function Get-PwshPyAclDistrust {
    param([string] $Path, [int] $Rights, [object] $Security = $null)

    # Returns why another account could change $Path through any of $Rights, or '' when none can.
    # The owner counts as a writer whatever the ACL says, because an owner may always rewrite it.
    # $Security is the path's security descriptor when the caller already read it (the tree walk, see
    # Get-PwshPyPythonTreeDistrust); otherwise it is read here.
    $trusted = Get-PwshPyTrustedSid
    $acl = $Security
    if ($null -eq $acl) {
        try {
            $acl = Get-Acl -LiteralPath $Path
        } catch {
            return "the ACL of $Path cannot be read ($($_.Exception.Message))"
        }
    }
    $sidType = [Security.Principal.SecurityIdentifier]
    $owner = $acl.GetOwner($sidType)
    $ownerSid = if ($owner) { $owner.Value } else { 'an unknown account' }
    if ($trusted -notcontains $ownerSid) { return "$Path is owned by $ownerSid, neither the running account nor an administrative one" }
    foreach ($rule in $acl.GetAccessRules($true, $true, $sidType)) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        # An inherit-only entry applies to children created later, not to this object.
        if ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
        $sid = $rule.IdentityReference.Value
        # CREATOR OWNER and OWNER RIGHTS stand for the owner, already judged above.
        if ($sid -eq 'S-1-3-0' -or $sid -eq 'S-1-3-4') { continue }
        if ($trusted -contains $sid) { continue }
        if (([int]$rule.FileSystemRights) -band $Rights) {
            return "$Path lets $sid change it ($($rule.FileSystemRights))"
        }
    }
    return ''
}

function Get-PwshPyListing {
    param([string] $Path)

    # POSIX: mode, uid and gid of $Path itself (a symlink is not followed) from ls -ldn, the one
    # listing every POSIX system has (stat's flags differ between GNU, BSD and busybox). A root run
    # resolves ls from its fixed PATH, so the listing cannot come from a planted ls.
    $ls = Get-Command ls -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ls) { return $null }
    # Quoted, always: pwsh on POSIX globs an unquoted argument to a native command, a variable's
    # value included, so 'd?r' would list whichever d0r sorts first instead of d?r itself.
    try {
        $listing = & $ls.Source -ldn "$Path" 2>$null
    } catch {
        $listing = $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $listing) { return $null }
    $fields = ([string](@($listing)[0])).Trim() -split '\s+'
    $acl = $false
    if ($fields[0].Length -gt 10 -and $fields[0][10] -eq [char]'+') { $acl = $true }
    if ($fields[0].Length -gt 10 -and $fields[0][10] -eq [char]'@') {
        # macOS puts '@' (extended attributes) where '+' would mark an ACL, hiding it, so ask for the
        # ACL itself: ls -e prints one line per entry below the file's own line.
        $acl = @(& $ls.Source -lde "$Path" 2>$null).Count -gt 1
    }
    return @{ Mode = $fields[0]; Uid = $fields[2]; Gid = $fields[3]; Acl = $acl }
}

function Get-PwshPyModeDistrust {
    param([string] $Path, [bool] $Ancestor)

    # POSIX: why $Path may not be on the way to what root runs, or ''. The file and the directory
    # holding it: root owns it, no ACL, no group or other write (busybox ls never shows '+', but on
    # Linux an ACL granting anyone write shows as group write through its mask). An ancestor: root
    # owns it and no ACL; group or other write only with the sticky bit (/tmp), which lets nobody
    # but an entry's owner rename or delete it.
    $entry = Get-PwshPyListing -Path $Path
    if (-not $entry) { return "$Path cannot be inspected" }
    $mode = $entry.Mode
    if ($entry.Acl) { return "$Path carries an ACL" }
    if ($Ancestor) {
        if ($PwshPyTrustedUids -notcontains $entry.Uid) { return "$Path is owned by uid $($entry.Uid), not root" }
        if ($mode[9] -eq [char]'t' -or $mode[9] -eq [char]'T') { return '' }
        if ($mode[8] -eq [char]'w') { return "$Path is writable by every account and not sticky" }
        if ($mode[5] -eq [char]'w') { return "$Path is writable by group $($entry.Gid) and not sticky" }
        return ''
    }
    if ($mode[8] -eq [char]'w') { return "$Path is writable by every account" }
    if ($mode[5] -eq [char]'w') { return "$Path is writable by group $($entry.Gid)" }
    if ($PwshPyTrustedUids -notcontains $entry.Uid) { return "$Path is owned by uid $($entry.Uid), not root" }
    return ''
}

function Get-PwshPyLeafDistrust {
    param([string] $Path, [bool] $Directory, [object] $Security = $null)

    if (-not $PwshPyOnWindows) { return (Get-PwshPyModeDistrust -Path $Path -Ancestor $false) }
    $rights = if ($Directory) { $PwshPyDirectoryWriteRights } else { $PwshPyFileWriteRights }
    return (Get-PwshPyAclDistrust -Path $Path -Rights $rights -Security $Security)
}

function Get-PwshPyAncestorDistrust {
    param([string] $Path)

    if (-not $PwshPyOnWindows) { return (Get-PwshPyModeDistrust -Path $Path -Ancestor $true) }
    return (Get-PwshPyAclDistrust -Path $Path -Rights $PwshPyAncestorWriteRights)
}

function Get-PwshPyLinkDistrust {
    param([string] $Path)

    # Who could repoint the link $Path. POSIX: its own mode means nothing, so only its owner (the
    # directory holding it is judged as an ancestor). Windows: the rights that delete or take over
    # the link object.
    if ($PwshPyOnWindows) { return (Get-PwshPyAclDistrust -Path $Path -Rights $PwshPyAncestorWriteRights) }
    $entry = Get-PwshPyListing -Path $Path
    if (-not $entry) { return "the symlink $Path cannot be inspected" }
    if ($PwshPyTrustedUids -notcontains $entry.Uid) { return "the symlink $Path is owned by uid $($entry.Uid), not root" }
    return ''
}

function Test-PwshPyExecutable {
    param([string] $Path)

    # POSIX: whether any execute bit is set on $Path (a symlink lists as executable; its target is
    # judged by the walk). From the same ls -ldn listing the trust rule reads.
    $entry = Get-PwshPyListing -Path $Path
    if (-not $entry -or $entry.Mode.Length -lt 10) { return $false }
    # Case-sensitive: an upper-case S or T marks the special bit WITHOUT execute.
    $mode = $entry.Mode
    return (([string]$mode[3] -cin @('x', 's')) -or ([string]$mode[6] -cin @('x', 's')) -or ([string]$mode[9] -cin @('x', 't')))
}

function Split-PwshPyPath {
    param([string] $Path)

    # The components below the root, separators of either kind.
    return @(($Path.Substring([IO.Path]::GetPathRoot($Path).Length) -split '[\\/]') | Where-Object { $_ })
}

function Get-PwshPyUvDistrust {
    param([string] $Path, [bool] $Directory = $false)

    # Returns why an elevated run must not execute the uv at $Path, or '' when it may. Walks the
    # path one component at a time from its root, resolving each symlink or junction where it is
    # met, so every real directory the path passes through is judged, the ones holding a link
    # included: whoever can rename any of them, or repoint a link, decides what runs. Reported in
    # order: a link that fails (at once), the directory holding uv, the first ancestor that fails,
    # then the file. A directory link on the way (/bin -> usr/bin on a merged-/usr system) is
    # resolved like any other. -Directory judges the last component as a directory (on Windows the
    # rights that add or remove an entry count too), for a path that names one.
    if (-not (Test-PwshPyAbsolutePath -Path $Path)) { return "$Path is not an absolute path" }
    if ($PwshPyOnWindows) { $Path = $Path.Replace('/', '\') }
    $current = [IO.Path]::GetPathRoot($Path)
    $rest = New-Object System.Collections.Generic.List[string]
    $rest.AddRange([string[]](Split-PwshPyPath -Path $Path))
    $ancestor = Get-PwshPyAncestorDistrust -Path $current
    $next = $null
    $hops = 0
    while ($rest.Count -gt 0) {
        $component = $rest[0]
        $rest.RemoveAt(0)
        if ($component -eq '.') { continue }
        if ($component -eq '..') {
            $parent = [IO.Path]::GetDirectoryName($current)
            if ($parent) { $current = $parent }
            continue
        }
        $next = Join-Path $current $component
        $item = Get-Item -LiteralPath $next -Force -ErrorAction SilentlyContinue
        $linkType = if ($item) { [string]$item.LinkType } else { '' }
        if ($linkType -eq 'SymbolicLink' -or $linkType -eq 'Junction') {
            $hops++
            if ($hops -gt 40) { return "$Path passes more than 40 symlinks" }
            $why = Get-PwshPyLinkDistrust -Path $next
            if ($why) { return $why }
            # Target is a string on PowerShell 7 and a one-element array on 5.1; a junction's may
            # carry the NT prefix.
            $target = ([string](@($item.Target)[0])) -replace '^\\\?\?\\', ''
            if (-not $target) { return "the symlink $next cannot be read" }
            if (Test-PwshPyAbsolutePath -Path $target) {
                if ($PwshPyOnWindows) { $target = $target.Replace('/', '\') }
                $root = [IO.Path]::GetPathRoot($target)
                if ($root -ne [IO.Path]::GetPathRoot($current) -and -not $ancestor) { $ancestor = Get-PwshPyAncestorDistrust -Path $root }
                $current = $root
            }
            $rest.InsertRange(0, [string[]](Split-PwshPyPath -Path $target))
            continue
        }
        # The last component is the file itself; everything before it is a directory on the way.
        if ($rest.Count -eq 0) { break }
        $current = $next
        if (-not $ancestor) { $ancestor = Get-PwshPyAncestorDistrust -Path $current }
    }
    if (-not $next) { return "$Path names no file" }
    $why = Get-PwshPyLeafDistrust -Path $current -Directory $true
    if ($why) { return "its directory $why" }
    if ($ancestor) { return "its ancestor $ancestor" }
    return (Get-PwshPyLeafDistrust -Path $next -Directory $Directory)
}

function Write-PwshPyNotice {
    param([string] $Message)

    # A line that does not stop the run. An elevated child's console window is never seen, and the
    # run below rewrites the .err relay file with the script's own stderr, so the child keeps its
    # notices in a relay file of their own, which the parent replays first.
    if ($PwshPyRelay) {
        [IO.File]::AppendAllText("$PwshPyRelay.note", $Message + [Environment]::NewLine)
        return
    }
    [Console]::Error.WriteLine($Message)
}

function Test-PwshPyUvAcceptable {
    param([string] $Candidate)

    # Only reached for an elevated run: a uv that fails the trust rule is named on stderr, so the
    # reason a different uv ran (or none) is visible.
    $why = Get-PwshPyUvDistrust -Path $Candidate
    if (-not $why) { return $true }
    Write-PwshPyNotice "pwshpy: skipping uv at ${Candidate}: $why (an elevated run only runs a uv no other account can modify)"
    return $false
}

function Get-PwshPyUvLocation {
    # The fixed places the installer puts uv, for a uv that is not on PATH. An unset variable
    # contributes nothing (Join-Path would fail on it).
    $locations = @()
    if ($PwshPyOnWindows) {
        if ($env:USERPROFILE) { $locations += (Join-Path $env:USERPROFILE '.local\bin\uv.exe') }
        if ($env:LOCALAPPDATA) { $locations += (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe') }
        if ($env:USERPROFILE) { $locations += (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe') }
        # The installer honours XDG_BIN_HOME on Windows too, and installs there when it is set.
        if ($env:XDG_BIN_HOME) { $locations += (Join-Path $env:XDG_BIN_HOME 'uv.exe') }
        return $locations
    }
    $locations += (Join-Path $HOME '.local/bin/uv')
    $locations += (Join-Path $HOME '.cargo/bin/uv')
    $locations += '/usr/local/bin/uv'
    # The installer honours XDG_BIN_HOME / CARGO_HOME, so a machine that sets either installs
    # uv there rather than under ~/.local/bin - check them too before giving up.
    if ($env:XDG_BIN_HOME) { $locations += (Join-Path $env:XDG_BIN_HOME 'uv') }
    if ($env:CARGO_HOME) { $locations += (Join-Path $env:CARGO_HOME 'bin/uv') }
    return $locations
}

function Get-PwshPyUvPath {
    param([bool] $Elevated)

    # Not elevated: the first uv on PATH, then the fixed install locations. Elevated: EVERY uv on
    # PATH in order, then the fixed locations, the first that passes the trust rule - a planted uv
    # early on PATH must not hide a trusted one later on it.
    if ($Elevated -and -not $PwshPyOnWindows) {
        # A root run executes its helpers from the fixed PATH, so the caller's PATH is walked by
        # hand: every directory it names is a candidate, judged like any other.
        foreach ($directory in ($PwshPyCallerPath -split [IO.Path]::PathSeparator)) {
            # An empty entry means the working directory; spelled 'uv' it is refused as relative.
            $candidate = if ($directory) { Join-Path $directory 'uv' } else { 'uv' }
            # Exact lookups only: Get-Command would read '[', '*' and '?' in a PATH entry as wildcards.
            if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
            if (-not (Test-PwshPyExecutable -Path $candidate)) { continue }
            if (Test-PwshPyUvAcceptable -Candidate $candidate) { return $candidate }
        }
        $found = @()
    } else {
        $found = @(Get-Command uv -CommandType Application -ErrorAction SilentlyContinue)
    }
    foreach ($hit in $found) {
        if (-not $Elevated) { return $hit.Source }
        if (Test-PwshPyUvAcceptable -Candidate $hit.Source) { return $hit.Source }
    }
    foreach ($candidate in (Get-PwshPyUvLocation)) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        if (-not $Elevated) { return $candidate }
        if (Test-PwshPyUvAcceptable -Candidate $candidate) { return $candidate }
    }
    return $null
}

function Install-PwshPyUv {
    Write-Host 'uv was not found; installing it for the current user from https://astral.sh/uv ...'
    if ($PwshPyOnWindows) {
        # Windows PowerShell 5.1 still defaults to TLS 1.0/1.1, which astral.sh refuses.
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        # Download the installer to a file and run it in a child process with an explicit
        # ExecutionPolicy Bypass, rather than piping it through Invoke-Expression: iex on a
        # fetched string is exactly the pattern PSScriptAnalyzer warns about, and a plain
        # `& $file` on a freshly downloaded script would trip the machine's execution policy.
        $installer = Join-Path ([IO.Path]::GetTempPath()) ('uv-install-' + [Guid]::NewGuid().ToString('N') + '.ps1')
        Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing -OutFile $installer
        try {
            & (Get-PwshPyHostPath) -NoProfile -ExecutionPolicy Bypass -File $installer
        } finally {
            Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
        }
        $env:PATH = (Join-Path $env:USERPROFILE '.local\bin') + [IO.Path]::PathSeparator + $env:PATH
    } else {
        & /bin/sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh || wget -qO- https://astral.sh/uv/install.sh | sh'
        if ($LASTEXITCODE -ne 0) { throw "the uv installer failed with exit code $LASTEXITCODE" }
        # A root run keeps its fixed PATH for helpers; the new uv's directory joins the caller's PATH,
        # which the uv search walks and the packed script gets back.
        $script:PwshPyCallerPath = (Join-Path $HOME '.local/bin') + [IO.Path]::PathSeparator + $script:PwshPyCallerPath
        if (-not $PwshPyRootPath) { $env:PATH = $script:PwshPyCallerPath }
    }
}

function Test-PwshPyElevated {
    if ($PwshPyOnWindows) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    return ((Get-PwshPyEuid) -eq '0')
}

function Get-PwshPyEuid {
    # POSIX: the effective uid from an id in the fixed helper directories ($PwshPyFixedPath), never
    # through PATH: a planted id first on it could answer "not root" for a root run
    # and switch the uv trust rule and the fixed helper PATH off. When none of them answers with a
    # decimal uid the run is treated as root - it fails closed. That costs a non-root caller on such
    # a system the per-user uv lookup (the root rule refuses a uv its own account owns), never
    # safety; dying instead would cost the same caller the whole run. Identical to the .sh runner.
    foreach ($id in $PwshPyFixedIds) {
        if (-not (Test-Path -LiteralPath $id -PathType Leaf)) { continue }
        try {
            $uid = ([string](& $id -u 2>$null)).Trim()
        } catch {
            continue
        }
        if ($uid -match '^[0-9]+$') { return $uid }
    }
    return '0'
}

function Get-PwshPyHostPath {
    # The running PowerShell executable: powershell.exe on 5.1, pwsh elsewhere.
    return (Get-Process -Id $PID).Path
}

function Test-PwshPyUacDeclined {
    param([Exception] $Failure)

    # A refused UAC prompt is Win32Exception 1223 (ERROR_CANCELLED); from PowerShell it arrives
    # wrapped in a MethodInvocationException, so walk the inner exceptions. Matched by the code
    # because the message text is localised.
    $win32 = $Failure
    while ($win32 -and -not ($win32 -is [System.ComponentModel.Win32Exception])) { $win32 = $win32.InnerException }
    return [bool]($win32 -and $win32.NativeErrorCode -eq 1223)
}

function ConvertTo-PwshPyCommandLineArgument {
    param([string] $Value)

    # Quotes one argument so the child's parser (CommandLineToArgvW, the C runtime, .NET) reads
    # it back unchanged: backslashes are literal except in front of a quote, where 2n+1 of them
    # make n plus a literal quote, so the ones in front of an embedded quote or the closing quote
    # are doubled. An empty argument needs its quotes, or it vanishes.
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $quoted = New-Object System.Text.StringBuilder
    [void]$quoted.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]'\') { $backslashes++; continue }
        if ($character -eq [char]'"') { $backslashes = 2 * $backslashes + 1 }
        [void]$quoted.Append([char]'\', $backslashes)
        [void]$quoted.Append($character)
        $backslashes = 0
    }
    [void]$quoted.Append([char]'\', 2 * $backslashes)
    [void]$quoted.Append('"')
    return $quoted.ToString()
}

function Join-PwshPyCommandLine {
    param([string[]] $Arguments)

    # A flat command line, because ProcessStartInfo.ArgumentList (which quotes for us) does not
    # exist on the .NET Framework that Windows PowerShell 5.1 runs on.
    $quoted = foreach ($argument in $Arguments) { ConvertTo-PwshPyCommandLineArgument -Value $argument }
    return (@($quoted) -join ' ')
}

function ConvertTo-PwshPyNativeCall {
    param([string] $FilePath, [string[]] $ArgumentList, [string] $StdoutPath, [string] $StderrPath)

    # The call `& FilePath ArgumentList...` as a script block in which every word is a single-quoted
    # literal. pwsh on Linux and macOS globs and tilde-expands each argument to a native program that
    # is not a quoted literal in the source - a variable, a splat and a subexpression alike - so
    # '*.log' would arrive as the matching file names; a quoted "$x" stays literal but cannot carry a
    # list. Written out as literals, nothing is expanded, on every host and version: the same
    # quoting a variable gets on Windows, where nothing is globbed. EscapeSingleQuotedStringContent
    # also doubles the typographic quotes PowerShell reads as single quotes, so no value can end its
    # literal early. The redirections go inside, so the program itself writes the files as before.
    $escape = { param([string] $Word) "'" + [Management.Automation.Language.CodeGeneration]::EscapeSingleQuotedStringContent($Word) + "'" }
    $words = New-Object System.Collections.Generic.List[string]
    $words.Add((& $escape $FilePath))
    foreach ($argument in $ArgumentList) { $words.Add((& $escape $argument)) }
    $text = '& ' + ($words -join ' ')
    if ($StdoutPath) { $text += ' > ' + (& $escape $StdoutPath) }
    if ($StderrPath) { $text += ' 2> ' + (& $escape $StderrPath) }
    return [scriptblock]::Create($text)
}

function Get-PwshPyAnchorDirectory {
    # A directory only administrators can write: / on POSIX, System32 on Windows, asked of the
    # system rather than of the environment.
    if ($PwshPyOnWindows) { return [Environment]::SystemDirectory }
    return '/'
}

function Get-PwshPyManagedPythonOption {
    param([string] $Python)

    # The uv options an elevated run adds: no uv.toml at all (--no-config: a system-wide one under
    # %ProgramData% or /etc, or the user's own, can name a cache directory, a package index or a Python
    # download mirror, each a way to choose what runs elevated, and its location is the caller's to set;
    # with the variables that name the same dropped (Clear-PwshPyInterpreterVariable), an elevated run
    # takes uv's defaults plus these flags, user decision); no .env file (--no-env-file, whatever
    # UV_NO_ENV_FILE says); only a uv-managed Python,
    # named outright (--python, see Resolve-PwshPyManagedPython); project discovery (the .python-version and uv.toml uv would
    # otherwise read from the packed script's directory and every directory above it, the cache,
    # which a non-administrator may be able to write) moved to the anchor; and uv itself started
    # there (--directory), so nothing in the caller's directory is read either. The generated shim
    # changes back to the caller's directory before the script runs.
    $anchor = Get-PwshPyAnchorDirectory
    return @('--no-config', '--python-preference', 'only-managed', '--no-env-file', '--python', $Python, '--project', $anchor, '--directory', $anchor)
}

function Invoke-PwshPyUvPython {
    param([string] $Uv, [string[]] $Arguments)

    # Runs `uv python <Arguments>` started in the anchor and reading no uv.toml (--no-config, see
    # Get-PwshPyManagedPythonOption), with the packed script's requires-python as the request when it
    # has one. Returns its exit code, its stdout (trimmed) and its stderr, which
    # is kept in a file rather than shown: an elevated child's console window is never seen, so the
    # caller hands it to Write-PwshPyNotice or Write-PwshPyFailure. With $ErrorActionPreference at
    # 'Stop', Windows PowerShell 5.1 would stop at the first line a native program writes to a
    # redirected stderr, so the call runs under 'Continue'.
    $words = @('python') + $Arguments + @('--no-config', '--directory', (Get-PwshPyAnchorDirectory))
    if ($PwshPyPythonRequest) { $words += $PwshPyPythonRequest }
    $stderr = [IO.Path]::GetTempFileName()
    $saved = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $stdout = @(. (ConvertTo-PwshPyNativeCall -FilePath $Uv -ArgumentList $words -StderrPath $stderr))
        $code = $LASTEXITCODE
        $text = [IO.File]::ReadAllText($stderr)
    } finally {
        $ErrorActionPreference = $saved
        Remove-Item -LiteralPath $stderr -Force -ErrorAction SilentlyContinue
    }
    return @{ Code = $code; Out = ((@($stdout) | ForEach-Object { [string]$_ }) -join "`n").Trim(); Err = $text.TrimEnd() }
}

function Get-PwshPyPythonRemedy {
    # What an elevated run that refused a managed-Python path tells the caller to do about it.
    if ($PwshPyOnWindows) {
        return 'take every other account''s write access off the path named above (only the running account, SYSTEM and Administrators may change it), or remove uv''s managed-Python directory and install the Python again elevated (uv python install)'
    }
    return 'make the path named above root''s alone (chown root, then chmod go-w on it, with -R for a directory), or remove uv''s managed-Python directory and install the Python again as root (uv python install)'
}

function Get-PwshPyPosixTreeDistrust {
    param([string] $Path)

    # POSIX half of Get-PwshPyPythonTreeDistrust, identical to the .sh runner's
    # pwshpy_python_tree_distrust: one find for the modes and owners, one batched ls for the ACLs (with
    # ACL lines where ls -e prints them, macOS and BSD), both from the fixed helper PATH. A regular
    # file named .lock is exempt from the write check: uv creates its lock file mode 0666 (measured with
    # uv 0.13) and only ever locks it, and no interpreter imports or runs a file of that name.
    $find = Get-Command find -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $ls = Get-Command ls -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $find -or -not $ls) { return "the entries below $Path cannot be listed (no find or ls)" }
    # The trailing slash makes find enter the directory when $Path is a symlink to it.
    $top = $Path.TrimEnd('/') + '/'
    $owners = New-Object System.Collections.Generic.List[string]
    foreach ($uid in $PwshPyTrustedUids) { $owners.AddRange([string[]]@('!', '-user', [string]$uid)) }
    $words = @($top, '(', '-type', 'l') + $owners.ToArray() + @(')', '-o', '(', '!', '-type', 'l', '(') + $owners.ToArray()
    $words += @('-o', '(', '(', '-perm', '-0002', '-o', '-perm', '-0020', ')', '!', '(', '-type', 'f', '-name', '.lock', ')', ')', ')', ')', '-print')
    $hits = @(. (ConvertTo-PwshPyNativeCall -FilePath $find.Source -ArgumentList $words -StderrPath '/dev/null'))
    if ($LASTEXITCODE -ne 0) { return "the entries below $Path cannot be listed" }
    if ($hits.Count -gt 0) {
        $hit = [string]$hits[0]
        $item = Get-Item -LiteralPath $hit -Force -ErrorAction SilentlyContinue
        $why = if ($item -and $item.LinkType) { Get-PwshPyLinkDistrust -Path $hit } else { Get-PwshPyLeafDistrust -Path $hit -Directory $true }
        if ($why) { return $why }
        return "$hit can be modified by another account"
    }
    # ls -e lists a file's ACL entries below it on macOS and BSD; GNU ls has no -e and marks an ACL with
    # '+' instead.
    $listOption = '-ldn'
    $null = @(. (ConvertTo-PwshPyNativeCall -FilePath $ls.Source -ArgumentList @('-lde', '/') -StdoutPath '/dev/null' -StderrPath '/dev/null'))
    if ($LASTEXITCODE -eq 0) { $listOption = '-lden' }
    $listing = @(. (ConvertTo-PwshPyNativeCall -FilePath $find.Source -ArgumentList @($top, '!', '-type', 'l', '-exec', $ls.Source, $listOption, '{}', '+') -StderrPath '/dev/null'))
    if ($LASTEXITCODE -ne 0) { return "the entries below $Path cannot be listed" }
    $acl = $false
    foreach ($line in $listing) {
        $text = [string]$line
        if ($text -match '^\s*[0-9]+: ' -or ($text.Length -gt 10 -and $text[10] -eq [char]'+')) { $acl = $true; break }
    }
    if (-not $acl) { return '' }
    # Which one, for the message: only now, entry by entry.
    foreach ($entry in @(. (ConvertTo-PwshPyNativeCall -FilePath $find.Source -ArgumentList @($top, '!', '-type', 'l', '-print') -StderrPath '/dev/null'))) {
        $listed = Get-PwshPyListing -Path ([string]$entry)
        if ($listed -and $listed.Acl) { return "$entry carries an ACL" }
    }
    return "an entry below $Path carries an ACL"
}

function Read-PwshPySecurity {
    param([IO.FileSystemInfo] $Item)

    # The owner and the DACL of $Item, read through .NET rather than Get-Acl: measured on vm-pydev-win,
    # about 0.7 ms an entry against Get-Acl's 1.6 ms, which over a tree of thousands is the difference.
    # On PowerShell 7 (.NET Core) the method is an extension, called as the static it is.
    $sections = [Security.AccessControl.AccessControlSections]'Access, Owner'
    if ($PSVersionTable.PSEdition -ne 'Core') { return $Item.GetAccessControl($sections) }
    if ($Item -is [IO.DirectoryInfo]) { return [IO.FileSystemAclExtensions]::GetAccessControl([IO.DirectoryInfo]$Item, $sections) }
    return [IO.FileSystemAclExtensions]::GetAccessControl([IO.FileInfo]$Item, $sections)
}

function Get-PwshPyWindowsTreeDistrust {
    param([string] $Path)

    # Windows half of Get-PwshPyPythonTreeDistrust: every entry below $Path judged with the rights of
    # its kind (Get-PwshPyLeafDistrust), a link (symlink or junction) as the link object it is
    # (Get-PwshPyLinkDistrust) and never followed: its target inside the directory is judged where it
    # lies. uv's lock file inherits the directory's ACL here (measured with uv 0.13), so unlike POSIX
    # no entry is exempt. Measured on vm-pydev-win: about 1.7 s for one install (2,541 entries) under
    # both PowerShells, 7.5 s (pwsh 7) to 11.3 s (5.1) for five.
    $stack = New-Object System.Collections.Generic.Stack[IO.DirectoryInfo]
    $stack.Push((New-Object IO.DirectoryInfo $Path))
    while ($stack.Count -gt 0) {
        $directory = $stack.Pop()
        try {
            $children = @($directory.GetFileSystemInfos())
        } catch {
            return "the entries below $Path cannot be listed ($($_.Exception.Message))"
        }
        foreach ($child in $children) {
            if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                $why = Get-PwshPyLinkDistrust -Path $child.FullName
            } else {
                $isDirectory = $child -is [IO.DirectoryInfo]
                try {
                    $security = Read-PwshPySecurity -Item $child
                } catch {
                    return "the ACL of $($child.FullName) cannot be read ($($_.Exception.Message))"
                }
                $why = Get-PwshPyLeafDistrust -Path $child.FullName -Directory $isDirectory -Security $security
                if ($isDirectory) { $stack.Push([IO.DirectoryInfo]$child) }
            }
            if ($why) { return $why }
        }
    }
    return ''
}

function Get-PwshPyPythonTreeDistrust {
    param([string] $Path)

    # Why an entry below uv's managed-Python directory $Path (which exists) may not be run or read
    # elevated, or ''. `uv python find` runs every install it considers there, and each interpreter
    # reads its own standard library and site-packages as it starts (a .pth there runs code), so a file
    # or directory another account can modify anywhere below is as bad as the directory itself
    # (measured with uv 0.13: a .pth in a writable install's site-packages ran before any later check).
    # Each entry is judged like the directory holding uv; identical to the .sh runner on POSIX.
    if ($PwshPyOnWindows) { return (Get-PwshPyWindowsTreeDistrust -Path $Path) }
    return (Get-PwshPyPosixTreeDistrust -Path $Path)
}

function Get-PwshPyPythonDirDistrust {
    param([string] $Path)

    # Why uv's managed-Python directory $Path may not be searched elevated, or ''. `uv python find`
    # EXECUTES every interpreter it considers there to ask its version, so a .pth planted in one's
    # site-packages would run elevated before any check of the path uv answers (measured with uv
    # 0.13): the directory is judged before uv starts, as a uv is, itself and every directory above
    # it, and then everything below it (Get-PwshPyPythonTreeDistrust). One that does not exist yet is
    # judged by the nearest one that does, where `uv python install` will create it; it holds no
    # install yet.
    $directory = $Path
    while (-not (Test-Path -LiteralPath $directory)) {
        $parent = [IO.Path]::GetDirectoryName($directory)
        if (-not $parent) { break }
        $directory = $parent
    }
    if ($directory -eq [IO.Path]::GetPathRoot($directory)) {
        $why = Get-PwshPyLeafDistrust -Path $directory -Directory $true
    } else {
        $why = Get-PwshPyUvDistrust -Path $directory -Directory $true
    }
    if ($why -or $directory -ne $Path) { return $why }
    return (Get-PwshPyPythonTreeDistrust -Path $Path)
}

function Resolve-PwshPyManagedPython {
    param([string] $Uv)

    # The uv-managed Python an elevated run hands `uv run --python`; exits when there is none it may
    # run. uv run probes a .venv in its working directory and EVERY directory above it, and under
    # only-managed even runs the script on that venv's python (measured with uv 0.13.0), so neither
    # the caller's directory nor any ancestor of the anchor may decide: C:\ lets any authenticated
    # user create C:\.venv above System32. `uv python find --system` probes no .venv at all, and
    # `--python <path>` stops uv run probing. None found: installed the way uv run would download one
    # (not when UV_PYTHON_DOWNLOADS is never or manual, where uv run would not either), then found
    # again. The path must pass the same check as uv itself.
    $find = @('find', '--system', '--python-preference', 'only-managed')
    $found = Invoke-PwshPyUvPython -Uv $Uv -Arguments $find
    if ($found.Code -ne 0) {
        if (@('never', 'manual') -contains [string]$env:UV_PYTHON_DOWNLOADS) {
            Write-PwshPyFailure $found.Err
            exit $found.Code
        }
        # --no-bin and --no-registry: uv run's own download neither links a pythonX.Y into
        # ~/.local/bin nor registers the install in the Windows registry, so neither does this.
        $install = @('install', '--no-bin')
        if ($PwshPyOnWindows) { $install += '--no-registry' }
        $installed = Invoke-PwshPyUvPython -Uv $Uv -Arguments $install
        if ($installed.Err) { Write-PwshPyNotice $installed.Err }
        if ($installed.Code -ne 0) { exit $installed.Code }
        $found = Invoke-PwshPyUvPython -Uv $Uv -Arguments $find
        if ($found.Code -ne 0) {
            Write-PwshPyFailure $found.Err
            exit $found.Code
        }
    }
    $python = $found.Out
    if (-not (Test-PwshPyAbsolutePath -Path $python)) {
        Write-PwshPyFailure "pwshpy: uv named no absolute path for its Python: $python"
        exit 70
    }
    $why = Get-PwshPyUvDistrust -Path $python
    if ($why) {
        Write-PwshPyFailure "pwshpy: refusing the Python uv found at ${python}: $why (an elevated run only runs a Python no other account can modify): $(Get-PwshPyPythonRemedy)"
        exit 127
    }
    return $python
}

function Get-PwshPyAnchoredUvArg {
    param([string] $CallerDirectory)

    # The uv args with each relative --with path made absolute against the caller's directory: the
    # value's head (a 'name @ ', or nothing) stays, the directory goes in front of the relative tail.
    $prefix = $CallerDirectory.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $anchored = New-Object System.Collections.Generic.List[string]
    for ($index = 0; $index -lt $PwshPyUvArgs.Count; $index++) {
        $value = [string]$PwshPyUvArgs[$index]
        $relative = [string]$PwshPyUvArgsRelative[$index]
        if (-not $relative) { $anchored.Add($value); continue }
        $anchored.Add($value.Substring(0, $value.Length - $relative.Length) + $prefix + $relative)
    }
    return $anchored.ToArray()
}

function Get-PwshPyCallerDirectory {
    # The directory the packed script must run in: PowerShell's current FileSystem location, which an
    # elevated child inherits as its working directory from the parent that started it.
    return (Get-Location -PSProvider FileSystem).ProviderPath
}

function Clear-PwshPyInterpreterVariable {
    # Drops each interpreter variable that is set, naming it on stderr, so a run that ignored it says why.
    # Removed through the Env: drive, not [Environment]::SetEnvironmentVariable($name, $null): on pwsh
    # 7.6 for Linux that leaves the variable in a child's environment as an empty string (measured),
    # which uv does not read as unset for every variable.
    foreach ($name in $PwshPyInterpreterVariables) {
        if ($null -eq [Environment]::GetEnvironmentVariable($name)) { continue }
        Write-PwshPyNotice "pwshpy: ignoring ${name}: an elevated run only runs a uv-managed Python from the running account's own data directory"
        Remove-Item -LiteralPath "Env:$name"
    }
    foreach ($name in $PwshPyLibraryVariables) {
        if ($null -eq [Environment]::GetEnvironmentVariable($name)) { continue }
        Write-PwshPyNotice "pwshpy: ignoring ${name}: an elevated run runs the Python with its own library paths and no user site-packages"
        Remove-Item -LiteralPath "Env:$name"
    }
    # Without PYTHONUSERBASE the user site-packages comes from APPDATA (Windows) or HOME (POSIX, which
    # some sudo setups keep), so a .pth the caller put there would run elevated (measured): off.
    Set-Item -LiteralPath 'Env:PYTHONNOUSERSITE' -Value '1'
    foreach ($name in $PwshPyUvSourceVariables) {
        if ($null -eq [Environment]::GetEnvironmentVariable($name)) { continue }
        Write-PwshPyNotice "pwshpy: ignoring ${name}: an elevated run lets uv use its defaults for package sources, caches and env files"
        Remove-Item -LiteralPath "Env:$name"
    }
    foreach ($name in $PwshPyNetworkVariables) {
        if ($null -eq [Environment]::GetEnvironmentVariable($name)) { continue }
        Write-PwshPyNotice "pwshpy: ignoring ${name}: an elevated run lets uv connect with its own defaults: no proxy, its built-in certificate roots, no keyring"
        Remove-Item -LiteralPath "Env:$name"
    }
}

function Get-PwshPyPasswdHome {
    param([string] $Entry, [string] $Uid)

    # The home in one passwd entry, or '' when the entry is not this uid's or names no absolute home.
    # It is the second-to-last field both in the 7-field passwd form and in the 10-field
    # master.passwd form BSD's id -P prints.
    $fields = ([string]$Entry).Trim() -split ':'
    if ($fields.Count -lt 7 -or $fields[2] -ne $Uid) { return '' }
    $accountHome = $fields[$fields.Count - 2]
    if (-not $accountHome.StartsWith('/')) { return '' }
    return $accountHome
}

function Get-PwshPyPasswdAnswer {
    param([string] $FilePath, [string[]] $ArgumentList)

    # The lines one passwd source prints (getent, id -P), or none when it cannot run: GNU id has no
    # -P, and a source that fails is simply not an answer - the next source is asked, and no answer
    # at all makes Get-PwshPyAccountHome throw.
    try {
        return @(. (ConvertTo-PwshPyNativeCall -FilePath $FilePath -ArgumentList $ArgumentList -StderrPath '/dev/null'))
    } catch {
        return @()
    }
}

function Get-PwshPyAccountKnownFolder {
    param([Environment+SpecialFolder] $Folder, [string] $Name)

    # Windows: the known folder $Folder of the running token (Environment.GetFolderPath, which asks the
    # shell, not the APPDATA or LOCALAPPDATA variable). No answer throws: a guessed directory would be
    # one the caller could create.
    $known = [Environment]::GetFolderPath($Folder)
    if (-not $known -or -not (Test-PwshPyAbsolutePath -Path $known)) {
        throw "cannot read the $Name known folder of the running account"
    }
    return $known
}

function Get-PwshPyAccountDataHome {
    # The data directory of the account this run is (uv keeps its managed Pythons in
    # <it>/uv/python), asked of the system rather than of the environment: APPDATA, HOME and
    # XDG_DATA_HOME are whatever the caller or the service that started it set. Windows: the
    # RoamingAppData known folder of the running token. POSIX: <home>/.local/share, the home from the
    # passwd database (Get-PwshPyAccountHome).
    if ($PwshPyOnWindows) { return (Get-PwshPyAccountKnownFolder -Folder ([Environment+SpecialFolder]::ApplicationData) -Name 'RoamingAppData') }
    return ((Get-PwshPyAccountHome) + '/.local/share')
}

function Get-PwshPyAccountCacheHome {
    # The directory below which uv keeps its default cache for the account this run is, asked of the
    # system for the reason the data directory is: Windows the LocalAppData known folder of the running
    # token (uv's cache is <it>\uv\cache), POSIX <home>/.cache from the passwd database (uv's cache is
    # <it>/uv). LOCALAPPDATA, XDG_CACHE_HOME and HOME are the caller's to set.
    if ($PwshPyOnWindows) { return (Get-PwshPyAccountKnownFolder -Folder ([Environment+SpecialFolder]::LocalApplicationData) -Name 'LocalAppData') }
    return ((Get-PwshPyAccountHome) + '/.cache')
}

function Get-PwshPyAccountHome {
    # POSIX: the home of the account this run is (no trailing slash; '' for /), from the passwd
    # database: getent asks every name service (Linux), id -P is the BSD and macOS form, and
    # /etc/passwd itself lists root everywhere; the tools come from the fixed helper directories, never
    # from PATH. No answer throws: a guessed directory would be one the caller could create.
    $uid = Get-PwshPyEuid
    $answers = New-Object System.Collections.Generic.List[string]
    foreach ($directory in ($PwshPyFixedPath -split ':')) {
        $getent = "$directory/getent"
        if (-not (Test-Path -LiteralPath $getent -PathType Leaf)) { continue }
        $answers.AddRange([string[]]@(Get-PwshPyPasswdAnswer -FilePath $getent -ArgumentList @('passwd', $uid)))
        break
    }
    foreach ($id in $PwshPyFixedIds) {
        if (-not (Test-Path -LiteralPath $id -PathType Leaf)) { continue }
        $answers.AddRange([string[]]@(Get-PwshPyPasswdAnswer -FilePath $id -ArgumentList @('-P')))
        break
    }
    if (Test-Path -LiteralPath '/etc/passwd' -PathType Leaf) { $answers.AddRange([IO.File]::ReadAllLines('/etc/passwd')) }
    foreach ($answer in $answers) {
        $accountHome = Get-PwshPyPasswdHome -Entry $answer -Uid $uid
        if ($accountHome) { return $accountHome.TrimEnd('/') }
    }
    throw "cannot read the home directory of uid $uid from the passwd database"
}

function Invoke-PwshPyElevated {
    param([string[]] $ForwardArgs)

    # This function EXITS rather than returning a code, deliberately. A PowerShell function
    # returns everything written to the output stream, so returning a code from a function
    # that also runs a child would swallow the child's stdout into the return value and
    # print nothing. Exiting here keeps the child's output flowing straight to the console.
    #
    # A refused elevation exits 77 (EX_NOPERM), so a caller can tell it from the script's own
    # exit codes; the fixed stderr line covers a script that itself exits 77.
    $self = $PSCommandPath
    if (-not $PwshPyOnWindows) {
        # No sudo is a missing tool (127), not a refused elevation (77).
        if (-not (Get-Command sudo -CommandType Application -ErrorAction SilentlyContinue)) {
            [Console]::Error.WriteLine('pwshpy: sudo not found; cannot elevate')
            exit 127
        }
        # Ask for the password with `sudo -v` first: sudo exits 1 when it refuses, which is also
        # an ordinary exit code of the script, so only asking separately tells the two apart.
        & sudo -v
        if ($LASTEXITCODE -ne 0) {
            [Console]::Error.WriteLine('pwshpy: elevation declined (sudo did not grant root)')
            exit 77
        }
        # sudo keeps the console, so output and exit code flow back without a relay. A sudo that
        # grants -v but then refuses the command itself (timestamp_timeout=0, sudoers allowing
        # only -v) exits with that sudo's own code, which no wrapper can tell from the script's.
        # Every word literal (ConvertTo-PwshPyNativeCall): the caller's '*' or '~' reaches the
        # script as typed, and a runner path holding '?' names this file, not a sibling it matches.
        $sudoArgs = @((Get-PwshPyHostPath), '-NoProfile', '-File', $self)
        if ($null -ne $ForwardArgs) { $sudoArgs += $ForwardArgs }
        . (ConvertTo-PwshPyNativeCall -FilePath 'sudo' -ArgumentList $sudoArgs)
        exit $LASTEXITCODE
    }

    # A UAC child cannot inherit this console and a shell-executed start cannot redirect its
    # streams, so the child writes them to files we replay here.
    $relay = Join-Path ([IO.Path]::GetTempPath()) ('pwshpy-pack-' + [Guid]::NewGuid().ToString('N'))
    # The caller's own -ExecutionPolicy Bypass lives only in its PSExecutionPolicyPreference
    # variable, and a UAC start builds the elevated environment fresh from the profile, so it
    # never reaches the child: on a default client (5.1 policy Restricted) the child would
    # refuse to load this file. The caller already chose to run it; Group Policy still wins.
    $childArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $self, '-PwshPyRelay', $relay)
    # Only a missing list is skipped: an empty-string argument is real and must reach the child.
    if ($null -ne $ForwardArgs) { $childArgs += $ForwardArgs }
    # Process.Start rather than Start-Process -Verb RunAs: Start-Process rethrows a refused prompt
    # as a message-only InvalidOperationException, which drops the Win32Exception 1223 that is
    # the only locale-independent sign of a refusal. Every argument is quoted (a temp or script
    # path under C:\Users\John Doe would otherwise split in two). The working directory has to
    # be set by hand: Start-Process fills in the current FileSystem location, while a bare
    # ProcessStartInfo starts the child in the process's own directory, which Set-Location never
    # moves.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = Get-PwshPyHostPath
    $startInfo.Arguments = Join-PwshPyCommandLine -Arguments $childArgs
    $startInfo.WorkingDirectory = (Get-Location -PSProvider FileSystem).ProviderPath
    $startInfo.UseShellExecute = $true
    $startInfo.Verb = 'runas'
    try {
        $process = [System.Diagnostics.Process]::Start($startInfo)
    } catch {
        if (Test-PwshPyUacDeclined -Failure $_.Exception) {
            [Console]::Error.WriteLine('pwshpy: elevation declined (the Administrator prompt was refused)')
            exit 77
        }
        throw
    }
    if ($null -eq $process) { throw 'the elevated PowerShell did not start' }
    $process.WaitForExit()
    # Read with ReadAllText, not Get-Content: the child's own failure text is UTF-8 without a BOM,
    # which Get-Content on 5.1 decodes as the ANSI code page (a German error message would arrive
    # mangled), while the script's redirected streams carry a BOM that ReadAllText honours.
    $relayText = @{}
    foreach ($suffix in @('out', 'note', 'err')) {
        $path = "$relay.$suffix"
        $relayText[$suffix] = $null
        if (Test-Path -LiteralPath $path) {
            $relayText[$suffix] = [IO.File]::ReadAllText($path)
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
    }
    # The child writes .out only when it starts the script, and .err earlier only to say why it
    # stopped, so a failure with neither means it never ran a line that could tell us (refused by
    # the execution policy, a window closed early). Its own window is gone by now, so say so
    # rather than exit with a bare code and nothing on screen. The exit code is left unchanged.
    if ($process.ExitCode -ne 0 -and $null -eq $relayText['out'] -and -not $relayText['err'] -and -not $relayText['note']) {
        [Console]::Error.WriteLine("pwshpy: the elevated child produced no output and exited $($process.ExitCode) (it stopped before running the script, e.g. refused by the execution policy or its window was closed)")
    }
    # Notices first: they were written before the script started (which uv was skipped, and why).
    if ($relayText['note']) { [Console]::Error.Write($relayText['note']) }
    if ($relayText['out']) { Write-Host $relayText['out'] -NoNewline }
    # The child's stderr goes back to stderr, so a caller capturing stdout gets only the script's
    # output, and an early failure reads as the error it is.
    if ($relayText['err']) { [Console]::Error.Write($relayText['err']) }
    exit $process.ExitCode
}

function Write-PwshPyFailure {
    param([string] $Message)

    # An elevated child's console is a window of its own that closes the moment it exits, so a
    # line printed there is never seen: it goes to the relay file the parent replays instead.
    if ($PwshPyRelay) {
        [IO.File]::AppendAllText("$PwshPyRelay.err", $Message + [Environment]::NewLine)
        return
    }
    [Console]::Error.WriteLine($Message)
}

function Write-PwshPyHelp {
    # Printed from the file's own header rather than through Get-Help: comment-based help
    # resolution varies with host and file layout, and an artefact handed to someone else
    # must explain itself the same way everywhere.
    foreach ($line in (Get-Content -LiteralPath $PSCommandPath)) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '#>') { break }
        # Skip the delimiters and the comment-based-help keywords, which are there so that
        # Get-Help and editors understand the block; the prose below them stands on its own.
        if ($trimmed -eq '<#' -or $trimmed -like '#Requires*') { continue }
        if ($trimmed -eq '.SYNOPSIS' -or $trimmed -eq '.DESCRIPTION') { continue }
        Write-Host $line
    }
}

function Write-PwshPyManifest {
    param([string] $CacheDir)

    Write-Host "entry      : $PwshPyEntry"
    Write-Host "payload    : sha256 $PwshPySha"
    Write-Host "uv args    : $($PwshPyUvArgs -join ' ')"
    Write-Host "cache dir  : $CacheDir"
    Write-Host 'files      :'
    foreach ($line in ($PwshPyFileHashes -split "`n")) {
        $entry = $line.Trim()
        if ($entry) { Write-Host ('  ' + ($entry -split ' \*', 2)[1]) }
    }
}

# --- argument split ---------------------------------------------------------------------
# Wrapper switches are consumed here; everything else is forwarded untouched.

$PwshPyClean = $false
$PwshPyNoInstallUv = $false
$PwshPyWantElevate = $false
$PwshPyInfo = $false
$PwshPyHelp = $false
$PwshPyRelay = ''
$PwshPyUvPin = ''
$PwshPyRest = New-Object System.Collections.ArrayList
# The consumed switches an elevated relaunch must pass on again; every other one is either
# handled before the relaunch (-PwshPyHelp, -PwshPyInfo) or set by it (-PwshPyRelay).
$PwshPyRelaunchSwitches = New-Object System.Collections.ArrayList
$PwshPyExpectRelay = $false
$PwshPyExpectUv = $false

foreach ($argument in $args) {
    $text = [string]$argument
    if ($PwshPyExpectRelay) { $PwshPyRelay = $text; $PwshPyExpectRelay = $false; continue }
    if ($PwshPyExpectUv) {
        $PwshPyUvPin = $text
        $PwshPyExpectUv = $false
        [void]$PwshPyRelaunchSwitches.Add('-PwshPyUv')
        [void]$PwshPyRelaunchSwitches.Add($text)
        continue
    }
    switch -Exact ($text) {
        '-PwshPyClean' { $PwshPyClean = $true; [void]$PwshPyRelaunchSwitches.Add($text); continue }
        '-PwshPyNoInstallUv' { $PwshPyNoInstallUv = $true; [void]$PwshPyRelaunchSwitches.Add($text); continue }
        '-PwshPyElevate' { $PwshPyWantElevate = $true; continue }
        '-PwshPyInfo' { $PwshPyInfo = $true; continue }
        '-PwshPyHelp' { $PwshPyHelp = $true; continue }
        '-PwshPyRelay' { $PwshPyExpectRelay = $true; continue }
        '-PwshPyUv' { $PwshPyExpectUv = $true; continue }
        default { [void]$PwshPyRest.Add($text) }
    }
}
# 64 (EX_USAGE): a malformed switch is neither a missing uv (127) nor the script's own exit code.
if ($PwshPyExpectUv) {
    [Console]::Error.WriteLine('pwshpy: -PwshPyUv needs the absolute path of a uv executable')
    exit 64
}
if ($PwshPyUvPin -and -not (Test-PwshPyAbsolutePath -Path $PwshPyUvPin)) {
    [Console]::Error.WriteLine("pwshpy: -PwshPyUv needs an absolute path, got: $PwshPyUvPin")
    exit 64
}
$PwshPyForward = @($PwshPyRest.ToArray())

# Everything up to the script run, as one block. Run in place (dot-sourced) it behaves exactly as
# if written inline; an elevated child runs it under a guard, because any error it raises there -
# a corrupt payload, an unwritable cache, a failed uv install - would otherwise be printed only in
# the child's own console window, which closes at once.
$PwshPyPrepare = {
    if ($PwshPyHelp) { Write-PwshPyHelp; exit 0 }

    $PwshPyCacheDir = Join-Path (Get-PwshPyCacheRoot) $PwshPySha.Substring(0, 16)

    if ($PwshPyInfo) { Write-PwshPyManifest -CacheDir $PwshPyCacheDir; exit 0 }

    if ($PwshPyWantElevate -and -not (Test-PwshPyElevated)) {
        exit (Invoke-PwshPyElevated -ForwardArgs (@($PwshPyRelaunchSwitches.ToArray()) + $PwshPyForward))
    }

    # Asked after any relaunch: the trust rule applies to whatever runs elevated, UAC, SYSTEM
    # (a scheduled task) or root alike. As root on POSIX every helper the runner calls (ls, the uv
    # installer's curl and sh) runs with full rights, so from here on they come from fixed system
    # directories, not from the PATH the caller handed over; that PATH is kept for the uv search
    # and handed back to the packed script. Windows needs no such reset: the runner calls no
    # external program there except uv and its own PowerShell, both by full path.
    $PwshPyElevatedRun = Test-PwshPyElevated
    # Script scope by name: Get-PwshPyUvPath, Install-PwshPyUv and the run below read it.
    $script:PwshPyCallerPath = $env:PATH
    $PwshPyRootPath = $PwshPyElevatedRun -and -not $PwshPyOnWindows
    if ($PwshPyRootPath) { $env:PATH = $PwshPyFixedPath }

    # --- unpack -----------------------------------------------------------------------------

    if ($PwshPyClean -and (Test-Path -LiteralPath $PwshPyCacheDir)) {
        Remove-Item -LiteralPath $PwshPyCacheDir -Recurse -Force
    }
    if (-not (Test-PwshPyTree -Root $PwshPyCacheDir)) {
        Invoke-PwshPyUnpackLocked -CacheDir $PwshPyCacheDir
    }

    # --- uv ---------------------------------------------------------------------------------

    if ($PwshPyOnWindows) {
        $PwshPyUvHint = 'An elevated run only runs a uv no account but this one, SYSTEM, Administrators, LOCAL SERVICE, NETWORK SERVICE and TrustedInstaller can modify, in its own directory or any directory above: install uv in this account''s own profile (the installer''s default) or where only administrators can write, or remove the other account''s access from the directory named above; a -PwshPyUv pin must pass the same check.'
    } else {
        $PwshPyUvHint = 'As root only a uv no other account can modify is run: install uv where only root can write (root''s own ~/.local/bin, the installer''s default), or take group and other write off the directory named above (chmod go-w); a -PwshPyUv pin must pass the same check.'
    }

    if ($PwshPyUvPin) {
        # A pin is the whole answer: no search and no install behind it, so a refused pin fails here.
        if (-not (Test-Path -LiteralPath $PwshPyUvPin -PathType Leaf)) {
            Write-PwshPyFailure "pwshpy: no uv at the path given with -PwshPyUv: $PwshPyUvPin"
            exit 127
        }
        if ($PwshPyElevatedRun) {
            $PwshPyPinWhy = Get-PwshPyUvDistrust -Path $PwshPyUvPin
            if ($PwshPyPinWhy) {
                Write-PwshPyFailure "pwshpy: refusing the uv given with -PwshPyUv: ${PwshPyUvPin}: $PwshPyPinWhy. $PwshPyUvHint"
                exit 127
            }
        }
        $PwshPyUv = $PwshPyUvPin
    } else {
        $PwshPyUv = Get-PwshPyUvPath -Elevated $PwshPyElevatedRun
    }
    if (-not $PwshPyUv) {
        if ($PwshPyNoInstallUv) {
            # Written by Write-PwshPyFailure rather than through Write-Error: with
            # $ErrorActionPreference = 'Stop' the error stream terminates the script on the spot,
            # and the caller would get PowerShell's exit 1 (or an elevated child's 70) instead of
            # the 127 meant here.
            if ($PwshPyElevatedRun) {
                Write-PwshPyFailure "pwshpy: no uv this elevated run can trust was found and -PwshPyNoInstallUv was given. $PwshPyUvHint"
            } else {
                Write-PwshPyFailure 'uv is not installed and -PwshPyNoInstallUv was given. Install it from https://astral.sh/uv'
            }
            exit 127
        }
        # Elevated, the installer writes into the running account's own profile, which only that
        # account, SYSTEM and Administrators may change, so a fresh install passes the trust rule
        # (under SYSTEM that profile is admin-only).
        Install-PwshPyUv
        $PwshPyUv = Get-PwshPyUvPath -Elevated $PwshPyElevatedRun
    }
    if (-not $PwshPyUv) {
        if ($PwshPyElevatedRun) {
            Write-PwshPyFailure "pwshpy: no uv this elevated run can trust was found after installation. $PwshPyUvHint"
        } else {
            Write-PwshPyFailure 'uv is still not on PATH after installation. Install it manually from https://astral.sh/uv'
        }
        exit 127
    }

    # Elevated: the interpreter, decided here, while a root run on POSIX still runs its helpers (the
    # passwd lookup, the ls the path check reads) from the fixed PATH, and inside the elevated child's
    # guard, so a failure is relayed. UV_PYTHON_INSTALL_DIR is named outright after the caller's own
    # choice was dropped: left to itself uv would look in the data home APPDATA (Windows) or
    # XDG_DATA_HOME (POSIX) names, which is the caller's to set too.
    if ($PwshPyElevatedRun) {
        Clear-PwshPyInterpreterVariable
        $PwshPyDataHome = Get-PwshPyAccountDataHome
        Set-Item -LiteralPath 'Env:UV_PYTHON_INSTALL_DIR' -Value (Join-Path (Join-Path $PwshPyDataHome 'uv') 'python')
        # The cache too, for the same reason: uv's default follows LOCALAPPDATA (Windows) or XDG_CACHE_HOME
        # and HOME (POSIX, which sudo -E or env_keep can hand over), and cached script environments and
        # wheels run elevated. uv's own layout below the folder: uv\cache on Windows, uv on POSIX.
        $PwshPyUvCache = Join-Path (Get-PwshPyAccountCacheHome) 'uv'
        if ($PwshPyOnWindows) { $PwshPyUvCache = Join-Path $PwshPyUvCache 'cache' }
        Set-Item -LiteralPath 'Env:UV_CACHE_DIR' -Value $PwshPyUvCache
        Write-PwshPyNotice "pwshpy: uv caches in ${PwshPyUvCache}: an elevated run uses the running account's own cache directory"
        # The directory before uv searches it: uv runs every Python it finds there.
        $PwshPyPythonDirWhy = Get-PwshPyPythonDirDistrust -Path $env:UV_PYTHON_INSTALL_DIR
        if ($PwshPyPythonDirWhy) {
            Write-PwshPyFailure "pwshpy: refusing uv's managed-Python directory $($env:UV_PYTHON_INSTALL_DIR): $PwshPyPythonDirWhy (an elevated run lets uv search only a directory no other account can modify, nor anything below it): $(Get-PwshPyPythonRemedy)"
            exit 127
        }
        # Script scope by name: the run preparation, a block of its own, reads it.
        $script:PwshPyPython = Resolve-PwshPyManagedPython -Uv $PwshPyUv
    }
}

# --- run --------------------------------------------------------------------------------
# The argument vector goes out of band: PowerShell 5.1 re-quotes arguments on their way to
# a native executable (empty strings vanish, embedded quotes are mangled) and only 7.3 fixed
# it. Base64 per argument sidesteps the quoting layer entirely, so the packed script sees
# exactly what the caller typed on every host.

# The argument vector, the target and the uv arguments, built inside the same guard as the preparation
# above: a failure here (resolving the target, reading the working directory, setting the variables
# the shim reads) in an elevated child would otherwise print only in its own window, which closes at
# once.
$PwshPyPrepareRun = {
    $PwshPyEncoded = @('v1')
    foreach ($argument in $PwshPyForward) {
        $PwshPyEncoded += [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$argument))
    }
    $env:PwshPyArgvPlaceholder = $null
    Set-Item -Path ('Env:' + $PwshPyArgvEnv) -Value ($PwshPyEncoded -join '|')

    $PwshPyTarget = Join-Path $PwshPyCacheDir $PwshPyShim
    # The packed script runs with the PATH its caller gave; the fixed one was only for the runner's own
    # helpers.
    if ($PwshPyRootPath) { $env:PATH = $PwshPyCallerPath }

    # uv picks the interpreter, and runs every one it considers to ask its version, so a python planted
    # in a PATH directory another account can write would run elevated. Elevated, uv runs the Python
    # Resolve-PwshPyManagedPython chose (a Python uv manages, user decision) and starts in the anchor
    # directory; the shim is told where to change back to. Everything uv is handed that the caller gave
    # relative to its own directory (a --with path, a relative PWSHPY_PACK_CACHE) is made absolute
    # first, or it would resolve against the anchor.
    $PwshPyRunArgs = @('run', '--no-project')
    if ($PwshPyElevatedRun) {
        $PwshPyRunArgs += Get-PwshPyManagedPythonOption -Python $script:PwshPyPython
        $PwshPyCallerCwd = Get-PwshPyCallerDirectory
        $PwshPyRunArgs += Get-PwshPyAnchoredUvArg -CallerDirectory $PwshPyCallerCwd
        $PwshPyTarget = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PwshPyTarget)
        Set-Item -LiteralPath ('Env:' + $PwshPyCwdEnv) -Value $PwshPyCallerCwd
    } else {
        $PwshPyRunArgs += $PwshPyUvArgs
        # Only an elevated run changes directory; one inherited from the caller's environment must not
        # move an unelevated script.
        if (Test-Path -LiteralPath ('Env:' + $PwshPyCwdEnv)) { Remove-Item -LiteralPath ('Env:' + $PwshPyCwdEnv) }
    }
    $PwshPyRunArgs += $PwshPyTarget
}

if ($PwshPyRelay) {
    try {
        # Created first, so a child that fails from here on has a file to say why in; the run
        # below truncates it again for the script's own stderr.
        [IO.File]::WriteAllText("$PwshPyRelay.err", '')
        . $PwshPyPrepare
        . $PwshPyPrepareRun
    } catch {
        # 70 (EX_SOFTWARE), so a caller can tell "the runner never started the script" from
        # PowerShell's generic 1; the stderr line covers a script that itself exits 70.
        Write-PwshPyFailure "pwshpy: the elevated child failed before running the script: $($_.Exception.Message) [$($_.CategoryInfo.Category)]"
        exit 70
    }
} else {
    . $PwshPyPrepare
    . $PwshPyPrepareRun
}

if ($PwshPyRelay) {
    # Elevated child: stream to the relay files the parent replays, then hand back the code.
    . (ConvertTo-PwshPyNativeCall -FilePath $PwshPyUv -ArgumentList $PwshPyRunArgs -StdoutPath "$PwshPyRelay.out" -StderrPath "$PwshPyRelay.err")
    exit $LASTEXITCODE
}

. (ConvertTo-PwshPyNativeCall -FilePath $PwshPyUv -ArgumentList $PwshPyRunArgs)
exit $LASTEXITCODE
