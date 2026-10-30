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
    UV_FIND_LINKS, UV_INSECURE_HOST, UV_INSECURE_NO_ZIP_VALIDATION, UV_CACHE_DIR,
    UV_PYTHON_CACHE_DIR, UV_ENV_FILE, HTTPS_PROXY, HTTP_PROXY, ALL_PROXY (and their lower-case
    forms), SSL_CERT_FILE, SSL_CERT_DIR, SSL_CLIENT_CERT, UV_NATIVE_TLS, UV_SYSTEM_CERTS,
    UV_KEYRING_PROVIDER, every variable whose name starts with GIT_ (git, which uv runs for a git
    source, reads them) or GCM_ (Git Credential Manager, the credential helper git runs, reads them;
    both on Windows in any case), SSH_ASKPASS and PASSWORD_STORE_DIR are ignored, each with a line on
    stderr; git reads no global config file (GIT_CONFIG_GLOBAL is /dev/null on POSIX and on Windows
    an empty file below the account's data directory, pwshpy\empty.gitconfig, refused with exit 127
    when another account could change it, so not the .gitconfig below the HOME, USERPROFILE or
    XDG_CONFIG_HOME it was given; git 2.32 or later, so for a script with a git source a git older
    than 2.32, which ignores that variable, is refused with exit code 127); the user site-packages is off
    (PYTHONNOUSERSITE=1) and uv run reads no .env file (--no-env-file). In a network that reaches
    the internet only through a proxy, an elevated run can download nothing: the managed Python and
    every package the script needs must already be in the account's directories named above.

    Set PWSHPY_PACK_CACHE to choose where the payload unpacks (default: a per-user cache dir). An
    elevated run unpacks and runs only from a cache directory that passes the same check as uv's
    managed-Python directory (itself, every directory above it and everything below it), or exits 127.

    Home: https://github.com/bitranox/pwshpy
#>

# No param() block and no [CmdletBinding()] here, deliberately. An advanced script binds
# PowerShell's common parameters, so a packed script's own -v / -d / -ea would be captured
# by -Verbose / -Debug / -ErrorAction and never reach Python. Hand-parsing $args keeps
# every argument the caller typed.

$ErrorActionPreference = 'Stop'
# Every native call here is judged by its exit code, and several fail as a matter of course (the
# no-password probe, a script's own exit code). A caller's profile setting this to $true would turn each
# such exit into a terminating error under 'Stop'. Hosts without the feature ignore the variable.
$PSNativeCommandUseErrorActionPreference = $false

$PwshPyEntry = @@PWSHPY_ENTRY@@
$PwshPyShim = '@@PWSHPY_SHIM@@'
$PwshPySha = '@@PWSHPY_PAYLOAD_SHA256@@'
$PwshPyArgvEnv = '@@PWSHPY_ARGV_ENV@@'
$PwshPyUvArgs = @(@@PWSHPY_UV_ARGS@@)
$PwshPyCwdEnv = '@@PWSHPY_CWD_ENV@@'
# An elevated run hands the shim the caller's whole PATH and the PATH uv was started on, so the script
# gets the first back behind the entries uv put in front of the second ([107]).
$PwshPyPathEnv = '@@PWSHPY_PATH_ENV@@'
$PwshPyUvPathEnv = '@@PWSHPY_UV_PATH_ENV@@'
# An elevated POSIX run names here the home variables it replaced for uv and git with the account's own,
# each caller value in <this>_<NAME>, so the shim gives the script the caller's back ([159]).
$PwshPyCallerEnv = '@@PWSHPY_CALLER_ENV@@'
# Per uv arg, the relative path an elevated run (which starts uv in another directory) puts the
# caller's directory in front of: '' for a value left as it is. Parallel to $PwshPyUvArgs.
$PwshPyUvArgsRelative = @(@@PWSHPY_UV_ARGS_RELATIVE@@)
# The packed script's requires-python ('' for none): the request an elevated run hands `uv python find`.
$PwshPyPythonRequest = @@PWSHPY_PYTHON_REQUEST@@
# $true when the packed script has a git source (pack-time detection, which errs towards $true): an
# elevated run then refuses a git older than 2.32 ([156]).
$PwshPyGitSource = @@PWSHPY_GIT_SOURCE@@

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

function Invoke-PwshPyChmod {
    param([string[]] $Arguments)

    # POSIX root run: chmod from the fixed helper PATH, failing loudly (a mode left as the umask made
    # it would be refused by the trust check later, with a reason that hides this one). Callers put
    # '--' before the mode: BSD chmod (macOS) stops parsing options at the mode, so a '--' after it
    # is taken for a file name; GNU chmod permutes its arguments and would hide the mistake.
    & chmod @Arguments
    if ($LASTEXITCODE -ne 0) { throw "chmod $($Arguments -join ' ') exited $LASTEXITCODE" }
}

function Initialize-PwshPyDirectory {
    param([string] $Path, [bool] $Private = $false)

    # Creates the directory $Path and any missing parent. An elevated POSIX run decides the modes of
    # what it creates ([R6]): 0755 each, 0700 for $Path when $Private, whatever the caller's umask (002
    # on a host with per-user groups would make them group-writable, which the trust check then refuses,
    # on this run and every later one). Created with that mode where .NET can (7 and later), so no
    # moment exists in which another account could add an entry; otherwise set right after creation.
    if (-not $PwshPyRootPath) {
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
        return
    }
    $missing = New-Object System.Collections.Generic.List[string]
    $probe = $Path
    while ($probe -and -not (Test-Path -LiteralPath $probe)) {
        $missing.Insert(0, $probe)
        $probe = Split-Path -Parent $probe
    }
    $modeType = 'System.IO.UnixFileMode' -as [type]
    foreach ($directory in $missing) {
        $octal = if ($Private -and $directory -eq $Path) { '700' } else { '755' }
        if ($modeType) {
            [void][IO.Directory]::CreateDirectory($directory, [Convert]::ToInt32($octal, 8) -as $modeType)
        } else {
            [void][IO.Directory]::CreateDirectory($directory)
        }
        Invoke-PwshPyChmod -Arguments @('--', $octal, $directory)
    }
}

function Expand-PwshPyPayload {
    param([string] $Destination)

    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
    # Private while it is written: no other account can reach a file in it before its mode is set.
    Initialize-PwshPyDirectory -Path $Destination -Private $true

    $archive = Join-Path $Destination '.payload.zip'
    [IO.File]::WriteAllBytes($archive, (Get-PwshPyPayload))
    # System.IO.Compression.FileSystem is preloaded on PowerShell 7 but not on 5.1.
    try { $null = [System.IO.Compression.ZipFile] } catch { Add-Type -AssemblyName System.IO.Compression.FileSystem }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($archive, $Destination)
    Remove-Item -LiteralPath $archive -Force
    if ($PwshPyRootPath) {
        # What the extraction created took the caller's umask ([R6]); no group or other write anywhere.
        Invoke-PwshPyChmod -Arguments @('-R', '--', 'go-w', $Destination)
        Invoke-PwshPyChmod -Arguments @('--', '755', $Destination)
    }
}

