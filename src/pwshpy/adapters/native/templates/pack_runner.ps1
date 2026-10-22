#Requires -Version 5.1
<#
.SYNOPSIS
    Packed with pwshpy: the Python script '@@PWSHPY_ENTRY@@' and its local modules, in one file.

.DESCRIPTION
    This file carries '@@PWSHPY_ENTRY@@' and every local module it imports as a base64 zip.
    Running it unpacks them into a per-user cache, installs `uv` if this machine has none,
    runs the script, and exits with the script's own exit code. Nothing needs to be
    installed first, not even Python.

    Look inside without running any of it:

        pwsh -File <this file> -PwshPyInfo

    Unpack the Python sources to read or edit them (uvx installs nothing permanently):

        uvx pwshpy unpack <this file> -o src

    Re-pack after editing, replacing this file:

        uvx pwshpy pack src/@@PWSHPY_ENTRY@@ -o <this file> --force

    Switches this wrapper understands. Every other argument is passed to the packed script
    exactly as you typed it, so a script taking -v or --path still works:

        -PwshPyHelp         show this help and exit
        -PwshPyInfo         show the payload manifest and exit
        -PwshPyClean        discard the cached extraction and unpack again
        -PwshPyNoInstallUv  fail with exit code 127 rather than installing uv
        -PwshPyElevate      relaunch elevated first (UAC on Windows, sudo on POSIX);
                            exit code 77 if the elevation is refused
        -PwshPyUv PATH      run the uv at this absolute path instead of searching for one

    Run elevated (an administrator token, SYSTEM, or root), it only runs a uv that no
    non-administrator can modify: neither the file nor the directory holding it may grant write
    access to any account other than SYSTEM, Administrators, LOCAL SERVICE, NETWORK SERVICE or
    TrustedInstaller (on POSIX: root must own both, and nobody else may write them). Any other uv
    it finds is skipped with a line on stderr, and a -PwshPyUv path that fails the check is
    refused with exit code 127.

    Set PWSHPY_PACK_CACHE to choose where the payload unpacks (default: a per-user cache dir).

    Home: https://github.com/bitranox/pwshpy
#>

# No param() block and no [CmdletBinding()] here, deliberately. An advanced script binds
# PowerShell's common parameters, so a packed script's own -v / -d / -ea would be captured
# by -Verbose / -Debug / -ErrorAction and never reach Python. Hand-parsing $args keeps
# every argument the caller typed.

$ErrorActionPreference = 'Stop'

$PwshPyEntry = '@@PWSHPY_ENTRY@@'
$PwshPyShim = '@@PWSHPY_SHIM@@'
$PwshPySha = '@@PWSHPY_PAYLOAD_SHA256@@'
$PwshPyArgvEnv = '@@PWSHPY_ARGV_ENV@@'
$PwshPyUvArgs = @(@@PWSHPY_UV_ARGS@@)

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
# their rights: SYSTEM, LOCAL SERVICE, NETWORK SERVICE, BUILTIN\Administrators, TrustedInstaller.
# By SID, never by name: the names are localised ('Administratoren' on a German Windows).
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

function Test-PwshPyAbsolutePath {
    param([string] $Path)

    # [IO.Path]::IsPathRooted calls 'C:uv.exe' and '\uv.exe' rooted, though both depend on the
    # current drive or directory; only a drive-qualified or UNC path names one file.
    if ($PwshPyOnWindows) { return ($Path -match '^[A-Za-z]:[\\/]' -or $Path -match '^[\\/]{2}[^\\/]') }
    return $Path.StartsWith('/')
}

