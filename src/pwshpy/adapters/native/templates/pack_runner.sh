#!/bin/sh
# Packed with pwshpy - https://github.com/bitranox/pwshpy
#
# This file carries the Python script '@@PWSHPY_ENTRY@@' and its local modules as a base64 tar.
# Running it unpacks them into a per-user cache, installs `uv` if this machine has none, runs the
# script, and exits with the script's own exit code. Nothing needs to be installed first, not even
# Python.
#
# It is strict POSIX sh, so it runs the same under any /bin/sh (dash, busybox ash, macOS sh, bash)
# and regardless of your login shell (zsh, fish, ...).
#
# See what is inside, without running any of it:   sh <this file> --pwshpy-info
# Unpack the sources to read or edit them:          uvx pwshpy unpack <this file> -o src
# Re-pack after editing, replacing this file:       uvx pwshpy pack src/@@PWSHPY_ENTRY@@ -o <this file> --force
#
# Switches this wrapper understands; every other argument is passed to the packed script exactly as
# you typed it (empty strings, spaces, quotes and unicode all survive):
#     --pwshpy-help          show this help and exit
#     --pwshpy-info          show the payload manifest and exit
#     --pwshpy-clean         discard the cached extraction and unpack again
#     --pwshpy-no-install-uv fail instead of installing uv when it is missing
#     --pwshpy-elevate       relaunch under sudo first
#
# Set PWSHPY_PACK_CACHE to choose where the payload unpacks (default: a per-user cache dir).

set -eu

PWSHPY_ENTRY='@@PWSHPY_ENTRY@@'
PWSHPY_SHA='@@PWSHPY_PAYLOAD_SHA256@@'
PWSHPY_UV_ARGS_RAW="@@PWSHPY_UV_ARGS@@"
PWSHPY_FILE_HASHES='@@PWSHPY_FILE_HASHES@@'

# The base64 payload, as a heredoc so a very long blob needs no shell quoting.
pwshpy_payload_b64() {
    cat <<'__PWSHPY_PAYLOAD_B64__'
@@PWSHPY_PAYLOAD_B64@@
__PWSHPY_PAYLOAD_B64__
}

pwshpy_die() {
    printf '%s\n' "pwshpy pack: $1" >&2
    exit "${2:-1}"
}