function Invoke-PwshPyUnpackLocked {
    param([string] $CacheDir)

    # Serialize extraction across processes with an exclusive lock file next to the cache dir, so
    # two cold-start runs of the same pack cannot stomp each other's extraction: without it, one
    # process's Remove-Item + re-extract can yank the directory another is already running from.
    # The warm path (a verified tree) never reaches here, so a normal run pays no lock cost.
    $parent = Split-Path -Parent $CacheDir
    Initialize-PwshPyDirectory -Path $parent
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
            # The lock file, too, took the caller's umask ([R6]).
            if ($PwshPyRootPath) { Invoke-PwshPyChmod -Arguments @('--', 'go-w', $lockPath) }
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
# Where uv takes packages (an index, a find-links location, a host it skips TLS checks for), how it
# checks a wheel's archive (UV_INSECURE_NO_ZIP_VALIDATION turns that off), its caches and an env file
# from: each is the caller's to choose, and an env file can set PYTHONPATH again (uv run loads
# UV_ENV_FILE despite --no-config, measured with uv 0.13). An elevated run drops them and uses uv's
# defaults (user decision, X21); its uv run also passes --no-env-file, which no variable undoes.
# Identical to the .sh runner.
$PwshPyUvSourceVariables = @(
    'UV_INDEX_URL', 'UV_DEFAULT_INDEX', 'UV_INDEX', 'UV_EXTRA_INDEX_URL', 'UV_FIND_LINKS', 'UV_INSECURE_HOST',
    'UV_INSECURE_NO_ZIP_VALIDATION', 'UV_CACHE_DIR', 'UV_PYTHON_CACHE_DIR', 'UV_ENV_FILE'
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

function Get-PwshPyLinkTargetKind {
    param([string] $Target, [string] $LinkType)

    # What the link target $Target (as read off .Target, the \??\ prefix gone) names: 'absolute' (a
    # drive path or a UNC path, Test-PwshPyAbsolutePath), 'relative' (to the link's own directory) or
    # 'unplaceable'. POSIX: absolute or relative. Windows: relative only when plainly so - no leading \
    # or /, no ':', and not a device name .Target spells without its \??\ (Volume{GUID}\..., GLOBALROOT\...,
    # UNC\...). Anything else names a place relative to the current drive (\Users\...), or one the
    # object manager resolves (a volume GUID, a device), which no walk here can judge, so it is
    # refused rather than joined below the link as before ([R1], spellings measured with mklink on a
    # Windows 11 VM, identical under pwsh 7.6 and Windows PowerShell 5.1). A junction's target is always
    # absolute; a junction with any other is refused too.
    if (Test-PwshPyAbsolutePath -Path $Target) { return 'absolute' }
    if (-not $PwshPyOnWindows) { return 'relative' }
    if ($LinkType -eq 'Junction') { return 'unplaceable' }
    if ($Target.StartsWith('\') -or $Target.StartsWith('/') -or $Target.Contains(':')) { return 'unplaceable' }
    foreach ($device in @('Volume{', 'GLOBALROOT\', 'GLOBALROOT/', 'UNC\', 'UNC/')) {
        if ($Target.StartsWith($device, [StringComparison]::OrdinalIgnoreCase)) { return 'unplaceable' }
    }
    return 'relative'
}

function Get-PwshPyUnplaceableReason {
    param([string] $Link, [string] $Target)

    # The reason a link whose target Get-PwshPyLinkTargetKind calls unplaceable is refused for.
    return "the link $Link points to $Target, which names no drive or share path (an elevated run follows only a link whose target is a drive path, a UNC path or relative to the link)"
}

function Get-PwshPyNetworkDistrust {
    param([string] $Path)

    # Windows: why an elevated run must not trust $Path for where it lies, or ''. A UNC path
    # (\\server\share, \\?\UNC\ or \\.\UNC\, either separator) is judged by the ACL its server reports,
    # and whoever runs that server decides the content whatever the ACL claims ([146]). Of the device
    # namespace (\\?\, \\.\) only \\?\C:\..., the long spelling of a drive path, is left to the ACL
    # walk: any other name there (\\?\GLOBALROOT\Device\Mup\..., a volume GUID, a pipe) is resolved by
    # the object manager, can reach a network redirector, and is never where uv or a PATH directory
    # lives. A drive letter is judged by its drive type ([147]): one mapped to a share (net use, a
    # logon script) reads DRIVE_REMOTE, [IO.DriveInfo] 'Network', on pwsh 7.6 and Windows PowerShell
    # 5.1 alike (measured on a Windows 11 test VM), while its ACL is again whatever the server reports.
    if (-not $PwshPyOnWindows) { return '' }
    $spelled = $Path.Replace('/', '\')
    if ($spelled -match '^(\\\\\?\\)?([A-Za-z]):\\') { return (Get-PwshPyDriveDistrust -Letter $Matches[2]) }
    if (-not $spelled.StartsWith('\\')) { return '' }
    if ($spelled -match '^\\\\[?.]\\' -and $spelled -notmatch '^\\\\[?.]\\UNC(\\|$)') {
        return 'it is a device path, and an elevated run trusts only local drive paths'
    }
    return (Get-PwshPyNetworkReason)
}

function Get-PwshPyDriveDistrust {
    param([string] $Letter)

    # Windows: the network rule's reason when the drive $Letter is a network drive, else ''. A drive
    # whose type cannot be read is refused too: the check fails closed.
    try {
        $type = (New-Object IO.DriveInfo $Letter).DriveType
    } catch {
        return "the type of drive ${Letter}: cannot be read ($($_.Exception.Message))"
    }
    if ($type -eq [IO.DriveType]::Network) { return (Get-PwshPyNetworkReason) }
    return ''
}

function Get-PwshPyNetworkReason {
    # The reason Get-PwshPyNetworkDistrust gives for a network path, kept in one place because the
    # refusals below the account's data directory pick their remedy by it
    # (Test-PwshPyBareNetworkReason).
    return 'it is a network path, and an elevated run trusts only local paths'
}

function Test-PwshPyBareNetworkReason {
    param([string] $Why)

    # Whether the refusal $Why is the network rule's own reason and nothing else, which is what the walk
    # returns when the path itself (the redirected data directory) is on a share. A link met below a
    # local data directory is refused as "the link L points to T: <reason>" and is not matched: the
    # data directory is not redirected then, and removing the link (the usual remedy) works. Compared
    # ordinally with our own fixed text, never with a localised system message.
    return [string]::Equals($Why, (Get-PwshPyNetworkReason), [StringComparison]::Ordinal)
}

function Get-PwshPyNetworkRemedy {
    # What an elevated run says in place of a remedy when a path below the running account's data
    # directory (the RoamingAppData known folder, see Get-PwshPyAccountDataHome) failed the network
    # rule for being on a share itself, not through a link met on the way. Folder redirection can put
    # that folder on a share, and then neither remedy the run names otherwise (take write access off,
    # remove the directory and run again) can work: the next run meets the same share ([146]).
    return 'the running account''s AppData is on a network share; an elevated run cannot use it'
}

function Get-PwshPyAclDistrust {
    param([string] $Path, [int] $Rights, [object] $Security = $null, [string[]] $Trusted = $null)

    # Returns why another account could change $Path through any of $Rights, or '' when none can.
    # The owner counts as a writer whatever the ACL says, because an owner may always rewrite it.
    # $Security is the path's security descriptor and $Trusted the trusted SIDs when the caller already
    # has them (the tree walk, see Get-PwshPyWindowsTreeDistrust); otherwise both are asked for here.
    $trusted = if ($Trusted) { $Trusted } else { Get-PwshPyTrustedSid }
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
    param([string] $Path, [bool] $Directory)

    if (-not $PwshPyOnWindows) { return (Get-PwshPyModeDistrust -Path $Path -Ancestor $false) }
    $rights = if ($Directory) { $PwshPyDirectoryWriteRights } else { $PwshPyFileWriteRights }
    return (Get-PwshPyAclDistrust -Path $Path -Rights $rights)
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

function ConvertFrom-PwshPyDriveLongPath {
    param([string] $Path)

    # Windows: $Path (backslashes) with the extended-length prefix of a drive path dropped, \\?\C:\x as
    # C:\x, the same file ([148]). Windows PowerShell 5.1 finds no drive in the long spelling, so its
    # Join-Path fails on every component and a trusted directory would be left off; pwsh 7 reads both.
    # Only the drive form: any other \\?\ or \\.\ name is refused by Get-PwshPyNetworkDistrust first.
    if ($Path -match '^\\\\\?\\[A-Za-z]:\\') { return $Path.Substring(4) }
    return $Path
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
    # Before any ACL is read: a share's ACL is whatever its server says. Every later root comes from
    # a link target, judged below.
    $network = Get-PwshPyNetworkDistrust -Path $Path
    if ($network) { return $network }
    if ($PwshPyOnWindows) { $Path = ConvertFrom-PwshPyDriveLongPath -Path $Path.Replace('/', '\') }
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
            # carry the NT prefix. A symlink to a share reads \\server\share on 7 but
            # UNC\server\share on 5.1 (measured), which would pass for a relative target there, so
            # 5.1 takes a target spelled UNC\... as the share it names; a relative link into a
            # directory named UNC is refused with it, which fails safe.
            $target = ([string](@($item.Target)[0])) -replace '^\\\?\?\\', ''
            if ($PSVersionTable.PSEdition -ne 'Core') { $target = $target -replace '^UNC\\', '\\' }
            if (-not $target) { return "the symlink $next cannot be read" }
            $kind = Get-PwshPyLinkTargetKind -Target $target -LinkType $linkType
            if ($kind -eq 'unplaceable') { return (Get-PwshPyUnplaceableReason -Link $next -Target $target) }
            if ($kind -eq 'absolute') {
                $network = Get-PwshPyNetworkDistrust -Path $target
                if ($network) { return "the link $next points to ${target}: $network" }
                if ($PwshPyOnWindows) { $target = ConvertFrom-PwshPyDriveLongPath -Path $target.Replace('/', '\') }
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

function Get-PwshPyTrustedPath {
    param([string] $Path)

    # The directories of the PATH $Path an elevated run lets uv see, in order, each one left out named
    # on stderr. uv runs git for a git dependency (and a build backend may too) by searching PATH, so a
    # directory another account can write would decide what runs elevated ([107]); each is judged by the
    # same walk as a uv, with '.' appended so the walk judges the directory itself as the leaf. An
    # empty entry is dropped silently (a trailing separator is common on Windows); a relative one names
    # the working directory and is left out like any untrusted one.
    $kept = New-Object System.Collections.Generic.List[string]
    foreach ($directory in ($Path -split [IO.Path]::PathSeparator)) {
        if (-not $directory) { continue }
        try {
            $why = Get-PwshPyUvDistrust -Path ($directory + [IO.Path]::DirectorySeparatorChar + '.') -Directory $true
        } catch {
            # Windows PowerShell 5.1 refuses a path holding a character such as '"' outright.
            $why = "$directory cannot be judged ($($_.Exception.Message))"
        }
        if ($why) {
            Write-PwshPyNotice "pwshpy: leaving $directory off uv's PATH: $why (an elevated run lets uv run git and other tools only from directories no other account can modify)"
            continue
        }
        $kept.Add($directory)
    }
    return ($kept -join [IO.Path]::PathSeparator)
}

function Get-PwshPyGitCandidate {
    param([string] $Path)

    # Every git file in the first directory on the PATH $Path that holds one, in the order uv tries
    # them, or none. Looked up by name and never run; Get-Command would also answer with an alias or
    # function of this session, and read '[', '*' and '?' in an entry as wildcards. POSIX: an
    # executable 'git', its mode read with the fixed-PATH ls. Windows: the names uv tries
    # (Get-PwshPyWindowsGitName). uv takes one of them, which one depends on what each file is (a bare
    # name only when it is a program), so an elevated run judges them all ([R2]).
    $names = if ($PwshPyOnWindows) { Get-PwshPyWindowsGitName } else { @('git') }
    foreach ($directory in ($Path -split [IO.Path]::PathSeparator)) {
        if (-not $directory) { continue }
        $found = New-Object System.Collections.Generic.List[string]
        foreach ($name in $names) {
            $candidate = $directory + [IO.Path]::DirectorySeparatorChar + $name
            try {
                if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
            } catch {
                continue
            }
            if (-not $PwshPyOnWindows -and -not (Test-PwshPyExecutable -Path $candidate)) { continue }
            if ($PwshPyOnWindows) { $candidate = Get-PwshPyOnDiskSpelling -Path $candidate }
            # A PATHEXT entry written twice in different case names the same file once.
            $known = $false
            foreach ($seen in $found) {
                if ([string]::Equals($seen, $candidate, [StringComparison]::OrdinalIgnoreCase)) { $known = $true }
            }
            if (-not $known) { $found.Add($candidate) }
        }
        if ($found.Count -gt 0) { return $found.ToArray() }
    }
    return @()
}

function Get-PwshPyGitOn {
    param([string] $Path)

    # The first git file Get-PwshPyGitCandidate finds on the PATH $Path, or ''.
    $candidates = @(Get-PwshPyGitCandidate -Path $Path)
    if ($candidates.Count -gt 0) { return $candidates[0] }
    return ''
}

function Get-PwshPyOnDiskSpelling {
    param([string] $Path)

    # Windows: the existing file $Path with its name spelled as the directory holds it ('git.exe' for a
    # lookup of git.EXE, which PATHEXT spells upper-case), so a message names the file as a listing
    # shows it. A name with a wildcard character is no file name on Windows and never gets here.
    $directory = [IO.Path]::GetDirectoryName($Path)
    $name = [IO.Path]::GetFileName($Path)
    if ($name.IndexOfAny([char[]]'*?') -ge 0) { return $Path }
    try {
        foreach ($entry in [IO.Directory]::GetFiles($directory, $name)) {
            if ([string]::Equals([IO.Path]::GetFileName($entry), $name, [StringComparison]::OrdinalIgnoreCase)) {
                return (Join-Path $directory ([IO.Path]::GetFileName($entry)))
            }
        }
    } catch {
        return $Path
    }
    return $Path
}

function Get-PwshPyWindowsGitName {
    # The file names, in order, under which uv finds git in one PATH directory on Windows ([R2]; uv
    # finds it with the which crate, 8.0.0: PathsIter in src/finder.rs): the bare 'git' first, taken
    # only when it is a program (GetBinaryType), then 'git' with each PATHEXT entry appended as
    # written, in PATHEXT order (an entry without its dot names a file nobody has). With PATHEXT unset
    # or empty only the bare name. So a git.cmd ahead of git.exe in PATHEXT, or in an earlier
    # directory, is the git uv runs.
    $names = New-Object System.Collections.Generic.List[string]
    $names.Add('git')
    foreach ($extension in ([string]$env:PATHEXT).Split(';')) {
        if ($extension) { $names.Add('git' + $extension) }
    }
    return $names.ToArray()
}

function Get-PwshPyGitKindDistrust {
    param([string] $Git)

    # Why the git file $Git is not one an elevated run may ask for its version, or ''. Windows: only a
    # program or a batch file (.exe, .com, .cmd, .bat) is run for its version; anything else PATHEXT
    # makes uv find (a bare file, a .js or .vbs a script host would open) is refused, which fails closed
    # ([R2]): uv takes a bare file only when it is a program, which this does not tell apart. POSIX: any
    # executable file.
    if (-not $PwshPyOnWindows) { return '' }
    $extension = [IO.Path]::GetExtension($Git)
    foreach ($known in @('.exe', '.com', '.cmd', '.bat')) {
        if ([string]::Equals($extension, $known, [StringComparison]::OrdinalIgnoreCase)) { return '' }
    }
    return 'it is not a program an elevated run asks for its version (a .exe, .com, .cmd or .bat), and uv may run it for a git source: remove it, install git as a program, or run the script without elevation'
}

function Initialize-PwshPyPrivateTemp {
    # Elevated POSIX: the account's private temporary directory (<account cache>/pwshpy-tmp), created
    # and its path returned, or the run stops with 127. Only root can enter it, judged like the pack
    # cache (Get-PwshPyCacheDistrust): before it is created (the nearest directory that exists), and
    # after. As the .sh's pwshpy_private_tmp.
    $temp = Join-Path (Get-PwshPyAccountCacheHome) 'pwshpy-tmp'
    $why = Get-PwshPyCacheDistrust -Path $temp
    if (-not $why) {
        Initialize-PwshPyDirectory -Path $temp -Private $true
        Invoke-PwshPyChmod -Arguments @('--', '700', $temp)
        $why = Get-PwshPyCacheDistrust -Path $temp
    }
    if ($why) {
        Write-PwshPyFailure "pwshpy: refusing the temporary directory ${temp}: $why (an elevated run lets uv and git keep temporary files only in a directory no other account can modify): make the path named above root's alone (chown -R root, chmod -R go-w on it)"
        exit 127
    }
    return $temp
}

function Initialize-PwshPyStderrFile {
    # A new empty file for a native program's stderr, its path returned. [IO.Path]::GetTempFileName()
    # places it in TMPDIR, which an elevated POSIX run got from the caller (sudo -E, env_keep): in a
    # directory the caller can write, an entry the caller swaps in at that name (a link) would have root
    # truncate and write the file it names ([R5]). So an elevated POSIX run makes it in a directory only
    # root can enter, below the running account's own cache directory, and opens it CreateNew, which
    # refuses an entry already there. Elsewhere GetTempFileName: unelevated the directory is the caller's
    # own, and an elevated Windows run's TEMP is the elevated token's profile.
    if (-not $PwshPyRootPath) { return [IO.Path]::GetTempFileName() }
    $directory = Join-Path (Get-PwshPyAccountCacheHome) 'pwshpy-tmp'
    Initialize-PwshPyDirectory -Path $directory -Private $true
    # An existing one too: whatever made it, from here only root can enter it.
    Invoke-PwshPyChmod -Arguments @('--', '700', $directory)
    $path = Join-Path $directory ([Guid]::NewGuid().ToString('N'))
    [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None).Dispose()
    return $path
}

function Get-PwshPyGitVersionDistrust {
    param([string] $Git)

    # Why an elevated run of a script with a git source must not let uv run the git $Git, or '' when it
    # is 2.32 or later ([156], the .sh runner's pwshpy_require_git_config_global): git honours
    # GIT_CONFIG_GLOBAL from 2.32 on, and an older one reads the caller's global git configuration, whose
    # core.fsmonitor, core.sshCommand or credential.helper runs a command elevated. A version this cannot
    # read, or a git --version that fails, is refused too: the check fails closed. The first line only,
    # as the .sh: a git printing a warning before its "git version" line names no version here.
    # 'Continue' for the call: with 'Stop', Windows PowerShell 5.1 stops at the first line a native
    # program writes to a redirected stderr.
    $stderr = Initialize-PwshPyStderrFile
    $saved = $ErrorActionPreference
    $first = ''
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(. (ConvertTo-PwshPyNativeCall -FilePath $Git -ArgumentList @('--version') -StderrPath $stderr))
        if ($LASTEXITCODE -eq 0 -and $lines.Count -gt 0) { $first = [string]$lines[0] }
    } catch {
        $first = ''
    } finally {
        $ErrorActionPreference = $saved
        Remove-Item -LiteralPath $stderr -Force -ErrorAction SilentlyContinue
    }
    $why = 'elevated it would read the caller''s git configuration (a script with a git source needs git 2.32 or later elevated): upgrade git, or run the script without elevation'
    # Case-sensitive and ASCII digits only: a version spelled any other way is not one this can read.
    if (-not ($first -cmatch '^git version ([0-9]{1,6})\.([0-9]{1,6})')) {
        return "git --version names no version this runner can read, and a git older than 2.32 ignores GIT_CONFIG_GLOBAL, so $why"
    }
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    if ($major -gt 2 -or ($major -eq 2 -and $minor -ge 32)) { return '' }
    $version = ($first.Substring('git version '.Length) -split ' ', 2)[0]
    return "git $version ignores GIT_CONFIG_GLOBAL, so $why"
}

function Get-PwshPyUvHome {
    # POSIX: the home below which the installer puts uv and the uv search looks, no trailing slash ('' for
    # /): $HOME, or in a root run the running account's passwd home, the directory its cache directory is
    # in (Get-PwshPyAccountCacheHome, as the [159] hand-over takes it), since sudo -E or env_keep hand the
    # caller's HOME over.
    if ($PwshPyRootPath) { return (Split-Path -Parent (Get-PwshPyAccountCacheHome)).TrimEnd('/') }
    return ([string]$HOME).TrimEnd('/')
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
    $uvHome = Get-PwshPyUvHome
    $locations += "$uvHome/.local/bin/uv"
    $locations += "$uvHome/.cargo/bin/uv"
    $locations += '/usr/local/bin/uv'
    # As root neither variable below is asked: sudo -E or env_keep hand the caller's over, and the
    # installer runs without them (Install-PwshPyUv).
    if ($PwshPyRootPath) { return $locations }
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

function Get-PwshPyPosixUvInstaller {
    param([string] $Url, [string] $UvHome)

    # POSIX: uv's installer downloaded from $Url to a file in a private directory, its path returned, or
    # the run stops with 127 naming the download failure. Downloaded first rather than piped into sh:
    # "curl | sh" ends with sh's status, 0 for the empty input a failed download leaves, and the run then
    # failed later with the trust remedy, never trying wget. As root the download runs with nothing of
    # the caller's environment, which sudo -E or env_keep hand over: under the caller's HOME the
    # installer put a root-owned uv in the caller's ~/.local/bin and edited the caller's shell files,
    # and it honours UV_INSTALL_DIR, XDG_BIN_HOME, CARGO_HOME and its own download-URL variables, curl
    # and wget their proxy and certificate variables and their rc files below HOME, so the caller would
    # choose what lands in a directory root then runs uv from. Only the account's own HOME and the fixed
    # helper PATH go in (a proxy is root's own ~/.curlrc, /etc/wgetrc or ~/.wgetrc), into the account's
    # private temporary directory, not the caller's TMPDIR. As the .sh's pwshpy_install_uv.
    if ($PwshPyRootPath) {
        $directory = Initialize-PwshPyPrivateTemp
    } else {
        # Made as the .sh's mktemp -d makes it, by mkdir without -p: a TMPDIR that does not exist is
        # refused, never created with every missing parent and left behind, as New-Item -Force would.
        # Looked up as an Application, so a mkdir function or alias a profile or module defines
        # is never what runs.
        $parent = [IO.Path]::GetTempPath().TrimEnd('/')
        if (-not $parent) { $parent = '/' }
        $directory = Join-Path $parent ('pwshpy-uv-' + [Guid]::NewGuid().ToString('N'))
        $made = $false
        try {
            $mkdir = Get-Command -Name mkdir -CommandType Application -TotalCount 1 -ErrorAction Stop
            & $mkdir.Source -m 700 -- $directory
            $made = $LASTEXITCODE -eq 0
        } catch {
            Write-Verbose "mkdir $directory failed: $_"
        }
        if (-not $made) {
            Write-PwshPyFailure "pwshpy: cannot create a temporary directory for uv's installer in ${parent} (an existing directory this account can write is needed)"
            exit 70
        }
    }
    $file = Join-Path $directory 'uv-installer.sh'
    Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    $homeValue = if ($UvHome) { $UvHome } else { '/' }
    $tried = New-Object System.Collections.Generic.List[string]
    foreach ($downloader in @('curl -LsSf -o', 'wget -q -O')) {
        $words = @($downloader -split ' ')
        $tool = $words[0]
        $present = if ([IO.Path]::IsPathRooted($tool)) {
            Test-Path -LiteralPath $tool -PathType Leaf
        } else {
            [bool](Get-Command -Name $tool -CommandType Application -ErrorAction SilentlyContinue)
        }
        if (-not $present) { continue }
        # Assigned away: anything a downloader prints must not become part of the path returned. As root
        # under umask 022, as the installer after it and the .sh's download.
        if ($PwshPyRootPath) {
            $null = & env -i "HOME=$homeValue" "PATH=$env:PATH" /bin/sh -c 'umask 022 && exec "$@"' sh @words $file $Url
        } else {
            $null = & $tool @($words | Select-Object -Skip 1) $file $Url
        }
        $code = $LASTEXITCODE
        $size = if (Test-Path -LiteralPath $file -PathType Leaf) { (Get-Item -LiteralPath $file).Length } else { 0 }
        if ($code -eq 0 -and $size -gt 0) { return $file }
        $tried.Add($(if ($code -eq 0) { "$tool exited 0 with nothing downloaded" } else { "$tool exited $code" }))
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }
    Clear-PwshPyUvInstaller -Path $file -Directory $directory
    if ($tried.Count -eq 0) {
        Write-PwshPyFailure 'pwshpy: need curl or wget to install uv'
        exit 127
    }
    $why = "pwshpy: could not download uv's installer from $Url ($($tried -join ', ')): check the network connection"
    if ($PwshPyRootPath) {
        $why += '; an elevated run does not apply a proxy or certificate setting of the caller''s environment: install uv for root beforehand, or set the proxy in root''s own ~/.curlrc (curl) or /etc/wgetrc or ~/.wgetrc (wget)'
    } else {
        $why += ' and any proxy setting (https_proxy), or install uv yourself'
    }
    Write-PwshPyFailure $why
    exit 127
}

function Clear-PwshPyUvInstaller {
    param([string] $Path, [string] $Directory)

    # Removes the downloaded installer, and the directory it was put in unless that is root's private
    # temporary directory, which the run goes on using.
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($PwshPyRootPath) { return }
    try { [IO.Directory]::Delete($Directory) } catch { Write-Verbose "left $Directory behind: $_" }
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
        # The caller's PATH too, not only this process's: an elevated run rebuilds uv's PATH from it
        # and hands it to the shim for the script, so a directory joined to $env:PATH alone would be
        # lost to both ([107]).
        $script:PwshPyCallerPath = (Join-Path $env:USERPROFILE '.local\bin') + [IO.Path]::PathSeparator + $script:PwshPyCallerPath
        $env:PATH = $script:PwshPyCallerPath
    } else {
        $url = 'https://astral.sh/uv/install.sh'
        $uvHome = Get-PwshPyUvHome
        $installerFile = Get-PwshPyPosixUvInstaller -Url $url -UvHome $uvHome
        $directory = Split-Path -Parent $installerFile
        # As the download: as root with nothing of the caller's environment, under umask 022.
        if ($PwshPyRootPath) {
            $homeValue = if ($uvHome) { $uvHome } else { '/' }
            & env -i "HOME=$homeValue" "PATH=$env:PATH" /bin/sh -c 'umask 022 && exec /bin/sh "$1"' sh $installerFile
        } else {
            & /bin/sh $installerFile
        }
        $installed = $LASTEXITCODE
        Clear-PwshPyUvInstaller -Path $installerFile -Directory $directory
        if ($installed -ne 0) {
            Write-PwshPyFailure "pwshpy: uv's installer (from $url) failed with exit code $installed"
            exit 127
        }
        # A root run keeps its fixed PATH for helpers; the new uv's directory joins the caller's PATH,
        # which the uv search walks and the packed script gets back.
        $script:PwshPyCallerPath = "$uvHome/.local/bin" + [IO.Path]::PathSeparator + $script:PwshPyCallerPath
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
    $stderr = Initialize-PwshPyStderrFile
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
    param([string] $Why)

    # What an elevated run that refused a managed-Python path for $Why tells the caller to do about it.
    if (Test-PwshPyBareNetworkReason -Why $Why) { return (Get-PwshPyNetworkRemedy) }
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
    # The two arms are grouped before -print: find binds the implicit -a tighter than -o, so an ungrouped
    # -print would end the second arm only, and a symlink matching the first would be printed by nothing.
    $words = @($top, '(', '(', '-type', 'l') + $owners.ToArray() + @(')', '-o', '(', '!', '-type', 'l', '(') + $owners.ToArray()
    $words += @('-o', '(', '(', '-perm', '-0002', '-o', '-perm', '-0020', ')', '!', '(', '-type', 'f', '-name', '.lock', ')', ')', ')', ')', ')', '-print')
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

    # The owner and the DACL of $Item, read through .NET rather than Get-Acl: measured on a Windows 11 test VM,
    # about 0.7 ms an entry against Get-Acl's 1.6 ms, which over a tree of thousands is the difference.
    # On PowerShell 7 (.NET Core) the method is an extension, called as the static it is.
    $sections = [Security.AccessControl.AccessControlSections]'Access, Owner'
    if ($PSVersionTable.PSEdition -ne 'Core') { return $Item.GetAccessControl($sections) }
    if ($Item -is [IO.DirectoryInfo]) { return [IO.FileSystemAclExtensions]::GetAccessControl([IO.DirectoryInfo]$Item, $sections) }
    return [IO.FileSystemAclExtensions]::GetAccessControl([IO.FileInfo]$Item, $sections)
}

function ConvertTo-PwshPyLongPath {
    param([string] $Path)

    # Windows: $Path in the extended-length form (\\?\C:\..., \\?\UNC\server\share\...). Windows
    # PowerShell 5.1 (.NET Framework) refuses a longer path than MAX_PATH (260) with "Illegal name" in
    # GetAccessControl and "does not exist" in Get-Acl, measured on a Windows 11 test VM at 377 characters,
    # while both read the extended form; an install's deepest .pyc already comes close in a profile.
    $Path = $Path.Replace('/', '\')
    if ($Path.StartsWith('\\?\')) { return $Path }
    if ($Path -match '^[A-Za-z]:\\') { return '\\?\' + $Path }
    if ($Path.StartsWith('\\')) { return '\\?\UNC\' + $Path.Substring(2) }
    return $Path
}

function ConvertFrom-PwshPyLongPath {
    param([string] $Path)

    # The usual spelling of an extended-length path (ConvertTo-PwshPyLongPath), for a message.
    if ($Path.StartsWith('\\?\UNC\')) { return '\\' + $Path.Substring(8) }
    if ($Path.StartsWith('\\?\')) { return $Path.Substring(4) }
    return $Path
}

function Resolve-PwshPyLinkedPath {
    param([string] $Path)

    # Windows: the path the absolute $Path names once every symlink and junction on the way is
    # followed (up to 40), component by component as Get-PwshPyUvDistrust follows them, or '' when a
    # link cannot be read. A managed-Python directory reached through a link (a redirected profile, or
    # the directory itself a link) holds uv's links spelled against where it really lies.
    $Path = ConvertFrom-PwshPyDriveLongPath -Path $Path.Replace('/', '\')
    $current = [IO.Path]::GetPathRoot($Path)
    $rest = New-Object System.Collections.Generic.List[string]
    $rest.AddRange([string[]](Split-PwshPyPath -Path $Path))
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
        if ($linkType -ne 'SymbolicLink' -and $linkType -ne 'Junction') {
            $current = $next
            continue
        }
        $hops++
        if ($hops -gt 40) { return '' }
        $target = ([string](@($item.Target)[0])) -replace '^\\\?\?\\', ''
        if ($PSVersionTable.PSEdition -ne 'Core') { $target = $target -replace '^UNC\\', '\\' }
        if (-not $target) { return '' }
        $kind = Get-PwshPyLinkTargetKind -Target $target -LinkType $linkType
        # A target no walk can place leaves the path unresolved, as an unreadable link does.
        if ($kind -eq 'unplaceable') { return '' }
        if ($kind -eq 'absolute') {
            $target = ConvertFrom-PwshPyDriveLongPath -Path $target.Replace('/', '\')
            $current = [IO.Path]::GetPathRoot($target)
        }
        $rest.InsertRange(0, [string[]](Split-PwshPyPath -Path $target))
    }
    return $current
}

function Get-PwshPyTreeLinkDistrust {
    param([IO.FileSystemInfo] $Link, [string] $Shown, [string] $Tree, [string[]] $Roots)

    # Windows: why the link $Link (a reparse point below the directory $Tree, $Shown in the usual
    # spelling) must not be passed, or '' ([149]). The tree walk judges a link as the object it is and
    # never follows it, which is sound only while its target lies inside the tree and is judged there:
    # a .pth linking out of the tree, to a file nobody judged or to a share, ran elevated (measured on a
    # Windows 11 test VM). So a link whose target leaves the tree, or is a network path, is refused
    # whatever the target's ACL. $Roots are the tree's spellings: $Tree itself and the path it names
    # with every link on the way followed (Resolve-PwshPyLinkedPath). uv's own links stay inside: its
    # minor-version junctions (cpython-3.12-... to cpython-3.12.13-... beside it) carry an absolute
    # target in the directory itself (measured on the same VM). Only symlinks and junctions redirect;
    # any other reparse point (a deduplicated or cloud file) is the file itself and keeps the walk's
    # ACL verdict.
    $linkType = [string]$Link.LinkType
    if ($linkType -ne 'SymbolicLink' -and $linkType -ne 'Junction') { return '' }
    # As Get-PwshPyUvDistrust reads a target: 5.1 spells one on a share UNC\server\share.
    $target = ([string](@($Link.Target)[0])) -replace '^\\\?\?\\', ''
    if ($PSVersionTable.PSEdition -ne 'Core') { $target = $target -replace '^UNC\\', '\\' }
    if (-not $target) { return "the link $Shown cannot be read" }
    $kind = Get-PwshPyLinkTargetKind -Target $target -LinkType $linkType
    if ($kind -eq 'unplaceable') { return (Get-PwshPyUnplaceableReason -Link $Shown -Target $target) }
    if ($kind -eq 'absolute') {
        $network = Get-PwshPyNetworkDistrust -Path $target
        if ($network) { return "the link $Shown points to ${target}: $network" }
        $resolved = $target
    } else {
        $resolved = Join-Path ([IO.Path]::GetDirectoryName($Shown)) $target
    }
    $outside = "the link $Shown points to $target, outside $Tree (an elevated run follows no link out of the directory it judged)"
    try {
        $full = [IO.Path]::GetFullPath((ConvertFrom-PwshPyDriveLongPath -Path $resolved.Replace('/', '\'))).TrimEnd('\')
    } catch {
        return $outside
    }
    foreach ($root in $Roots) {
        if (-not $root) { continue }
        $root = $root.TrimEnd('\')
        if ($full -eq $root -or $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { return '' }
    }
    return $outside
}

function Get-PwshPyWindowsTreeDistrust {
    param([string] $Path)

    # Windows half of Get-PwshPyPythonTreeDistrust: every entry below $Path judged with the rights of
    # its kind, as Get-PwshPyLeafDistrust judges one, a link (symlink or junction) as the link object it
    # is (Get-PwshPyLinkDistrust) and never followed: its target inside the directory is judged where
    # it lies, and one outside it, or on a share, is refused (Get-PwshPyTreeLinkDistrust). uv's lock file inherits the directory's ACL here (measured with uv 0.13), so unlike POSIX
    # no entry is exempt. The trusted SIDs are asked for once, and a verdict is kept per distinct owner
    # and DACL (their SDDL) and kind: the entries of an install share a few, so most are judged by a
    # lookup (measured on a Windows 11 test VM, five installs: see Get-PwshPyPythonTreeDistrust).
    $trusted = Get-PwshPyTrustedSid
    $sections = [Security.AccessControl.AccessControlSections]'Access, Owner'
    $verdicts = @{}
    # Where a link below may point: inside the tree, as spelled or as it really lies.
    $roots = @([IO.Path]::GetFullPath($Path), (Resolve-PwshPyLinkedPath -Path ([IO.Path]::GetFullPath($Path))))
    # Walked in the extended-length form, so a path past MAX_PATH reads on 5.1 too.
    $stack = New-Object System.Collections.Generic.Stack[IO.DirectoryInfo]
    $stack.Push((New-Object IO.DirectoryInfo (ConvertTo-PwshPyLongPath -Path $Path)))
    while ($stack.Count -gt 0) {
        $directory = $stack.Pop()
        try {
            $children = @($directory.GetFileSystemInfos())
        } catch {
            return "the entries below $Path cannot be listed ($($_.Exception.Message))"
        }
        foreach ($child in $children) {
            $shown = ConvertFrom-PwshPyLongPath -Path $child.FullName
            if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                # As Get-PwshPyLinkDistrust judges a link, read through the extended form.
                try {
                    $security = Get-Acl -LiteralPath $child.FullName
                } catch {
                    return "the ACL of $shown cannot be read ($($_.Exception.Message))"
                }
                $why = Get-PwshPyAclDistrust -Path $shown -Rights $PwshPyAncestorWriteRights -Security $security -Trusted $trusted
                if (-not $why) { $why = Get-PwshPyTreeLinkDistrust -Link $child -Shown $shown -Tree $Path -Roots $roots }
            } else {
                $isDirectory = $child -is [IO.DirectoryInfo]
                try {
                    $security = Read-PwshPySecurity -Item $child
                } catch {
                    return "the ACL of $shown cannot be read ($($_.Exception.Message))"
                }
                $key = [string]$isDirectory + '|' + $security.GetSecurityDescriptorSddlForm($sections)
                $why = $verdicts[$key]
                if ($null -eq $why) {
                    $rights = if ($isDirectory) { $PwshPyDirectoryWriteRights } else { $PwshPyFileWriteRights }
                    $why = Get-PwshPyAclDistrust -Path $shown -Rights $rights -Security $security -Trusted $trusted
                    # A refusal names its path, so only an acceptance is kept for the others.
                    if (-not $why) { $verdicts[$key] = '' }
                }
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
        # A root is judged here without the walk, so the walk's network rule is asked for too.
        $why = Get-PwshPyNetworkDistrust -Path $directory
        if (-not $why) { $why = Get-PwshPyLeafDistrust -Path $directory -Directory $true }
    } else {
        $why = Get-PwshPyUvDistrust -Path $directory -Directory $true
    }
    if ($why -or $directory -ne $Path) { return $why }
    return (Get-PwshPyPythonTreeDistrust -Path $Path)
}

function Get-PwshPyCacheDistrust {
    param([string] $Path)

    # Why an elevated run must not unpack into, or run the script from, the cache directory $Path (an
    # absolute path), or '' ([98]). The tree is verified against its hashes and executed later, so an
    # account that can write the directory, or rename one above it, could swap a file between the check
    # and uv reading it; and an unpack removes and rewrites the directory with elevated rights. Its root
    # is whatever PWSHPY_PACK_CACHE, LOCALAPPDATA or XDG_CACHE_HOME the caller set, so it is judged like
    # uv's managed-Python directory: itself, every directory above it and every entry below it, or,
    # before the first unpack, the nearest directory that exists.
    return (Get-PwshPyPythonDirDistrust -Path $Path)
}

function Assert-PwshPyCacheTrusted {
    param([string] $Path)

    # Exits 127 when an elevated run may not use the cache directory $Path (Get-PwshPyCacheDistrust).
    $why = Get-PwshPyCacheDistrust -Path $Path
    if (-not $why) { return }
    if ($PwshPyOnWindows) {
        $remedy = 'set PWSHPY_PACK_CACHE to a directory only this account, SYSTEM and Administrators can modify, or take every other account''s write access off the path named above'
    } else {
        $remedy = 'set PWSHPY_PACK_CACHE to a directory only root can modify, or make the path named above root''s alone (chown -R root, chmod -R go-w on it)'
    }
    Write-PwshPyFailure "pwshpy: refusing the cache directory ${Path}: $why (an elevated run unpacks and runs the script only from a directory no other account can modify, nor anything below it): $remedy"
    exit 127
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
        Write-PwshPyFailure "pwshpy: refusing the Python uv found at ${python}: $why (an elevated run only runs a Python no other account can modify): $(Get-PwshPyPythonRemedy -Why $why)"
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
        Write-PwshPyNotice "pwshpy: ignoring ${name}: an elevated run lets uv use its defaults for package sources, archive checks, caches and env files"
        Remove-Item -LiteralPath "Env:$name"
    }
    foreach ($name in $PwshPyNetworkVariables) {
        if ($null -eq [Environment]::GetEnvironmentVariable($name)) { continue }
        Write-PwshPyNotice "pwshpy: ignoring ${name}: an elevated run lets uv connect with its own defaults: no proxy, its built-in certificate roots, no keyring"
        Remove-Item -LiteralPath "Env:$name"
    }
    # git, which uv runs for a git source (a PEP 723 dependency or a --with value), reads every GIT_
    # variable: GIT_CONFIG_COUNT with GIT_CONFIG_KEY_n/GIT_CONFIG_VALUE_n sets any config key (a
    # core.fsmonitor command ran from uv's git fetch, measured with uv 0.13 and git 2.53), and
    # GIT_SSH_COMMAND, GIT_PROXY_COMMAND, GIT_EXEC_PATH, GIT_ASKPASS or GIT_SSL_CAINFO name a command to
    # run or a CA to trust. Dropped by prefix, so a variable a later git adds is gone too, with
    # SSH_ASKPASS: git runs it for a password when GIT_ASKPASS and core.askPass are unset (it ran on an
    # HTTP 401 from uv's git fetch, measured). The credential helper git runs on an authentication
    # challenge reads its own: Git Credential Manager (Git for Windows' default helper) every GCM_
    # variable (its docs/environment.md; GCM_GPG_PATH names the gpg it runs, GCM_PLAINTEXT_STORE_PATH
    # and GCM_DPAPI_STORE_PATH the directory it reads credentials from, GCM_CREDENTIAL_STORE which
    # store), dropped by prefix too, and PASSWORD_STORE_DIR, the directory its gpg store reads (its
    # docs/credstores.md). Windows matches the prefixes in any case, as its environment does
    # (Git_Ssh_Command is GIT_SSH_COMMAND there); POSIX only in upper case, as the .sh runner.
    $gitNames = @(foreach ($variable in @(Get-ChildItem -LiteralPath 'Env:')) {
        $name = $variable.Name
        $isGit = if ($PwshPyOnWindows) {
            $name -like 'GIT_*' -or $name -like 'GCM_*'
        } else {
            $name -clike 'GIT_*' -or $name -clike 'GCM_*'
        }
        if ($isGit) { $name }
    })
    foreach ($name in $gitNames + @('SSH_ASKPASS', 'PASSWORD_STORE_DIR')) {
        if ($null -eq [Environment]::GetEnvironmentVariable($name)) { continue }
        Write-PwshPyNotice "pwshpy: ignoring ${name}: an elevated run lets uv run git with none of the caller's git or credential-helper settings"
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

function Get-PwshPyEmptyFileDistrust {
    param([string] $Path)

    # Why $Path cannot serve an elevated run as an empty file no other account can change, or ''. Makes
    # the directory and the file when they are missing. Each directory is judged before anything is
    # created in it: the one above (the account's data directory), so the directory is only ever created
    # where no other account can add, swap or redirect an entry ([131]: a link on the way would have it
    # created at the link's target first), then the directory itself before the file. An existing entry
    # is never truncated or deleted (a link there could point at any file the elevated account may
    # write). A link, a directory or a file with content in its place is refused: only an account
    # trusted with the directory can have put it there, and removing it is left to that account. A
    # create that fails (a file where the directory belongs, an entry Get-Item does not see) is a
    # reason too, so the caller refuses with its remedy rather than stopping on the exception.
    $directory = [IO.Path]::GetDirectoryName($Path)
    $why = Get-PwshPyUvDistrust -Path ([IO.Path]::GetDirectoryName($directory)) -Directory $true
    if ($why) { return $why }
    try {
        $null = [IO.Directory]::CreateDirectory($directory)
    } catch {
        return "$directory cannot be created ($($_.Exception.Message))"
    }
    $why = Get-PwshPyUvDistrust -Path $directory -Directory $true
    if ($why) { return $why }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) {
        try {
            # CreateNew: fails rather than open whatever appeared at the path since it was looked at.
            [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write).Dispose()
            $item = Get-Item -LiteralPath $Path -Force
        } catch {
            return "$Path cannot be created ($($_.Exception.Message))"
        }
    }
    if ($item.LinkType -or $item.PSIsContainer) { return "$Path is not a plain file" }
    if ($item.Length -ne 0) { return "$Path is not empty" }
    return (Get-PwshPyUvDistrust -Path $Path)
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
        # A user sudo lets run this pwsh, the relaunch's own program, without a password (a NOPASSWD
        # rule) is asked nothing ([280]): under sudo's default verifypw=all, -v wants a password unless
        # EVERY rule of the user is NOPASSWD, so beside Ubuntu's %sudo rule it asked for one the
        # command does not need, and without a terminal (or any password) the run was declined. The
        # probe runs the same program as the relaunch, word for word (ConvertTo-PwshPyNativeCall, as
        # the relaunch below: a host path holding [ ] * ? is never globbed into a sibling's), so a rule
        # for some other program only cannot pass it; it prints nothing, and its refusal is not an
        # error of this run.
        $probeArgs = @('-n', (Get-PwshPyHostPath), '-NoProfile', '-NonInteractive', '-Command', 'exit 0')
        . (ConvertTo-PwshPyNativeCall -FilePath 'sudo' -ArgumentList $probeArgs -StdoutPath '/dev/null' -StderrPath '/dev/null')
        if ($LASTEXITCODE -ne 0) {
            # Ask for the password with `sudo -v` first: sudo exits 1 when it refuses, which is also
            # an ordinary exit code of the script, so only asking separately tells the two apart.
            & sudo -v
            if ($LASTEXITCODE -ne 0) {
                [Console]::Error.WriteLine('pwshpy: elevation declined (sudo did not grant root)')
                exit 77
            }
            # Only the -v prompts ([136-sh]): the relaunch runs as sudo -n, which never asks, so a
            # sudo that keeps no credential after -v (timestamp_timeout=0) would prompt a second time
            # without -n. Such a setup is told by sudo -n -v failing, and declined like a refused prompt.
            & sudo -n -v
            if ($LASTEXITCODE -ne 0) {
                [Console]::Error.WriteLine('pwshpy: elevation declined (sudo keeps no credential after the password prompt, so it would ask again (timestamp_timeout=0))')
                exit 77
            }
        }
        # sudo keeps the console, so output and exit code flow back without a relay. A sudo that
        # grants -v but then refuses the command itself (sudoers allowing only -v) exits with that
        # sudo's own code, which no wrapper can tell from the script's.
        # Every word literal (ConvertTo-PwshPyNativeCall): the caller's '*' or '~' reaches the
        # script as typed, and a runner path holding '?' names this file, not a sibling it matches.
        $sudoArgs = @('-n', (Get-PwshPyHostPath), '-NoProfile', '-File', $self)
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
    if ($PwshPyGitSource) {
        Write-Host 'git source : yes (an elevated run needs git 2.32 or later)'
    } else {
        Write-Host 'git source : no'
    }
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
    # As root the pack cache's default is below the running account's own cache directory (from the
    # passwd database, as uv's cache below), never XDG_CACHE_HOME or HOME: sudo -E or env_keep hand those
    # over from the caller, whose directory the cache check would then refuse ([R3]), and the caller would
    # choose where root unpacks. PWSHPY_PACK_CACHE stays the caller's to set, and is judged like any other.
    # Before -PwshPyInfo, which names the directory this run uses; a run that relaunches itself under sudo
    # gets here again as root.
    if (-not $PwshPyOnWindows -and -not $env:PWSHPY_PACK_CACHE -and (Test-PwshPyElevated)) {
        $PwshPyCacheDir = Join-Path (Join-Path (Get-PwshPyAccountCacheHome) 'pwshpy-pack') $PwshPySha.Substring(0, 16)
    }

    if ($PwshPyInfo) { Write-PwshPyManifest -CacheDir $PwshPyCacheDir; exit 0 }

    if ($PwshPyWantElevate -and -not (Test-PwshPyElevated)) {
        exit (Invoke-PwshPyElevated -ForwardArgs (@($PwshPyRelaunchSwitches.ToArray()) + $PwshPyForward))
    }

    # Asked after any relaunch: the trust rule applies to whatever runs elevated, UAC, SYSTEM
    # (a scheduled task) or root alike. As root on POSIX every helper the runner calls (ls, the uv
    # installer's curl and sh) runs with full rights, so from here on they come from fixed system
    # directories, not from the PATH the caller handed over; that PATH is kept for the uv search
    # and handed back to the packed script. Windows needs no such reset: the runner calls no
    # external program there except uv and its own PowerShell, both by full path. On both, uv itself
    # sees only the caller's PATH directories no other account can modify (see the run below).
    $PwshPyElevatedRun = Test-PwshPyElevated
    # Script scope by name: Get-PwshPyUvPath, Install-PwshPyUv and the run below read it.
    $script:PwshPyCallerPath = $env:PATH
    $PwshPyRootPath = $PwshPyElevatedRun -and -not $PwshPyOnWindows
    if ($PwshPyRootPath) { $env:PATH = $PwshPyFixedPath }

    # --- unpack -----------------------------------------------------------------------------

    # Elevated: the cache is judged before anything is removed or written in it, and the tree an unpack
    # wrote is judged again, since a new directory inherits whatever the one above passes down ([98]).
    # Absolute first: the walk refuses a relative path, and a relative PWSHPY_PACK_CACHE names the
    # caller's directory.
    if ($PwshPyElevatedRun) {
        $PwshPyCacheDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PwshPyCacheDir)
        Assert-PwshPyCacheTrusted -Path $PwshPyCacheDir
    }
    if ($PwshPyClean -and (Test-Path -LiteralPath $PwshPyCacheDir)) {
        Remove-Item -LiteralPath $PwshPyCacheDir -Recurse -Force
    }
    if (-not (Test-PwshPyTree -Root $PwshPyCacheDir)) {
        Invoke-PwshPyUnpackLocked -CacheDir $PwshPyCacheDir
        if ($PwshPyElevatedRun) { Assert-PwshPyCacheTrusted -Path $PwshPyCacheDir }
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
        # With every GIT_ variable gone git still reads its global files, .gitconfig and
        # XDG_CONFIG_HOME's git\config, below the HOME, USERPROFILE (Windows: a UAC relaunch keeps the
        # caller's profile) and XDG_CONFIG_HOME this run got, and a core.fsmonitor, core.sshCommand or
        # credential.helper there runs a command elevated when uv fetches a git source. A set
        # GIT_CONFIG_GLOBAL replaces both (git 2.32 and later; an older git ignores it and still reads
        # them, so for a script with a git source one is refused below, [156]). POSIX: /dev/null, which only root can change. Windows: an empty file below the
        # account's own data directory, judged as an elevated run judges a uv, which Git for Windows
        # reads as an empty configuration (measured with 2.55). Not the NUL device, which an MSYS2 or
        # Cygwin git may read as a file named NUL in its working directory, nor a path such as
        # \dev\null, which a git not mapping it resolves on the current drive, where any user may
        # create the folder. Set after the drop, which would remove it. The system file
        # (Program Files' etc\gitconfig, /etc/gitconfig) is the administrators' and still applies.
        $PwshPyGitGlobal = '/dev/null'
        if ($PwshPyOnWindows) {
            $PwshPyGitGlobal = Join-Path (Join-Path (Get-PwshPyAccountDataHome) 'pwshpy') 'empty.gitconfig'
            $PwshPyGitGlobalWhy = Get-PwshPyEmptyFileDistrust -Path $PwshPyGitGlobal
            if ($PwshPyGitGlobalWhy) {
                $PwshPyGitGlobalRemedy = "take every other account's write access off the path named above and leave the file empty, or remove its directory and run again elevated, which creates it"
                # A share is no less a share on the next run, which would create the file there again.
                if (Test-PwshPyBareNetworkReason -Why $PwshPyGitGlobalWhy) { $PwshPyGitGlobalRemedy = Get-PwshPyNetworkRemedy }
                Write-PwshPyFailure "pwshpy: refusing the git configuration file ${PwshPyGitGlobal}: $PwshPyGitGlobalWhy (an elevated run points GIT_CONFIG_GLOBAL only at an empty file no other account can modify): $PwshPyGitGlobalRemedy"
                exit 127
            }
        }
        Set-Item -LiteralPath 'Env:GIT_CONFIG_GLOBAL' -Value $PwshPyGitGlobal
        Write-PwshPyNotice "pwshpy: setting GIT_CONFIG_GLOBAL=${PwshPyGitGlobal}: an elevated run lets git read no caller's global config"
        $PwshPyDataHome = Get-PwshPyAccountDataHome
        Set-Item -LiteralPath 'Env:UV_PYTHON_INSTALL_DIR' -Value (Join-Path (Join-Path $PwshPyDataHome 'uv') 'python')
        # The cache too, for the same reason: uv's default follows LOCALAPPDATA (Windows) or XDG_CACHE_HOME
        # and HOME (POSIX, which sudo -E or env_keep can hand over), and cached script environments and
        # wheels run elevated. uv's own layout below the folder: uv\cache on Windows, uv on POSIX.
        $PwshPyUvCache = Join-Path (Get-PwshPyAccountCacheHome) 'uv'
        if ($PwshPyOnWindows) { $PwshPyUvCache = Join-Path $PwshPyUvCache 'cache' }
        Set-Item -LiteralPath 'Env:UV_CACHE_DIR' -Value $PwshPyUvCache
        Write-PwshPyNotice "pwshpy: uv caches in ${PwshPyUvCache}: an elevated run uses the running account's own cache directory"
        # POSIX: the home too ([159], user decision: own HOME for uv), as the .sh root run. uv and the git
        # it runs read and write below HOME and the XDG directories (uv's credentials and its netrc, git's
        # ~/.git-credentials, caches, state), which sudo -E or env_keep hand over from the caller, so as
        # root they would read the caller's secrets and create files the caller cannot remove. uv and git
        # get the running account's own: its passwd home (the directory its cache directory is in), its
        # cache and data directories, <home>/.config, and the defaults below that home for the state
        # directory, the netrc and uv's credentials; the shim gives the script the caller's back. Windows
        # is not covered: an elevated token keeps the caller's own profile there.
        if (-not $PwshPyOnWindows) {
            $PwshPyCallerNames = New-Object System.Collections.Generic.List[string]
            foreach ($name in @('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'NETRC', 'UV_CREDENTIALS_DIR', 'TMPDIR', 'TMP', 'TEMP')) {
                $handOver = "Env:${PwshPyCallerEnv}_$name"
                if (Test-Path -LiteralPath "Env:$name") {
                    Set-Item -LiteralPath $handOver -Value ([Environment]::GetEnvironmentVariable($name))
                } else {
                    Remove-Item -LiteralPath $handOver -ErrorAction SilentlyContinue
                }
                $PwshPyCallerNames.Add($name)
            }
            Set-Item -LiteralPath "Env:$PwshPyCallerEnv" -Value ($PwshPyCallerNames -join ' ')
            foreach ($name in @('XDG_STATE_HOME', 'NETRC', 'UV_CREDENTIALS_DIR')) {
                Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
            }
            $PwshPyAccountCache = Get-PwshPyAccountCacheHome
            $PwshPyAccountHome = Split-Path -Parent $PwshPyAccountCache
            if (-not $PwshPyAccountHome) { $PwshPyAccountHome = '/' }
            Set-Item -LiteralPath 'Env:HOME' -Value $PwshPyAccountHome
            Set-Item -LiteralPath 'Env:XDG_CONFIG_HOME' -Value ($PwshPyAccountHome.TrimEnd('/') + '/.config')
            Set-Item -LiteralPath 'Env:XDG_CACHE_HOME' -Value $PwshPyAccountCache
            Set-Item -LiteralPath 'Env:XDG_DATA_HOME' -Value $PwshPyDataHome
            Write-PwshPyNotice "pwshpy: setting HOME=${PwshPyAccountHome}: an elevated run lets uv and git use the running account's own home"
            # The temporary directory too (handed over above), as the .sh root run: uv, git and the build
            # backends uv runs create files and directories below TMPDIR (Python's tempfile also reads
            # TEMP and TMP), the caller's under sudo -E or env_keep, where the caller could swap a
            # directory root made for a link to anywhere. All three name the account's private temporary
            # directory (Initialize-PwshPyPrivateTemp).
            $PwshPyTemp = Initialize-PwshPyPrivateTemp
            foreach ($name in @('TMPDIR', 'TMP', 'TEMP')) { Set-Item -LiteralPath "Env:$name" -Value $PwshPyTemp }
            Write-PwshPyNotice "pwshpy: setting TMPDIR=${PwshPyTemp}: an elevated run lets uv and git keep temporary files in the account's own directory"
        }
        # The directory before uv searches it: uv runs every Python it finds there.
        $PwshPyPythonDirWhy = Get-PwshPyPythonDirDistrust -Path $env:UV_PYTHON_INSTALL_DIR
        if ($PwshPyPythonDirWhy) {
            Write-PwshPyFailure "pwshpy: refusing uv's managed-Python directory $($env:UV_PYTHON_INSTALL_DIR): $PwshPyPythonDirWhy (an elevated run lets uv search only a directory no other account can modify, nor anything below it): $(Get-PwshPyPythonRemedy -Why $PwshPyPythonDirWhy)"
            exit 127
        }
        # The PATH uv will see: the caller's directories no other account can modify ([107]), judged
        # while a POSIX root run's helpers still come from the fixed PATH. Script scope by name: the run
        # preparation, a block of its own, reads it.
        $script:PwshPyTrustedUvPath = Get-PwshPyTrustedPath -Path $PwshPyCallerPath
        # The git uv will run, asked only for a script that needs one; with none, uv fails on its own.
        # Before uv is asked for a managed Python, which it may download: a run refused here calls no uv
        # ([156], as the .sh root run).
        if ($PwshPyGitSource) {
            # The directories on uv's PATH were judged, the git files found there were not: one another
            # account can modify, or a link to one, runs elevated when uv fetches a git source ([157],
            # user decision: judge the git file). Judged like uv, and before it is run for its version.
            # Every git file in the directory uv takes git from, since which one uv runs depends on what
            # each file is ([R2]); one that fails stops the run.
            foreach ($PwshPyUvGit in @(Get-PwshPyGitCandidate -Path $script:PwshPyTrustedUvPath)) {
                $PwshPyGitWhy = Get-PwshPyUvDistrust -Path $PwshPyUvGit
                if ($PwshPyGitWhy) {
                    $PwshPyGitRemedy = if ($PwshPyOnWindows) { 'take every other account''s write access off the path named above (only the running account, SYSTEM and Administrators may change it), or run the script without elevation' } else { 'make the path named above root''s alone (chown root, then chmod go-w on it), or run the script without elevation' }
                    Write-PwshPyFailure "pwshpy: refusing git at ${PwshPyUvGit}: $PwshPyGitWhy (an elevated run lets uv run only a git no other account can modify): $PwshPyGitRemedy"
                    exit 127
                }
                $PwshPyGitWhy = Get-PwshPyGitKindDistrust -Git $PwshPyUvGit
                if ($PwshPyGitWhy) {
                    Write-PwshPyFailure "pwshpy: refusing git at ${PwshPyUvGit}: $PwshPyGitWhy"
                    exit 127
                }
                $PwshPyGitWhy = Get-PwshPyGitVersionDistrust -Git $PwshPyUvGit
                if ($PwshPyGitWhy) {
                    Write-PwshPyFailure "pwshpy: refusing git at ${PwshPyUvGit}: $PwshPyGitWhy"
                    exit 127
                }
            }
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
    # Only a POSIX root run hands the caller's home variables over to the shim ([159]); in any other run
    # a hand-over marker came with the caller's environment, and the shim would act on it.
    if (-not ($PwshPyElevatedRun -and -not $PwshPyOnWindows)) {
        Remove-Item -LiteralPath "Env:$PwshPyCallerEnv" -ErrorAction SilentlyContinue
    }

    $PwshPyTarget = Join-Path $PwshPyCacheDir $PwshPyShim
    # Elevated (Windows or POSIX root), uv sees only the caller's PATH directories no other account can
    # modify, judged while a POSIX root run's helpers still come from the fixed PATH; the packed script
    # gets the caller's whole PATH back from the shim, behind the entries uv puts in front of the PATH
    # it was given, which the shim is handed too ([107]).
    if ($PwshPyElevatedRun) {
        $PwshPyTrustedUvPath = $script:PwshPyTrustedUvPath
        Set-Item -LiteralPath ('Env:' + $PwshPyPathEnv) -Value $PwshPyCallerPath
        # When no caller directory is trusted this is ''. Windows PowerShell 5.1 deletes a variable set
        # to '', so under 5.1 the shim finds no uv PATH, cannot tell what uv put in front, and gives
        # the script the caller's PATH without uv's entry. pwsh 7 keeps the variable set to '', so the
        # shim keeps uv's entry there, as the .sh does (uv 0.13 hands a script run on an empty PATH
        # '<interpreter bin>:'). Either way it fails safe: nothing untrusted is added.
        Set-Item -LiteralPath ('Env:' + $PwshPyUvPathEnv) -Value $PwshPyTrustedUvPath
        # A git dependency would fail with uv's bare "git not found" when the only gits are in
        # directories left off; name the cause.
        $PwshPyCallerGit = Get-PwshPyGitOn -Path $PwshPyCallerPath
        if ($PwshPyCallerGit -and -not (Get-PwshPyGitOn -Path $PwshPyTrustedUvPath)) {
            $PwshPyGitWhere = if ($PwshPyOnWindows) { 'fails elevated until git is installed in a directory no other account can modify (e.g. a system-wide install)' } else { 'fails as root until git is installed in a directory only root can modify' }
            Write-PwshPyNotice "pwshpy: no git on uv's PATH: $PwshPyCallerGit and any other git on the caller's PATH are in directories another account can modify; a git dependency $PwshPyGitWhere"
        }
        $env:PATH = $PwshPyTrustedUvPath
    }

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
        # Only an elevated run changes directory or hands over a PATH; one inherited from the caller's
        # environment must not move an unelevated script or replace its PATH.
        foreach ($PwshPyHandOver in @($PwshPyCwdEnv, $PwshPyPathEnv, $PwshPyUvPathEnv)) {
            if (Test-Path -LiteralPath ('Env:' + $PwshPyHandOver)) { Remove-Item -LiteralPath ('Env:' + $PwshPyHandOver) }
        }
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