function Get-PwshPyAclDistrust {
    param([string] $Path, [bool] $Directory)

    # Returns why a non-administrator could change $Path, or '' when none can. The owner counts as
    # a writer whatever the ACL says, because an owner may always rewrite the ACL.
    try {
        $acl = Get-Acl -LiteralPath $Path
    } catch {
        return "the ACL of $Path cannot be read ($($_.Exception.Message))"
    }
    $sidType = [Security.Principal.SecurityIdentifier]
    $owner = $acl.GetOwner($sidType)
    $ownerSid = if ($owner) { $owner.Value } else { 'an unknown account' }
    if ($PwshPyTrustedSids -notcontains $ownerSid) { return "$Path is owned by $ownerSid, not an administrative account" }
    $dangerous = if ($Directory) { $PwshPyDirectoryWriteRights } else { $PwshPyFileWriteRights }
    foreach ($rule in $acl.GetAccessRules($true, $true, $sidType)) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        # An inherit-only entry applies to children created later, not to this object.
        if ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
        $sid = $rule.IdentityReference.Value
        # CREATOR OWNER and OWNER RIGHTS stand for the owner, already judged above.
        if ($sid -eq 'S-1-3-0' -or $sid -eq 'S-1-3-4') { continue }
        if ($PwshPyTrustedSids -contains $sid) { continue }
        if (([int]$rule.FileSystemRights) -band $dangerous) {
            return "$Path lets $sid change it ($($rule.FileSystemRights))"
        }
    }
    return ''
}

function Get-PwshPyModeDistrust {
    param([string] $Path)

    # POSIX: root must own $Path, nobody else may write it, and group write counts only for group 0,
    # whose members are root equivalents. A mode with an ACL ('+') is refused outright: its group
    # bits then show the ACL mask, not who may write. ls -ldn because it is on every POSIX system
    # (stat's flags differ between GNU, BSD and busybox).
    $ls = Get-Command ls -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ls) { return "$Path cannot be inspected (no ls)" }
    try {
        $listing = & $ls.Source -ldn $Path 2>$null
    } catch {
        $listing = $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $listing) { return "$Path cannot be inspected" }
    $fields = ([string](@($listing)[0])).Trim() -split '\s+'
    $mode = $fields[0]
    if ($mode.Length -gt 10 -and $mode[10] -eq [char]'+') { return "$Path carries an ACL" }
    if ($mode[8] -eq [char]'w') { return "$Path is writable by every account" }
    if ($fields[2] -ne '0') { return "$Path is owned by uid $($fields[2]), not root" }
    if ($fields[3] -ne '0' -and $mode[5] -eq [char]'w') { return "$Path is writable by group $($fields[3])" }
    return ''
}

function Get-PwshPyUvDistrust {
    param([string] $Path)

    # Returns why an elevated run must not execute the uv at $Path, or '' when it may. Judges the
    # directory holding it, then follows a symlink one hop at a time, judging each hop's directory,
    # and finally the file the chain ends at: whoever can rewrite a link's directory decides what
    # runs as much as whoever can write the target.
    $current = $Path
    for ($hop = 0; $hop -le 40; $hop++) {
        if (-not (Test-PwshPyAbsolutePath -Path $current)) { return "$current is not an absolute path" }
        $directory = [IO.Path]::GetDirectoryName($current)
        if ($directory) {
            $why = if ($PwshPyOnWindows) { Get-PwshPyAclDistrust -Path $directory -Directory $true } else { Get-PwshPyModeDistrust -Path $directory }
            if ($why) { return "its directory $why" }
        }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) { return "$current cannot be inspected" }
        if ([string]$item.LinkType -ne 'SymbolicLink') {
            if ($PwshPyOnWindows) { return (Get-PwshPyAclDistrust -Path $current -Directory $false) }
            return (Get-PwshPyModeDistrust -Path $current)
        }
        # Target is a string on PowerShell 7 and a one-element array on 5.1.
        $target = [string](@($item.Target)[0])
        if (-not $target) { return "the symlink $current cannot be read" }
        if (-not (Test-PwshPyAbsolutePath -Path $target)) { $target = Join-Path $directory $target }
        $current = $target
    }
    return "$Path is a chain of more than 40 symlinks"
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
    Write-PwshPyNotice "pwshpy: skipping uv at ${Candidate}: $why (an elevated run only runs a uv no non-administrator can modify)"
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
    $found = @(Get-Command uv -CommandType Application -ErrorAction SilentlyContinue)
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
        $env:PATH = (Join-Path $HOME '.local/bin') + [IO.Path]::PathSeparator + $env:PATH
    }
}

