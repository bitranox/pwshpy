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
#     --pwshpy-elevate       relaunch under sudo first; exit 77 if sudo refuses
#     --pwshpy-uv PATH       run the uv at this absolute path instead of searching for one
#
# Run as root, it only runs a uv that root owns and no other account can modify (the file and every
# directory holding it); any other uv it finds is skipped with a line on stderr, and a --pwshpy-uv
# path that fails the check is refused with exit code 127.
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

# Serialize extraction across processes with a lock file created under noclobber, which the shell
# itself opens with O_EXCL. Not `mkdir "$_lock"`: the mkdir utility is not always exclusive - the
# uutils coreutils mkdir that Ubuntu 26.04 ships exits 0 for BOTH of two racing calls on one path
# about a third of the time, letting two cold starts extract into the same directory at once.
# The warm path (a verified tree) never locks, so a normal run pays no lock cost.
pwshpy_take_lock() {
    (set -C && : >"$_lock") 2>/dev/null
}

# Release the lock, and drop the EXIT trap that releases it if this process dies holding it.
pwshpy_release_lock() {
    rm -f "$_lock"
    trap - EXIT
}

pwshpy_unpack_locked() {
    mkdir -p "$(dirname "$PWSHPY_CACHE")"
    _lock="$PWSHPY_CACHE.lock"
    _attempt=0
    while [ "$_attempt" -lt 600 ]; do
        if pwshpy_take_lock; then
            # A corrupt payload or a failing tar exits through pwshpy_die or set -e; without this
            # trap the lock would outlive the process and every later start would wait it out.
            trap 'rm -f "$_lock"' EXIT
            if pwshpy_verify_tree; then
                pwshpy_release_lock
                return 0
            fi
            pwshpy_extract
            if ! pwshpy_verify_tree; then
                pwshpy_release_lock
                pwshpy_die "the unpacked payload in $PWSHPY_CACHE does not match its recorded hashes"
            fi
            pwshpy_release_lock
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

# As root the uv found here runs with full rights, so a uv another account can replace would hand
# that account root: where it was found (PATH, a fixed location) proves nothing, who can write it
# does. Prints why "$1" may not run as root, or nothing when it may: root owns it, group 0 or no
# group may write it, and nobody else may. Group 0 is accepted because its members are root
# equivalents on every system this targets; any other writable group is not. A mode with an ACL
# ('+') is refused outright, because its group bits then show the ACL mask, not who may write.
pwshpy_path_distrust() {
    _pd_ls=$(ls -ldn "$1" 2>/dev/null) || {
        printf '%s' "$1 cannot be inspected"
        return 0
    }
    # IFS pinned for this read alone: a caller splitting on another IFS (the PATH walk splits on
    # ':') would otherwise cut the listing at the colon in its time of day.
    IFS=' ' read -r _pd_mode _ _pd_uid _pd_gid _ <<__PWSHPY_LS__
$_pd_ls
__PWSHPY_LS__
    case $_pd_mode in
        ??????????+*) printf '%s' "$1 carries an ACL" ;;
        ????????w*) printf '%s' "$1 is writable by every account" ;;
        *)
            if [ "$_pd_uid" != "0" ]; then
                printf '%s' "$1 is owned by uid $_pd_uid, not root"
            elif [ "$_pd_gid" != "0" ]; then
                case $_pd_mode in ?????w*) printf '%s' "$1 is writable by group $_pd_gid" ;; esac
            fi
            ;;
    esac
    return 0
}

