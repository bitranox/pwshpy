#!/bin/sh
# Packed with pwshpy - https://github.com/bitranox/pwshpy
#
# This file carries the Python script '@@PWSHPY_ENTRY_NAME@@' and its local modules as a base64 tar.
# Running it unpacks them into a per-user cache, installs `uv` if this machine has none, runs the
# script, and exits with the script's own exit code. Nothing needs to be installed first, not even
# Python.
#
# It is strict POSIX sh, so it runs the same under any /bin/sh (dash, busybox ash, macOS sh, bash)
# and regardless of your login shell (zsh, fish, ...).
#
# See what is inside, without running any of it:   sh <this file> --pwshpy-info
# Unpack the sources to read or edit them:          uvx pwshpy unpack <this file> -o src
# Re-pack after editing, replacing this file:       uvx pwshpy pack src/@@PWSHPY_ENTRY_NAME@@ -o <this file> --force
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
# Run as root, it only runs a uv that root owns and no other account can modify (the file, the
# directory holding it, and every directory above that); any other uv it finds is skipped with a
# line on stderr, and a --pwshpy-uv path that fails the check is refused with exit code 127. As root
# it also runs its own helpers (ls, tar, base64, ...) only from the fixed system directories, never
# from the PATH it was given, and runs the script only on a Python uv manages (downloaded on first
# use unless uv's Python downloads are switched off), with uv started in / so that no .venv in or
# above your directory is ever run (the script itself still runs in your directory); UV_PYTHON,
# VIRTUAL_ENV, CONDA_PREFIX, UV_MANAGED_PYTHON, UV_NO_MANAGED_PYTHON, UV_PYTHON_INSTALL_DIR,
# UV_CONFIG_FILE, UV_PYTHON_INSTALL_MIRROR and UV_PYTHON_DOWNLOADS_JSON_URL are ignored, each with a
# line on stderr.
#
# Set PWSHPY_PACK_CACHE to choose where the payload unpacks (default: a per-user cache dir).

set -eu

# --- root: helpers from fixed system directories --------------------------------------------
# As root every helper this runner calls (ls, readlink, tar, base64, sha256sum, rm, mkdir, cut,
# openssl, ...) runs with full rights, so none may come from the PATH the caller handed over: a
# directory on it that another account can write would decide what they are, before the uv check
# below could say anything. They come from the system directories alone (user decision): /usr/bin,
# /bin, /usr/sbin, /sbin and the NixOS and Guix system profiles, not /usr/local, which a local
# install, a package manager run by hand, or a group-writable /usr/local/bin can populate. The
# caller's PATH is kept: the uv search still walks it, judging each candidate, and the packed script
# gets it back.
#
# The euid is asked of an id in those same directories, for the same reason, and never of one on
# PATH: a planted id answering "not root" would switch all of
# this off. When none of them answers with a decimal uid, the run is treated as root - it fails
# closed. That costs a non-root caller on such a system the per-user uv lookup (the root rule
# refuses a uv its own account owns), never safety; dying instead would cost the same caller the
# whole run.
pwshpy_euid() {
    for _e in /usr/bin/id /bin/id /usr/sbin/id /sbin/id /run/current-system/sw/bin/id \
        /run/current-system/profile/bin/id; do
        if [ -x "$_e" ]; then
            _uid=$("$_e" -u 2>/dev/null) || continue
            case $_uid in
                '' | *[!0-9]*) continue ;;
            esac
            printf '%s' "$_uid"
            return 0
        fi
    done
    printf '0'
}

PWSHPY_CALLER_PATH=${PATH:-}
PWSHPY_AS_ROOT=0
if [ "$(pwshpy_euid)" = "0" ]; then
    PWSHPY_AS_ROOT=1
fi
if [ "$PWSHPY_AS_ROOT" = "1" ]; then
    PATH=/usr/bin:/bin:/usr/sbin:/sbin:/run/current-system/sw/bin:/run/current-system/profile/bin
    export PATH
fi