function Test-PwshPyElevated {
    if ($PwshPyOnWindows) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    return ((& id -u) -eq '0')
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
        & sudo (Get-PwshPyHostPath) -NoProfile -File $self @ForwardArgs
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

    # --- unpack -----------------------------------------------------------------------------

    if ($PwshPyClean -and (Test-Path -LiteralPath $PwshPyCacheDir)) {
        Remove-Item -LiteralPath $PwshPyCacheDir -Recurse -Force
    }
    if (-not (Test-PwshPyTree -Root $PwshPyCacheDir)) {
        Invoke-PwshPyUnpackLocked -CacheDir $PwshPyCacheDir
    }

    # --- uv ---------------------------------------------------------------------------------

    # Asked after any relaunch: the trust rule applies to whatever runs elevated, UAC, SYSTEM
    # (a scheduled task) or root alike.
    $PwshPyElevatedRun = Test-PwshPyElevated
    if ($PwshPyOnWindows) {
        $PwshPyUvHint = 'An elevated run only runs a uv no non-administrator can modify: install one where only administrators can write (for example under C:\Program Files) or pass -PwshPyUv <path>.'
    } else {
        $PwshPyUvHint = 'As root only a uv owned by root that no other account can modify is run: install one there (for example under /usr/local/bin) or pass -PwshPyUv <path>.'
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
        # Elevated, the installer writes into the running account's profile: SYSTEM's is admin-only,
        # so a scheduled task's fresh install passes the trust rule; an administrator's own profile
        # is theirs to write unelevated, so a uv installed there is refused just like one found there.
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
}

if ($PwshPyRelay) {
    try {
        # Created first, so a child that fails from here on has a file to say why in; the run
        # below truncates it again for the script's own stderr.
        [IO.File]::WriteAllText("$PwshPyRelay.err", '')
        . $PwshPyPrepare
    } catch {
        # 70 (EX_SOFTWARE), so a caller can tell "the runner never started the script" from
        # PowerShell's generic 1; the stderr line covers a script that itself exits 70.
        Write-PwshPyFailure "pwshpy: the elevated child failed before running the script: $($_.Exception.Message) [$($_.CategoryInfo.Category)]"
        exit 70
    }
} else {
    . $PwshPyPrepare
}

# --- run --------------------------------------------------------------------------------
# The argument vector goes out of band: PowerShell 5.1 re-quotes arguments on their way to
# a native executable (empty strings vanish, embedded quotes are mangled) and only 7.3 fixed
# it. Base64 per argument sidesteps the quoting layer entirely, so the packed script sees
# exactly what the caller typed on every host.

$PwshPyEncoded = @('v1')
foreach ($argument in $PwshPyForward) {
    $PwshPyEncoded += [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$argument))
}
$env:PwshPyArgvPlaceholder = $null
Set-Item -Path ('Env:' + $PwshPyArgvEnv) -Value ($PwshPyEncoded -join '|')

$PwshPyTarget = Join-Path $PwshPyCacheDir $PwshPyShim

if ($PwshPyRelay) {
    # Elevated child: stream to the relay files the parent replays, then hand back the code.
    & $PwshPyUv run --no-project @PwshPyUvArgs $PwshPyTarget > "$PwshPyRelay.out" 2> "$PwshPyRelay.err"
    exit $LASTEXITCODE
}

& $PwshPyUv run --no-project @PwshPyUvArgs $PwshPyTarget
exit $LASTEXITCODE