# Prints why the uv at "$1" may not run as root, or nothing when it may. Judges the directory
# holding it, then follows a symlink one hop at a time (readlink without -f, which older macOS
# lacks) and judges each hop's directory, then the file the chain ends at: whoever can rewrite a
# link, or the directory holding one, decides what runs as much as whoever can write the target.
# A relative path is refused: it names whatever the working directory holds.
pwshpy_uv_distrust() {
    _ud_path=$1
    _ud_hops=0
    while :; do
        case $_ud_path in
            /*) ;;
            *)
                printf '%s' "$_ud_path is not an absolute path"
                return 0
                ;;
        esac
        _ud_dir=${_ud_path%/*}
        [ -n "$_ud_dir" ] || _ud_dir=/
        _ud_why=$(pwshpy_path_distrust "$_ud_dir")
        if [ -n "$_ud_why" ]; then
            printf '%s' "its directory $_ud_why"
            return 0
        fi
        [ -L "$_ud_path" ] || break
        _ud_hops=$((_ud_hops + 1))
        if [ "$_ud_hops" -gt 40 ]; then
            printf '%s' "$1 is a chain of more than 40 symlinks"
            return 0
        fi
        _ud_target=$(readlink "$_ud_path") || {
            printf '%s' "the symlink $_ud_path cannot be read"
            return 0
        }
        case $_ud_target in
            /*) _ud_path=$_ud_target ;;
            *) _ud_path=$_ud_dir/$_ud_target ;;
        esac
    done
    pwshpy_path_distrust "$_ud_path"
    return 0
}

# Whether "$1" is a uv this run may execute. Not root: any executable, exactly as before. As root:
# a regular file that passes pwshpy_uv_distrust; a refused one is named on stderr so the reason a
# different uv ran (or none) is visible.
pwshpy_uv_acceptable() {
    if [ -z "$1" ] || [ ! -x "$1" ]; then
        return 1
    fi
    [ "$PWSHPY_AS_ROOT" = "1" ] || return 0
    [ -f "$1" ] || return 1
    _ua_why=$(pwshpy_uv_distrust "$1")
    if [ -z "$_ua_why" ]; then
        return 0
    fi
    printf '%s\n' "pwshpy: skipping uv at $1: $_ua_why (as root only a uv no other account can modify is run)" >&2
    return 1
}

# Prints the uv to run. Not root: the first uv on PATH, then the fixed install locations. As root:
# EVERY uv on PATH in order, then the fixed locations, the first that passes pwshpy_uv_acceptable -
# a planted uv early on PATH must not hide a trusted one later on it.
pwshpy_find_uv() {
    if [ "$PWSHPY_AS_ROOT" != "1" ]; then
        if command -v uv >/dev/null 2>&1; then
            command -v uv
            return 0
        fi
    else
        _fu_oldifs=$IFS
        IFS=:
        set -f
        for _fu_dir in $PATH; do
            # The list is split once, up front; the body runs with the caller's IFS and globbing.
            IFS=$_fu_oldifs
            set +f
            # An empty PATH entry means the working directory; spelled "." it is refused as relative.
            if pwshpy_uv_acceptable "${_fu_dir:-.}/uv"; then
                printf '%s' "${_fu_dir:-.}/uv"
                return 0
            fi
        done
        IFS=$_fu_oldifs
        set +f
    fi
    # The installer honours XDG_BIN_HOME / CARGO_HOME; each is a candidate only when it is set, or
    # an unset one would probe /uv and /bin/uv.
    for _c in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /usr/local/bin/uv \
        "${XDG_BIN_HOME:+$XDG_BIN_HOME/uv}" "${CARGO_HOME:+$CARGO_HOME/bin/uv}"; do
        if pwshpy_uv_acceptable "$_c"; then
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
PWSHPY_UV_PIN=
# The consumed value-less switches an elevated relaunch must pass on again, so the list needs no
# quoting; --pwshpy-uv carries a path and is passed on separately, quoted.
PWSHPY_FWD=
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
            PWSHPY_FWD="$PWSHPY_FWD --pwshpy-clean"
            shift
            ;;
        --pwshpy-no-install-uv)
            PWSHPY_NO_INSTALL_UV=1
            PWSHPY_FWD="$PWSHPY_FWD --pwshpy-no-install-uv"
            shift
            ;;
        --pwshpy-elevate)
            PWSHPY_WANT_ELEVATE=1
            shift
            ;;
        --pwshpy-uv)
            # 64 (EX_USAGE): a malformed switch is neither a missing uv (127) nor the script's own code.
            if [ "$#" -lt 2 ]; then
                pwshpy_die "--pwshpy-uv needs the absolute path of a uv executable" 64
            fi
            case $2 in
                /*) ;;
                *) pwshpy_die "--pwshpy-uv needs an absolute path, got: $2" 64 ;;
            esac
            PWSHPY_UV_PIN=$2
            shift 2
            ;;
        --)
            shift
            break
            ;;
        *) break ;;
    esac
done

PWSHPY_CACHE="$(pwshpy_cache_root)/$(printf '%s' "$PWSHPY_SHA" | cut -c1-16)"

# Before the elevate block: the manifest needs no root, so --pwshpy-info never prompts for sudo.
if [ "$PWSHPY_INFO" = "1" ]; then
    pwshpy_write_manifest
    exit 0
fi

# Ask for the password with `sudo -v` first: sudo exits 1 when it refuses, which is also an ordinary
# exit code of the script, so only asking separately lets a refusal exit 77 (EX_NOPERM) instead.
# No sudo at all is a missing tool (127), not a refusal.
# Relaunch through /bin/sh rather than exec-ing "$0": a packed file is usually not executable.
# The explicit -- keeps the script's own arguments from being parsed as wrapper switches again.
if [ "$PWSHPY_WANT_ELEVATE" = "1" ] && [ "$(id -u)" != "0" ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        printf '%s\n' "pwshpy: sudo not found; cannot elevate" >&2
        exit 127
    fi
    if ! sudo -v; then
        printf '%s\n' "pwshpy: elevation declined (sudo did not grant root)" >&2
        exit 77
    fi
    # A sudo that grants -v but then refuses the command itself (timestamp_timeout=0, sudoers
    # allowing only -v) exits with that sudo's own code, which no wrapper can tell from the script's.
    # The pin goes in front of the script's arguments as two quoted words, so a path with a space
    # survives; the value-less switches carry no values, so splitting their unquoted list is
    # exactly what is wanted.
    if [ -n "$PWSHPY_UV_PIN" ]; then
        set -- --pwshpy-uv "$PWSHPY_UV_PIN" -- "$@"
    else
        set -- -- "$@"
    fi
    # shellcheck disable=SC2086
    exec sudo /bin/sh "$0" $PWSHPY_FWD "$@"
fi

# Asked once, after any relaunch: the trust rule below applies to whatever runs as root, sudo or not.
PWSHPY_AS_ROOT=0
if [ "$(id -u)" = "0" ]; then
    PWSHPY_AS_ROOT=1
fi

# --- unpack -----------------------------------------------------------------------------

if [ "$PWSHPY_CLEAN" = "1" ]; then
    rm -rf "$PWSHPY_CACHE"
fi
if ! pwshpy_verify_tree; then
    pwshpy_unpack_locked
fi

# --- uv ---------------------------------------------------------------------------------

# What to say when no usable uv is left: as root, also how to provide one the trust rule accepts.
PWSHPY_UV_HINT="Install it from https://astral.sh/uv"
if [ "$PWSHPY_AS_ROOT" = "1" ]; then
    PWSHPY_UV_HINT="As root only a uv owned by root that no other account can modify is run: install one there (for example under /usr/local/bin) or pass --pwshpy-uv PATH"
fi

if [ -n "$PWSHPY_UV_PIN" ]; then
    # A pin is the whole answer: no search and no install behind it, so a refused pin fails here.
    if [ ! -f "$PWSHPY_UV_PIN" ] || [ ! -x "$PWSHPY_UV_PIN" ]; then
        pwshpy_die "no executable uv at the path given with --pwshpy-uv: $PWSHPY_UV_PIN" 127
    fi
    if [ "$PWSHPY_AS_ROOT" = "1" ]; then
        PWSHPY_PIN_WHY=$(pwshpy_uv_distrust "$PWSHPY_UV_PIN")
        if [ -n "$PWSHPY_PIN_WHY" ]; then
            printf '%s\n' "pwshpy: refusing the uv given with --pwshpy-uv: $PWSHPY_UV_PIN: $PWSHPY_PIN_WHY. $PWSHPY_UV_HINT" >&2
            exit 127
        fi
    fi
    PWSHPY_UV=$PWSHPY_UV_PIN
elif ! PWSHPY_UV="$(pwshpy_find_uv)"; then
    if [ "$PWSHPY_NO_INSTALL_UV" = "1" ]; then
        pwshpy_die "no usable uv was found and --pwshpy-no-install-uv was given. $PWSHPY_UV_HINT" 127
    fi
    pwshpy_install_uv
    if ! PWSHPY_UV="$(pwshpy_find_uv)"; then
        pwshpy_die "no usable uv was found after installation. $PWSHPY_UV_HINT" 127
    fi
fi

# --- run --------------------------------------------------------------------------------
# POSIX sh forwards "$@" intact, so arguments (empty strings, spaces, quotes, unicode) reach the
# script unchanged and uv reads the entry's PEP 723 block directly - no shim, no argv encoding.
# Prepend the entry, then the fixed uv args (a single-quoted list, eval-expanded), then exec.

set -- "$PWSHPY_CACHE/$PWSHPY_ENTRY" "$@"
eval "set -- $PWSHPY_UV_ARGS_RAW \"\$@\""
exec "$PWSHPY_UV" run --no-project "$@"