pwshpy_show_help() {
    # Print the header comment block verbatim - no external help system to depend on.
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

# --- capability probes (choose portable tools once) -------------------------------------

# base64 decode: GNU/busybox use -d, macOS/BSD use -D; fall back to openssl.
pwshpy_b64_opt=""
pwshpy_use_openssl_b64=0
if printf 'aGk=' | base64 -d >/dev/null 2>&1; then
    pwshpy_b64_opt="-d"
elif printf 'aGk=' | base64 -D >/dev/null 2>&1; then
    pwshpy_b64_opt="-D"
elif printf 'aGk=' | base64 --decode >/dev/null 2>&1; then
    pwshpy_b64_opt="--decode"
elif command -v openssl >/dev/null 2>&1; then
    pwshpy_use_openssl_b64=1
else
    pwshpy_die "need a base64 decoder (base64 or openssl)"
fi

pwshpy_b64decode() {
    if [ "$pwshpy_use_openssl_b64" = "1" ]; then
        openssl base64 -d
    else
        base64 $pwshpy_b64_opt
    fi
}

# sha256: Linux has sha256sum, macOS has shasum.
if command -v sha256sum >/dev/null 2>&1; then
    pwshpy_sha256_of() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
    pwshpy_sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
    pwshpy_die "need sha256sum or shasum to verify the payload"
fi

# --- cache location ---------------------------------------------------------------------

pwshpy_cache_root() {
    if [ -n "${PWSHPY_PACK_CACHE:-}" ]; then
        printf '%s' "$PWSHPY_PACK_CACHE"
    elif [ -n "${XDG_CACHE_HOME:-}" ]; then
        printf '%s' "$XDG_CACHE_HOME/pwshpy-pack"
    else
        printf '%s' "$HOME/.cache/pwshpy-pack"
    fi
}

# --- payload + integrity ----------------------------------------------------------------

pwshpy_decode_payload_to() {
    pwshpy_payload_b64 | tr -d '[:space:]' | pwshpy_b64decode >"$1"
}

# Verify the extracted tree against the embedded per-file hashes. ANY failure - a mismatch, a
# missing file, or a transient read error from a file another process is mid-extraction on (this
# runs before the lock, so two cold starts DO overlap here) - returns non-zero so the caller
# re-extracts under the lock, never aborting the run.
pwshpy_verify_tree() {
    [ -d "$PWSHPY_CACHE" ] || return 1
    _oldifs=$IFS
    IFS='
'
    for _line in $PWSHPY_FILE_HASHES; do
        IFS=$_oldifs
        if [ -n "$_line" ]; then
            _expected=${_line%%  *}
            _path=${_line#*  }
            _target="$PWSHPY_CACHE/$_path"
            if [ ! -f "$_target" ]; then
                IFS=$_oldifs
                return 1
            fi
            _actual=$(pwshpy_sha256_of "$_target" 2>/dev/null) || {
                IFS=$_oldifs
                return 1
            }
            if [ "$_actual" != "$_expected" ]; then
                IFS=$_oldifs
                return 1
            fi
        fi
        IFS='
'
    done
    IFS=$_oldifs
    return 0
}

pwshpy_extract() {
    rm -rf "$PWSHPY_CACHE"
    mkdir -p "$PWSHPY_CACHE"
    _archive="$PWSHPY_CACHE/.payload.tar"
    pwshpy_decode_payload_to "$_archive"
    # Verify the whole payload before extracting: a blob damaged in transit fails with a clear
    # message here rather than a cryptic tar error.
    _actual=$(pwshpy_sha256_of "$_archive")
    if [ "$_actual" != "$PWSHPY_SHA" ]; then
        rm -f "$_archive"
        pwshpy_die "the embedded payload is corrupt: sha256 $_actual does not match the expected $PWSHPY_SHA"
    fi
    tar xf "$_archive" -C "$PWSHPY_CACHE"
    rm -f "$_archive"
}

# Serialize extraction across processes with a mkdir lock (atomic on POSIX). The warm path (a
# verified tree) never locks, so a normal run pays no lock cost.
pwshpy_unpack_locked() {
    mkdir -p "$(dirname "$PWSHPY_CACHE")"
    _lock="$PWSHPY_CACHE.lock"
    _attempt=0
    while [ "$_attempt" -lt 600 ]; do
        if mkdir "$_lock" 2>/dev/null; then
            if pwshpy_verify_tree; then
                rmdir "$_lock" 2>/dev/null || true
                return 0
            fi
            pwshpy_extract
            if ! pwshpy_verify_tree; then
                rmdir "$_lock" 2>/dev/null || true
                pwshpy_die "the unpacked payload in $PWSHPY_CACHE does not match its recorded hashes"
            fi
            rmdir "$_lock" 2>/dev/null || true
            return 0
        fi
        # Another process holds the lock; it may already have finished.
        if pwshpy_verify_tree; then return 0; fi
        sleep 1
        _attempt=$((_attempt + 1))
    done
    pwshpy_die "timed out waiting to unpack $PWSHPY_CACHE (another process holds $_lock)"
}

# --- uv ---------------------------------------------------------------------------------

pwshpy_find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for _c in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /usr/local/bin/uv \
        "${XDG_BIN_HOME:-}/uv" "${CARGO_HOME:-}/bin/uv"; do
        if [ -n "$_c" ] && [ -x "$_c" ]; then
            printf '%s' "$_c"
            return 0
        fi
    done
    return 1
}

pwshpy_install_uv() {
    printf '%s\n' "uv was not found; installing it for the current user from https://astral.sh/uv ..." >&2
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        pwshpy_die "need curl or wget to install uv" 127
    fi
    PATH="$HOME/.local/bin:$PATH"
    export PATH
}

pwshpy_write_manifest() {
    printf 'entry      : %s\n' "$PWSHPY_ENTRY"
    printf 'payload    : sha256 %s\n' "$PWSHPY_SHA"
    printf 'uv args    : %s\n' "$PWSHPY_UV_ARGS_RAW"
    printf 'cache dir  : %s\n' "$PWSHPY_CACHE"
    printf 'files      :\n'
    _oldifs=$IFS
    IFS='
'
    for _line in $PWSHPY_FILE_HASHES; do
        IFS=$_oldifs
        [ -n "$_line" ] && printf '  %s\n' "${_line#*  }"
        IFS='
'
    done
    IFS=$_oldifs
}

# --- argument split ---------------------------------------------------------------------
# Wrapper switches are consumed here; the first non --pwshpy-* argument (or a literal --) ends
# option parsing and everything from there is the packed script's own arguments.

PWSHPY_CLEAN=0
PWSHPY_NO_INSTALL_UV=0
PWSHPY_WANT_ELEVATE=0
PWSHPY_INFO=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --pwshpy-help)
            pwshpy_show_help
            exit 0
            ;;
        --pwshpy-info)
            PWSHPY_INFO=1
            shift
            ;;
        --pwshpy-clean)
            PWSHPY_CLEAN=1
            shift
            ;;
        --pwshpy-no-install-uv)
            PWSHPY_NO_INSTALL_UV=1
            shift
            ;;
        --pwshpy-elevate)
            PWSHPY_WANT_ELEVATE=1
            shift
            ;;
        --)
            shift
            break
            ;;
        *) break ;;
    esac
done

if [ "$PWSHPY_WANT_ELEVATE" = "1" ] && [ "$(id -u)" != "0" ]; then
    exec sudo "$0" "$@"
fi

PWSHPY_CACHE="$(pwshpy_cache_root)/$(printf '%s' "$PWSHPY_SHA" | cut -c1-16)"

if [ "$PWSHPY_INFO" = "1" ]; then
    pwshpy_write_manifest
    exit 0
fi

# --- unpack -----------------------------------------------------------------------------

if [ "$PWSHPY_CLEAN" = "1" ]; then
    rm -rf "$PWSHPY_CACHE"
fi
if ! pwshpy_verify_tree; then
    pwshpy_unpack_locked
fi

# --- uv ---------------------------------------------------------------------------------

if ! PWSHPY_UV="$(pwshpy_find_uv)"; then
    if [ "$PWSHPY_NO_INSTALL_UV" = "1" ]; then
        pwshpy_die "uv is not installed and --pwshpy-no-install-uv was given. Install it from https://astral.sh/uv" 127
    fi
    pwshpy_install_uv
    if ! PWSHPY_UV="$(pwshpy_find_uv)"; then
        pwshpy_die "uv is still not on PATH after installation. Install it manually from https://astral.sh/uv" 127
    fi
fi

# --- run --------------------------------------------------------------------------------
# POSIX sh forwards "$@" intact, so arguments (empty strings, spaces, quotes, unicode) reach the
# script unchanged and uv reads the entry's PEP 723 block directly - no shim, no argv encoding.
# Prepend the entry, then the fixed uv args (a single-quoted list, eval-expanded), then exec.

set -- "$PWSHPY_CACHE/$PWSHPY_ENTRY" "$@"
eval "set -- $PWSHPY_UV_ARGS_RAW \"\$@\""
exec "$PWSHPY_UV" run --no-project "$@"