PWSHPY_ENTRY=@@PWSHPY_ENTRY@@
PWSHPY_SHA='@@PWSHPY_PAYLOAD_SHA256@@'
# The fixed uv args as one single-quoted word holding a list of single-quoted words, expanded by the
# eval at the run: a dependency spec carrying $, ` or " is taken literally, never expanded here.
PWSHPY_UV_ARGS_RAW=@@PWSHPY_UV_ARGS@@
# The same args for a root run, which starts uv in /: a --with path relative to the caller's directory
# is spelled from "$PWSHPY_CALLER_PREFIX", set before the eval that expands this list.
PWSHPY_UV_ARGS_ANCHORED_RAW=@@PWSHPY_UV_ARGS_ANCHORED@@
# The generated shim a root run executes instead of the entry (it changes back to the caller's
# directory first); it travels in the payload, verified like every other file.
PWSHPY_SHIM='@@PWSHPY_SHIM@@'
PWSHPY_FILE_HASHES=@@PWSHPY_FILE_HASHES@@

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
    # The list is split on newlines alone and never globbed (a packed file named data[1].json must
    # not be matched against the working directory); the body runs with globbing back on.
    set -f
    for _line in $PWSHPY_FILE_HASHES; do
        IFS=$_oldifs
        set +f
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
    set +f
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
# does. The accounts trusted to own what root runs: root alone (a test seam widens this list).
PWSHPY_TRUSTED_UIDS=0

# Sets PWSHPY_ST_MODE / PWSHPY_ST_UID / PWSHPY_ST_GID from ls -ldn, the one listing every POSIX
# system has (stat's flags differ between GNU, BSD and busybox). IFS is pinned for the read alone:
# a caller splitting on ':' would otherwise cut the listing at the colon in its time of day.
pwshpy_stat() {
    _st_ls=$(ls -ldn "$1" 2>/dev/null) || return 1
    IFS=' ' read -r PWSHPY_ST_MODE _ PWSHPY_ST_UID PWSHPY_ST_GID _ <<__PWSHPY_LS__
$_st_ls
__PWSHPY_LS__
    return 0
}

# Whether the path last given to pwshpy_stat ("$1") carries an ACL. GNU ls marks one with '+';
# macOS puts '@' (extended attributes) in that column instead, hiding a '+', so there the ACL
# itself is asked for: ls -e prints one line per entry below the file's own. busybox ls marks
# neither, which is why no group write is accepted at all: on Linux an ACL granting anyone write
# shows as group write through its mask.
pwshpy_has_acl() {
    case $PWSHPY_ST_MODE in
        ??????????+*) return 0 ;;
        ??????????@*)
            # ls on purpose: it is the one tool that prints a macOS ACL.
            # shellcheck disable=SC2012
            if [ -n "$(ls -lde "$1" 2>/dev/null | sed -n '2p')" ]; then
                return 0
            fi
            ;;
    esac
    return 1
}

pwshpy_trusted_uid() {
    case " $PWSHPY_TRUSTED_UIDS " in
        *" $1 "*) return 0 ;;
    esac
    return 1
}

# Prints why "$1" (the uv file, or the directory holding it) may not be what root runs, or nothing:
# root owns it, it has no ACL, and neither its group nor anyone else may write it.
pwshpy_leaf_distrust() {
    if ! pwshpy_stat "$1"; then
        printf '%s' "$1 cannot be inspected"
        return 0
    fi
    if pwshpy_has_acl "$1"; then
        printf '%s' "$1 carries an ACL"
        return 0
    fi
    case $PWSHPY_ST_MODE in
        ????????w*)
            printf '%s' "$1 is writable by every account"
            return 0
            ;;
        ?????w*)
            printf '%s' "$1 is writable by group $PWSHPY_ST_GID"
            return 0
            ;;
    esac
    if ! pwshpy_trusted_uid "$PWSHPY_ST_UID"; then
        printf '%s' "$1 is owned by uid $PWSHPY_ST_UID, not root"
    fi
    return 0
}

# Prints why the directory "$1", ABOVE the one holding uv, lets another account swap what lies
# below it (rename an entry away and put another in its place), or nothing. Root must own it and
# it may carry no ACL; group or other write is accepted only with the sticky bit (/tmp), which
# lets nobody but an entry's owner rename or delete it.
pwshpy_ancestor_distrust() {
    if ! pwshpy_stat "$1"; then
        printf '%s' "$1 cannot be inspected"
        return 0
    fi
    if pwshpy_has_acl "$1"; then
        printf '%s' "$1 carries an ACL"
        return 0
    fi
    if ! pwshpy_trusted_uid "$PWSHPY_ST_UID"; then
        printf '%s' "$1 is owned by uid $PWSHPY_ST_UID, not root"
        return 0
    fi
    case $PWSHPY_ST_MODE in
        ?????????[tT]*) ;;
        ????????w*) printf '%s' "$1 is writable by every account and not sticky" ;;
        ?????w*) printf '%s' "$1 is writable by group $PWSHPY_ST_GID and not sticky" ;;
    esac
    return 0
}

