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
        -PwshPyElevate      relaunch elevated first (UAC on Windows, sudo on POSIX)

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

function Get-PwshPyPayloadBytes {
    return [Convert]::FromBase64String(($PwshPyPayload -replace '\s', ''))
}

function Expand-PwshPyPayload {
    param([string] $Destination)

    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null

    $archive = Join-Path $Destination '.payload.zip'
    [IO.File]::WriteAllBytes($archive, (Get-PwshPyPayloadBytes))
    # System.IO.Compression.FileSystem is preloaded on PowerShell 7 but not on 5.1.
    try { $null = [System.IO.Compression.ZipFile] } catch { Add-Type -AssemblyName System.IO.Compression.FileSystem }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($archive, $Destination)
    Remove-Item -LiteralPath $archive -Force
}

function Test-PwshPyTree {
    param([string] $Root)

    # Re-verify on every run, not just after extraction: the cache is a user-writable
    # directory whose contents may later be executed elevated, so a tampered file has to
    # be caught before it runs, not merely when it is unpacked.
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
    return $true
}

function Get-PwshPyUvPath {
    $found = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    if ($PwshPyOnWindows) {
        $candidates = @(
            (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe'),
            (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe')
        )
    } else {
        $candidates = @(
            (Join-Path $HOME '.local/bin/uv'),
            (Join-Path $HOME '.cargo/bin/uv'),
            '/usr/local/bin/uv'
        )
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Install-PwshPyUv {
    Write-Host 'uv was not found; installing it for the current user from https://astral.sh/uv ...'
    if ($PwshPyOnWindows) {
        # Windows PowerShell 5.1 still defaults to TLS 1.0/1.1, which astral.sh refuses.
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $installer = Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing
        Invoke-Expression $installer
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

function Invoke-PwshPyElevated {
    param([string[]] $ForwardArgs)

    # This function EXITS rather than returning a code, deliberately. A PowerShell function
    # returns everything written to the output stream, so returning a code from a function
    # that also runs a child would swallow the child's stdout into the return value and
    # print nothing. Exiting here keeps the child's output flowing straight to the console.
    $self = $PSCommandPath
    if (-not $PwshPyOnWindows) {
        # sudo keeps the console, so output and exit code flow back without a relay.
        & sudo (Get-PwshPyHostPath) -NoProfile -File $self @ForwardArgs
        exit $LASTEXITCODE
    }

    # A UAC child cannot inherit this console and Start-Process rejects -RedirectStandard*
    # together with -Verb RunAs, so the child writes its streams to files we replay here.
    $relay = Join-Path ([IO.Path]::GetTempPath()) ('pwshpy-pack-' + [Guid]::NewGuid().ToString('N'))
    $childArgs = @('-NoProfile', '-File', $self, '-PwshPyRelay', $relay) + $ForwardArgs
    $process = Start-Process -FilePath (Get-PwshPyHostPath) -ArgumentList $childArgs -Verb RunAs -Wait -PassThru
    foreach ($stream in @(@("$relay.out", $false), @("$relay.err", $true))) {
        if (Test-Path -LiteralPath $stream[0]) {
            $text = Get-Content -LiteralPath $stream[0] -Raw
            if ($text) { if ($stream[1]) { Write-Host $text -ForegroundColor Red } else { Write-Host $text -NoNewline } }
            Remove-Item -LiteralPath $stream[0] -Force -ErrorAction SilentlyContinue
        }
    }
    exit $process.ExitCode
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
$PwshPyRest = New-Object System.Collections.ArrayList
$PwshPyExpectRelay = $false

foreach ($argument in $args) {
    $text = [string]$argument
    if ($PwshPyExpectRelay) { $PwshPyRelay = $text; $PwshPyExpectRelay = $false; continue }
    switch -Exact ($text) {
        '-PwshPyClean' { $PwshPyClean = $true; continue }
        '-PwshPyNoInstallUv' { $PwshPyNoInstallUv = $true; continue }
        '-PwshPyElevate' { $PwshPyWantElevate = $true; continue }
        '-PwshPyInfo' { $PwshPyInfo = $true; continue }
        '-PwshPyHelp' { $PwshPyHelp = $true; continue }
        '-PwshPyRelay' { $PwshPyExpectRelay = $true; continue }
        default { [void]$PwshPyRest.Add($text) }
    }
}
$PwshPyForward = @($PwshPyRest.ToArray())

if ($PwshPyHelp) { Write-PwshPyHelp; exit 0 }

$PwshPyCacheDir = Join-Path (Get-PwshPyCacheRoot) $PwshPySha.Substring(0, 16)

if ($PwshPyInfo) { Write-PwshPyManifest -CacheDir $PwshPyCacheDir; exit 0 }

if ($PwshPyWantElevate -and -not (Test-PwshPyElevated)) {
    exit (Invoke-PwshPyElevated -ForwardArgs $PwshPyForward)
}

# --- unpack -----------------------------------------------------------------------------

if ($PwshPyClean -and (Test-Path -LiteralPath $PwshPyCacheDir)) {
    Remove-Item -LiteralPath $PwshPyCacheDir -Recurse -Force
}
if (-not (Test-PwshPyTree -Root $PwshPyCacheDir)) {
    Expand-PwshPyPayload -Destination $PwshPyCacheDir
    if (-not (Test-PwshPyTree -Root $PwshPyCacheDir)) {
        throw "the unpacked payload in $PwshPyCacheDir does not match its recorded hashes"
    }
}

# --- uv ---------------------------------------------------------------------------------

$PwshPyUv = Get-PwshPyUvPath
if (-not $PwshPyUv) {
    if ($PwshPyNoInstallUv) {
        # Written straight to stderr rather than through Write-Error: with
        # $ErrorActionPreference = 'Stop' the error stream terminates the script on the spot,
        # and the caller would get PowerShell's exit 1 instead of the 127 meant here.
        [Console]::Error.WriteLine('uv is not installed and -PwshPyNoInstallUv was given. Install it from https://astral.sh/uv')
        exit 127
    }
    Install-PwshPyUv
    $PwshPyUv = Get-PwshPyUvPath
}
if (-not $PwshPyUv) {
    [Console]::Error.WriteLine('uv is still not on PATH after installation. Install it manually from https://astral.sh/uv')
    exit 127
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