# Prints why the uv at "$1" may not run as root, or nothing when it may. Walks the path one
# component at a time from /, resolving each symlink where it is met (readlink without -f, which
# older macOS lacks), so every real directory the path passes through is judged, the ones that
# hold a link included: whoever can rename any of them, or repoint a link, decides what runs. A
# link's own mode means nothing, so only its owner is judged. Reported in order: a link whose owner
# fails (at once), the directory holding uv, the first ancestor that fails, then the file. A relative path is refused: it names
# whatever the working directory holds. A directory symlink such as /bin -> usr/bin on a merged-/usr
# system is resolved like any other link.
pwshpy_uv_distrust() {
    case $1 in
        /*) ;;
        *)
            printf '%s' "$1 is not an absolute path"
            return 0
            ;;
    esac
    _w_cur=
    _w_next=
    _w_rest=${1#/}
    _w_hops=0
    _w_ancestor=$(pwshpy_ancestor_distrust /)
    while [ -n "$_w_rest" ]; do
        case $_w_rest in
            */*)
                _w_comp=${_w_rest%%/*}
                _w_rest=${_w_rest#*/}
                ;;
            *)
                _w_comp=$_w_rest
                _w_rest=
                ;;
        esac
        case $_w_comp in
            '' | .) continue ;;
            ..)
                _w_cur=${_w_cur%/*}
                continue
                ;;
        esac
        _w_next=$_w_cur/$_w_comp
        if [ -L "$_w_next" ]; then
            _w_hops=$((_w_hops + 1))
            if [ "$_w_hops" -gt 40 ]; then
                printf '%s' "$1 passes more than 40 symlinks"
                return 0
            fi
            if ! pwshpy_stat "$_w_next"; then
                printf '%s' "the symlink $_w_next cannot be inspected"
                return 0
            fi
            if ! pwshpy_trusted_uid "$PWSHPY_ST_UID"; then
                printf '%s' "the symlink $_w_next is owned by uid $PWSHPY_ST_UID, not root"
                return 0
            fi
            _w_target=$(readlink "$_w_next") || {
                printf '%s' "the symlink $_w_next cannot be read"
                return 0
            }
            case $_w_target in
                /*)
                    _w_cur=
                    _w_target=${_w_target#/}
                    ;;
            esac
            if [ -n "$_w_rest" ]; then
                _w_rest=$_w_target/$_w_rest
            else
                _w_rest=$_w_target
            fi
            continue
        fi
        # The last component is the file itself; everything before it is a directory on the way.
        [ -n "$_w_rest" ] || break
        _w_cur=$_w_next
        if [ -z "$_w_ancestor" ]; then
            _w_ancestor=$(pwshpy_ancestor_distrust "$_w_cur")
        fi
    done
    _w_why=$(pwshpy_leaf_distrust "${_w_cur:-/}")
    if [ -n "$_w_why" ]; then
        printf '%s' "its directory $_w_why"
        return 0
    fi
    if [ -n "$_w_ancestor" ]; then
        printf '%s' "its ancestor $_w_ancestor"
        return 0
    fi
    pwshpy_leaf_distrust "$_w_next"
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
        # The caller's PATH, not the fixed one this root run executes its helpers from.
        for _fu_dir in $PWSHPY_CALLER_PATH; do
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

# The variables through which a caller names the interpreter itself, past --python-preference
# only-managed (measured with uv 0.13: an explicit UV_PYTHON path and an active virtual or conda
# environment each run their own python), or that uv refuses beside that flag; then the ones that
# move where uv's managed Pythons live or come from (another install directory, config file, download
# mirror or download list), which would let the caller's environment supply the interpreter after all
# (user decision on [59]). A root run drops each one that is set, naming it on stderr; identical to
# the .ps1 runner.
pwshpy_drop_interpreter_variables() {
    for _iv in UV_PYTHON VIRTUAL_ENV CONDA_PREFIX UV_MANAGED_PYTHON UV_NO_MANAGED_PYTHON \
        UV_PYTHON_INSTALL_DIR UV_CONFIG_FILE UV_PYTHON_INSTALL_MIRROR UV_PYTHON_DOWNLOADS_JSON_URL; do
        if eval "[ -n \"\${$_iv+set}\" ]"; then
            printf '%s\n' "pwshpy: ignoring $_iv: as root no environment variable chooses the Python" >&2
            unset "$_iv"
        fi
    done
}

# Sets PWSHPY_CALLER_CWD to the physical working directory, a trailing newline in its name included
# (command substitution would strip it, so a sentinel character follows and is removed again).
pwshpy_caller_cwd() {
    _cwd=$(pwd -P && printf x) || pwshpy_die "cannot determine the working directory" 70
    _cwd=${_cwd%x}
    PWSHPY_CALLER_CWD=${_cwd%?}
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
    # As root the helpers keep their fixed PATH; the new uv's directory joins the caller's PATH,
    # which the uv search walks and the packed script gets back.
    if [ "$PWSHPY_AS_ROOT" = "1" ]; then
        PWSHPY_CALLER_PATH="$HOME/.local/bin:$PWSHPY_CALLER_PATH"
    else
        PATH="$HOME/.local/bin:$PATH"
        export PATH
    fi
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
    set -f
    for _line in $PWSHPY_FILE_HASHES; do
        IFS=$_oldifs
        [ -n "$_line" ] && printf '  %s\n' "${_line#*  }"
        IFS='
'
    done
    IFS=$_oldifs
    set +f
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
if [ "$PWSHPY_WANT_ELEVATE" = "1" ] && [ "$PWSHPY_AS_ROOT" != "1" ]; then
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
    PWSHPY_UV_HINT="As root only a uv no other account can modify is run: install uv where only root can write (root's own ~/.local/bin, the installer's default), or take group and other write off the directory named above (chmod go-w); a --pwshpy-uv pin must pass the same check"
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
# script unchanged - no argv encoding. Not root, uv runs the entry itself and reads its PEP 723 block
# directly. Prepend the script, then the fixed uv args (a single-quoted list, eval-expanded), then exec.

if [ "$PWSHPY_AS_ROOT" = "1" ]; then
    # uv picks the interpreter, and runs every one it considers to ask its version, so a python
    # planted in a PATH directory another account can write would run as root. As root uv considers
    # only the Pythons it manages (user decision), downloading one on first use unless downloads are
    # switched off; project discovery (the .python-version and uv.toml uv would read from the cache
    # and every directory above it) moves to /, which only root can write; and uv itself starts in /
    # (--directory), because it probes a .venv in its working directory and every one above, and
    # under only-managed even runs the script on that venv's python (measured with uv 0.13.0). So uv
    # runs the generated shim, which changes back to the caller's directory (handed over in
    # @@PWSHPY_CWD_ENV@@) before the entry runs; every path the caller gave relative to that
    # directory (a --with path, a relative PWSHPY_PACK_CACHE) is made absolute first.
    pwshpy_caller_cwd
    PWSHPY_CALLER_PREFIX=${PWSHPY_CALLER_CWD%/}/
    case $PWSHPY_CACHE in
        /*) ;;
        *) PWSHPY_CACHE=$PWSHPY_CALLER_PREFIX$PWSHPY_CACHE ;;
    esac
    set -- "$PWSHPY_CACHE/$PWSHPY_SHIM" "$@"
    eval "set -- $PWSHPY_UV_ARGS_ANCHORED_RAW \"\$@\""
    # The packed script runs with the PATH its caller gave: the fixed one was only for this runner's
    # own helpers.
    PATH=$PWSHPY_CALLER_PATH
    export PATH
    pwshpy_drop_interpreter_variables
    export "@@PWSHPY_CWD_ENV@@=$PWSHPY_CALLER_CWD"
    # The shim keeps its own arguments only when no out-of-band argument vector is set; one inherited
    # from the caller's environment must not replace them.
    unset @@PWSHPY_ARGV_ENV@@
    set -- --python-preference only-managed --project / --directory / "$@"
else
    set -- "$PWSHPY_CACHE/$PWSHPY_ENTRY" "$@"
    eval "set -- $PWSHPY_UV_ARGS_RAW \"\$@\""
fi
exec "$PWSHPY_UV" run --no-project "$@"
