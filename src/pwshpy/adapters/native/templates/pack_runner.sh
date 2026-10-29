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
# from the PATH it was given, and runs the script only on a Python uv manages in root's own data
# directory (<home from the passwd database>/.local/share/uv/python; downloaded on first use unless
# UV_PYTHON_DOWNLOADS is never or manual), found with `uv python find --system` and handed to uv run
# as --python, after the same check as uv itself, so no .venv anywhere is ever run (the script
# itself still runs in your directory). That directory passes the same check before uv searches it
# (uv runs every Python it finds there), and so does every file and directory below it (a regular
# file named .lock aside, mode only); one another account can modify is refused with exit code 127.
# uv's cache is root's own too (<home from the passwd database>/.cache/uv, named on stderr). Every
# uv call it makes as root reads no uv.toml (--no-config). UV_PYTHON, VIRTUAL_ENV, CONDA_PREFIX,
# UV_MANAGED_PYTHON, UV_NO_MANAGED_PYTHON, UV_PYTHON_INSTALL_DIR, UV_CONFIG_FILE,
# UV_PYTHON_INSTALL_MIRROR, UV_PYTHON_DOWNLOADS_JSON_URL, UV_ASTRAL_MIRROR_URL,
# UV_PYPY_INSTALL_MIRROR, UV_GRAALPY_INSTALL_MIRROR, UV_PYODIDE_INSTALL_MIRROR, PYTHONHOME,
# PYTHONPATH, PYTHONUSERBASE, UV_INDEX_URL, UV_DEFAULT_INDEX, UV_INDEX, UV_EXTRA_INDEX_URL,
# UV_FIND_LINKS, UV_INSECURE_HOST, UV_INSECURE_NO_ZIP_VALIDATION, UV_CACHE_DIR, UV_PYTHON_CACHE_DIR,
# UV_ENV_FILE, HTTPS_PROXY, HTTP_PROXY, ALL_PROXY (and their lower-case forms), SSL_CERT_FILE,
# SSL_CERT_DIR, SSL_CLIENT_CERT, UV_NATIVE_TLS, UV_SYSTEM_CERTS, UV_KEYRING_PROVIDER, every variable
# whose name starts with GIT_ (git, which uv runs for a git source, reads them) or GCM_ (Git
# Credential Manager, the credential helper git runs, reads them), SSH_ASKPASS and PASSWORD_STORE_DIR
# are ignored, each with a line on stderr; git reads no global config file (GIT_CONFIG_GLOBAL=/dev/null,
# so not the ~/.gitconfig or $XDG_CONFIG_HOME/git/config of the HOME it was given), and for a script
# with a git source a git older than 2.32, which ignores that variable, is refused with exit code 127;
# the user site-packages is off (PYTHONNOUSERSITE=1) and uv run reads no .env file (--no-env-file).
# In a network that reaches the internet only through a proxy, a root run can download nothing: the
# managed Python and every package the script needs must already be in root's directories named
# above.
#
# Set PWSHPY_PACK_CACHE to choose where the payload unpacks (default: a per-user cache dir).
# As root it unpacks and runs only from a cache directory that passes the same check as uv's
# managed-Python directory (itself, every directory above it and everything below it), or exits 127.

set -eu

# --- root: helpers from fixed system directories --------------------------------------------
# As root every helper this runner calls (ls, readlink, tar, base64, sha256sum, rm, mkdir, cut,
# openssl, ...) runs with full rights, so none may come from the PATH the caller handed over: a
# directory on it that another account can write would decide what they are, before the uv check
# below could say anything. They come from the system directories alone (user decision): /usr/bin,
# /bin, /usr/sbin, /sbin and the NixOS and Guix system profiles, not /usr/local, which a local
# install, a package manager run by hand, or a group-writable /usr/local/bin can populate. The
# caller's PATH is kept: the uv search still walks it, judging each candidate; uv itself sees only
# its directories no other account can modify, and the packed script gets all of it back.
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
# Where a root run starts uv: a directory only root can write, so no .venv, .python-version or
# uv.toml in it or above it can be planted by another account. Named once so every uv call a root
# run makes uses the same one.
PWSHPY_ANCHOR=/
# The packed script's requires-python ('' for none): the request a root run hands `uv python find`.
PWSHPY_PYTHON_REQUEST=@@PWSHPY_PYTHON_REQUEST@@
# 1 when the packed script has a git source (pack-time detection, which errs towards 1): a root run then
# refuses a git older than 2.32.
PWSHPY_GIT_SOURCE=@@PWSHPY_GIT_SOURCE@@

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

# Prints why the program at "$1" (a uv, or the Python uv found) may not run as root, or nothing when
# it may. Walks the path one component at a time from /, resolving each symlink where it is met
# (readlink without -f, which older macOS lacks), so every real directory the path passes through is
# judged, the ones that hold a link included: whoever can rename any of them, or repoint a link,
# decides what runs. A link's own mode means nothing, so only its owner is judged. Reported in order:
# a link whose owner fails (at once), the directory holding the program, the first ancestor that
# fails, then the file. A relative path is refused: it names whatever the working directory holds. A
# directory symlink such as /bin -> usr/bin on a merged-/usr system is resolved like any other link.
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

# Prints the directories of the PATH "$1" that a root run lets uv see, colon-joined, and names each
# one it leaves out on stderr. uv runs git for a git dependency (and a build backend may too) by
# searching PATH, so a directory another account can write would decide what runs as root; each is
# judged by the same walk as a uv, with "/." so the walk judges the directory itself as the leaf.
# An empty or relative entry names the working directory and is left out like any untrusted one.
pwshpy_trusted_path() {
    _tp_out=
    _tp_oldifs=$IFS
    IFS=:
    set -f
    for _tp_dir in $1; do
        # The list is split once, up front; the body runs with the caller's IFS and globbing.
        IFS=$_tp_oldifs
        set +f
        _tp_why=$(pwshpy_uv_distrust "${_tp_dir:-.}/.")
        if [ -n "$_tp_why" ]; then
            _tp_why="$_tp_why (as root uv runs git and other tools only from directories no other account"
            printf '%s\n' "pwshpy: leaving ${_tp_dir:-.} off uv's PATH: $_tp_why can modify)" >&2
            continue
        fi
        _tp_out=${_tp_out:+$_tp_out:}$_tp_dir
    done
    IFS=$_tp_oldifs
    set +f
    printf '%s' "$_tp_out"
}

# Prints the git the PATH "$1" finds, or nothing. The search runs in a subshell, so this runner's own
# PATH stays the fixed one, and command -v only searches, never runs it. An empty PATH holds no git:
# searched, it would name the working directory.
pwshpy_git_on() {
    [ -n "$1" ] || return 0
    (
        PATH=$1
        command -v git
    ) || :
}

# Returns when the git "$1" is 2.32 or later, else exits 127 (user decision on [116]): git honours
# GIT_CONFIG_GLOBAL from 2.32 on, and an older one run by uv as root reads the caller's global git
# configuration, whose core.fsmonitor, core.sshCommand or credential.helper runs a command as root. A
# version this cannot read, or a git --version that fails, is refused too: the check fails closed.
pwshpy_require_git_config_global() {
    _gc_why="as root it would read the caller's git configuration (a script with a git source needs git"
    _gc_why="$_gc_why 2.32 or later as root): upgrade git, or run the script without root"
    _gc_out=$("$1" --version </dev/null 2>/dev/null) || _gc_out=
    # The first line only, on purpose: a git that prints a warning line before its "git version" line
    # names no version here and is refused, which fails closed.
    _gc_nl='
'
    _gc_out=${_gc_out%%"$_gc_nl"*}
    case $_gc_out in
        'git version '*.*) _gc_version=${_gc_out#git version } ;;
        *) _gc_version= ;;
    esac
    _gc_major=${_gc_version%%.*}
    _gc_minor=${_gc_version#*.}
    # What follows the minor number (".1.windows.1", "rc0", " (Apple Git-145)") does not matter.
    _gc_minor=${_gc_minor%%[!0-9]*}
    case $_gc_major:$_gc_minor in
        [0-9]*:[0-9]*) ;;
        *) _gc_version= ;;
    esac
    case $_gc_major in
        *[!0-9]* | ???????*) _gc_version= ;;
    esac
    case $_gc_minor in
        ???????*) _gc_version= ;;
    esac
    if [ -z "$_gc_version" ]; then
        _gc_unread="git --version names no version this runner can read, and a git older than 2.32 ignores"
        pwshpy_die "refusing git at $1: $_gc_unread GIT_CONFIG_GLOBAL, so $_gc_why" 127
    fi
    if [ "$_gc_major" -gt 2 ] || { [ "$_gc_major" -eq 2 ] && [ "$_gc_minor" -ge 32 ]; }; then
        return 0
    fi
    pwshpy_die "refusing git ${_gc_version%% *} at $1: it ignores GIT_CONFIG_GLOBAL, so $_gc_why" 127
}

# The variables through which a caller names the interpreter itself, past --python-preference
# only-managed (measured with uv 0.13: an explicit UV_PYTHON path and an active virtual or conda
# environment each run their own python), or that uv refuses beside that flag; then the ones that
# move where uv's managed Pythons live or come from (another install directory, config file, download
# mirror or download list; uv 0.13 also fetches CPython through UV_ASTRAL_MIRROR_URL and PyPy, GraalPy
# and Pyodide through their own mirror variables, measured), which would let the caller's environment
# supply the interpreter after all (user decision on [59]); then the library variables the interpreter
# reads itself; then the ones that name where uv takes packages, its cache or an env file from; then
# the ones that decide how uv connects. A root run drops each one that is set, naming it on stderr, and
# turns the user's own site-packages off; identical to the .ps1 runner.
pwshpy_drop_interpreter_variables() {
    _iv_why="as root only a uv-managed Python from the running account's own data directory runs"
    for _iv in UV_PYTHON VIRTUAL_ENV CONDA_PREFIX UV_MANAGED_PYTHON UV_NO_MANAGED_PYTHON \
        UV_PYTHON_INSTALL_DIR UV_CONFIG_FILE UV_PYTHON_INSTALL_MIRROR UV_PYTHON_DOWNLOADS_JSON_URL \
        UV_ASTRAL_MIRROR_URL UV_PYPY_INSTALL_MIRROR UV_GRAALPY_INSTALL_MIRROR UV_PYODIDE_INSTALL_MIRROR; do
        if eval "[ -n \"\${$_iv+set}\" ]"; then
            printf '%s\n' "pwshpy: ignoring $_iv: $_iv_why" >&2
            unset "$_iv"
        fi
    done
    # The interpreter reads these itself, whichever one runs: PYTHONHOME replaces its standard library,
    # PYTHONPATH puts the caller's directories ahead of it and PYTHONUSERBASE names the user
    # site-packages, so each would run code the caller chose as root.
    for _iv in PYTHONHOME PYTHONPATH PYTHONUSERBASE; do
        if eval "[ -n \"\${$_iv+set}\" ]"; then
            _iv_text="as root the Python runs with its own library paths and no user site-packages"
            printf '%s\n' "pwshpy: ignoring $_iv: $_iv_text" >&2
            unset "$_iv"
        fi
    done
    # Without PYTHONUSERBASE the user site-packages comes from HOME, which some sudo setups keep, so a
    # .pth the caller put in ~/.local/lib/pythonX.Y/site-packages would run as root (measured): off.
    PYTHONNOUSERSITE=1
    export PYTHONNOUSERSITE
    # Where uv takes packages (an index, a find-links location, a host it skips TLS checks for), how it
    # checks a wheel's archive (UV_INSECURE_NO_ZIP_VALIDATION turns that off), its caches and an env file
    # from: each is the caller's to choose, and an env file can set PYTHONPATH again (uv run loads
    # UV_ENV_FILE despite --no-config, measured with uv 0.13). A root run uses uv's defaults (user
    # decision, X21); the run below also passes --no-env-file, which no variable undoes.
    for _iv in UV_INDEX_URL UV_DEFAULT_INDEX UV_INDEX UV_EXTRA_INDEX_URL UV_FIND_LINKS UV_INSECURE_HOST \
        UV_INSECURE_NO_ZIP_VALIDATION UV_CACHE_DIR UV_PYTHON_CACHE_DIR UV_ENV_FILE; do
        if eval "[ -n \"\${$_iv+set}\" ]"; then
            _iv_text="as root uv uses its defaults for package sources, archive checks, caches and env files"
            printf '%s\n' "pwshpy: ignoring $_iv: $_iv_text" >&2
            unset "$_iv"
        fi
    done
    # How uv connects: a proxy (uv 0.13 sends https through HTTPS_PROXY, http through HTTP_PROXY and
    # both through ALL_PROXY, each also in lower case, measured), the certificate roots it trusts
    # (SSL_CERT_FILE and SSL_CERT_DIR replace them, measured; UV_NATIVE_TLS and UV_SYSTEM_CERTS switch to
    # the platform store), the client certificate it presents, and a keyring helper it runs for
    # credentials (UV_KEYRING_PROVIDER=subprocess runs `keyring` from PATH). Each lets the caller decide
    # what the default index serves to a root run, or what runs as root; NO_PROXY only exempts hosts
    # from a proxy and changes nothing once these are gone. The packed script runs without them too.
    for _iv in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy ALL_PROXY all_proxy SSL_CERT_FILE SSL_CERT_DIR \
        SSL_CLIENT_CERT UV_NATIVE_TLS UV_SYSTEM_CERTS UV_KEYRING_PROVIDER; do
        if eval "[ -n \"\${$_iv+set}\" ]"; then
            _iv_text="as root uv connects with its own defaults: no proxy, its built-in certificate roots, no keyring"
            printf '%s\n' "pwshpy: ignoring $_iv: $_iv_text" >&2
            unset "$_iv"
        fi
    done
    # git, which uv runs for a git source (a PEP 723 dependency or a --with value), reads every GIT_
    # variable: GIT_CONFIG_COUNT with GIT_CONFIG_KEY_n/GIT_CONFIG_VALUE_n sets any config key (a
    # core.fsmonitor command ran from uv's git fetch, measured with uv 0.13 and git 2.53), and
    # GIT_SSH_COMMAND, GIT_PROXY_COMMAND, GIT_EXEC_PATH, GIT_ASKPASS or GIT_SSL_CAINFO name a command to
    # run or a CA to trust. Dropped by prefix, so a variable a later git adds is gone too, with
    # SSH_ASKPASS: git runs it for a password when GIT_ASKPASS and core.askPass are unset (it ran on an
    # HTTP 401 from uv's git fetch, measured). The credential helper git runs on an authentication
    # challenge reads its own: Git Credential Manager every GCM_ variable (its docs/environment.md;
    # GCM_GPG_PATH names the gpg it runs, GCM_PLAINTEXT_STORE_PATH and GCM_DPAPI_STORE_PATH the
    # directory it reads credentials from, GCM_CREDENTIAL_STORE which store), dropped by prefix too,
    # and PASSWORD_STORE_DIR, the directory its gpg store reads (its docs/credstores.md). Names are read
    # from env, a line per variable; a line inside a multi-line value that only looks like one names
    # nothing set, and is skipped.
    _iv_env=$(env) || pwshpy_die "cannot list the environment to drop the caller's git variables" 70
    _iv_names=$(printf '%s\n' "$_iv_env" | sed -n -e 's/^\(GIT_[A-Za-z0-9_]*\)=.*/\1/p' \
        -e 's/^\(GCM_[A-Za-z0-9_]*\)=.*/\1/p')
    for _iv in $_iv_names SSH_ASKPASS PASSWORD_STORE_DIR; do
        if eval "[ -n \"\${$_iv+set}\" ]"; then
            _iv_text="as root uv runs git with none of the caller's git or credential-helper settings"
            printf '%s\n' "pwshpy: ignoring $_iv: $_iv_text" >&2
            unset "$_iv"
        fi
    done
}

# Prints the home directory of the account this run is (no trailing slash; "" for /), from the
# passwd database: as root $HOME, XDG_DATA_HOME and XDG_CACHE_HOME are whatever the caller or the
# service that started it set (sudo keeps HOME on some systems, and sudo -E or env_keep the rest).
# getent asks every name service (Linux), id -P is the BSD and macOS form, and /etc/passwd itself
# lists root everywhere; all three come from the fixed helper PATH, and an entry counts only when its
# uid field is the euid. The home is the second-to-last field in both the 7-field passwd and the
# 10-field master.passwd form id -P prints. No absolute answer fails the call: a guessed directory
# would be one the caller could create.
pwshpy_account_home() {
    _ah_uid=$(pwshpy_euid)
    for _ah_source in getent id passwd; do
        _ah_entry=
        case $_ah_source in
            getent) command -v getent >/dev/null 2>&1 && _ah_entry=$(getent passwd "$_ah_uid" 2>/dev/null) ;;
            id) _ah_entry=$(id -P 2>/dev/null) ;;
            passwd) [ -r /etc/passwd ] && _ah_entry=$(awk -F: -v u="$_ah_uid" '$3 == u { print; exit }' /etc/passwd) ;;
        esac || _ah_entry=
        _ah_rest=${_ah_entry#*:*:}
        [ "${_ah_rest%%:*}" = "$_ah_uid" ] || continue
        _ah_home=${_ah_entry%:*}
        _ah_home=${_ah_home##*:}
        case $_ah_home in
            /*)
                printf '%s' "${_ah_home%/}"
                return 0
                ;;
        esac
    done
    return 1
}

# Prints the data directory of the account this run is (uv keeps its managed Pythons in
# <it>/uv/python), below its passwd home (pwshpy_account_home).
pwshpy_account_data_home() {
    _adh_home=$(pwshpy_account_home) || return 1
    printf '%s' "$_adh_home/.local/share"
}

# Prints the cache directory of the account this run is (uv's default cache is <it>/uv), below its
# passwd home, for the reason the data directory is (pwshpy_account_home).
pwshpy_account_cache_home() {
    _ach_home=$(pwshpy_account_home) || return 1
    printf '%s' "$_ach_home/.cache"
}

# What a root run that refused a managed-Python path tells the caller to do about it.
PWSHPY_PYTHON_REMEDY="make the path named above root's alone (chown root, then chmod go-w on it, with -R for a"
PWSHPY_PYTHON_REMEDY="$PWSHPY_PYTHON_REMEDY directory), or remove uv's managed-Python directory and install the Python"
PWSHPY_PYTHON_REMEDY="$PWSHPY_PYTHON_REMEDY again as root (uv python install)"

# What pwshpy_python_tree_distrust's listings print when find itself failed: no path find prints
# (they all start with the directory given) and no ls line (they start with the mode) looks like it.
PWSHPY_WALK_FAILED="pwshpy-walk-failed"

# Prints why an entry below uv's managed-Python directory "$1" (which exists) may not be run or read
# as root, or nothing. `uv python find` runs every install it considers there, and each interpreter
# reads its own standard library and site-packages as it starts (a .pth there runs code), so a file or
# directory another account can modify anywhere below is as bad as the directory itself (measured with
# uv 0.13: a .pth in a writable install's site-packages ran as root before any later check). Each entry
# is judged like the directory holding uv: root owns it, neither its group nor anyone else may write
# it, and it carries no ACL; a symlink by its owner alone (its own mode means nothing, and its target
# inside the directory is judged where it lies). A regular file named .lock is the one exception to the
# write check: uv creates its lock file mode 0666 (measured with uv 0.13) and only ever locks it, and no
# interpreter imports or runs a file of that name. One find for the modes and owners, one batched ls
# for the ACLs (with ACL lines where ls -e prints them, macOS and BSD): about 0.8 s for 85,640 entries
# (15 installs) on a Linux host, a fraction of that for the one or two installs of a normal directory.
pwshpy_python_tree_distrust() {
    _pt_root=$1
    set --
    for _pt_uid in $PWSHPY_TRUSTED_UIDS; do
        set -- "$@" ! -user "$_pt_uid"
    done
    # The trailing slash makes find enter the directory when "$1" is a symlink to it. The whole output is
    # read (sed, not head): find killed by a closed pipe would print nothing on a host where SIGPIPE is
    # ignored, and its failure is the one answer that must not be missed. The two arms are grouped before
    # -print: find binds the implicit -a tighter than -o, so an ungrouped -print would end the second arm
    # only, and a symlink matching the first would be printed by nothing.
    _pt_hit=$({ find "$_pt_root/" \( \( -type l "$@" \) -o \( ! -type l \( "$@" -o \( \( -perm -0002 -o \
        -perm -0020 \) ! \( -type f -name .lock \) \) \) \) \) -print 2>/dev/null \
        || printf '%s\n' "$PWSHPY_WALK_FAILED"; } | sed -n 1p)
    if [ "$_pt_hit" = "$PWSHPY_WALK_FAILED" ]; then
        printf '%s' "the entries below $_pt_root cannot be listed"
        return 0
    fi
    if [ -n "$_pt_hit" ]; then
        if [ -L "$_pt_hit" ] && pwshpy_stat "$_pt_hit"; then
            printf '%s' "the symlink $_pt_hit is owned by uid $PWSHPY_ST_UID, not root"
            return 0
        fi
        _pt_why=$(pwshpy_leaf_distrust "$_pt_hit")
        printf '%s' "${_pt_why:-$_pt_hit can be modified by another account}"
        return 0
    fi
    # ls -e lists a file's ACL entries below it on macOS and BSD; GNU ls has no -e and marks an ACL with
    # '+' instead (busybox: -e is the full date, and it marks nothing, see pwshpy_has_acl).
    _pt_list=-ldn
    if ls -lde / >/dev/null 2>&1; then
        _pt_list=-lden
    fi
    _pt_acl=$({ find "$_pt_root/" ! -type l -exec ls "$_pt_list" {} + 2>/dev/null \
        || printf '%s\n' "$PWSHPY_WALK_FAILED"; } | awk -v failed="$PWSHPY_WALK_FAILED" '
        $0 == failed { lost = 1 }
        /^ *[0-9]+: / || substr($1, 11, 1) == "+" { acl = 1 }
        END { if (lost) print "lost"; else if (acl) print "acl" }')
    case $_pt_acl in
        lost) printf '%s' "the entries below $_pt_root cannot be listed" ;;
        acl)
            # Which one, for the message: only now, entry by entry.
            _pt_named=$(find "$_pt_root/" ! -type l -print 2>/dev/null | while IFS= read -r _pt_path; do
                if pwshpy_stat "$_pt_path" && pwshpy_has_acl "$_pt_path"; then
                    printf '%s' "$_pt_path carries an ACL"
                    break
                fi
            done)
            printf '%s' "${_pt_named:-an entry below $_pt_root carries an ACL}"
            ;;
    esac
    return 0
}

# Prints why uv's managed-Python directory "$1" may not be searched as root, or nothing. `uv python
# find` EXECUTES every interpreter it considers there to ask its version, so a .pth planted in one's
# site-packages would run as root before any check of the path uv answers (measured with uv 0.13):
# the directory is judged before uv starts, as a uv is, itself and every directory above it, and then
# everything below it (pwshpy_python_tree_distrust). One that does not exist yet is judged by the
# nearest one that does, where `uv python install` will create it; it holds no install yet.
pwshpy_python_dir_distrust() {
    _pd_dir=$1
    while [ ! -e "$_pd_dir" ] && [ ! -L "$_pd_dir" ]; do
        _pd_dir=${_pd_dir%/*}
        [ -n "$_pd_dir" ] || _pd_dir=/
    done
    if [ "$_pd_dir" = / ]; then
        _pd_why=$(pwshpy_leaf_distrust /)
    else
        _pd_why=$(pwshpy_uv_distrust "$_pd_dir")
    fi
    if [ -n "$_pd_why" ] || [ "$_pd_dir" != "$1" ]; then
        printf '%s' "$_pd_why"
        return 0
    fi
    pwshpy_python_tree_distrust "$1"
}

# Runs `uv python $1 ...` started in the anchor and reading no uv.toml (--no-config, see the run
# below), with the packed script's requires-python as the request when it has one, so the Python found
# or installed is one the script accepts.
pwshpy_uv_python() {
    _up_command=$1
    shift
    if [ -n "$PWSHPY_PYTHON_REQUEST" ]; then
        "$PWSHPY_UV" python "$_up_command" --no-config --directory "$PWSHPY_ANCHOR" "$@" "$PWSHPY_PYTHON_REQUEST"
    else
        "$PWSHPY_UV" python "$_up_command" --no-config --directory "$PWSHPY_ANCHOR" "$@"
    fi
}

# Prints why a root run must not unpack into, or run the script from, the cache directory "$1" (an
# absolute path), or nothing ([98]). The tree is verified against its hashes and executed later, so an
# account that can write the directory, or rename one above it, could swap a file between the check and
# uv reading it; and an unpack removes and rewrites the directory as root. Its root is whatever
# PWSHPY_PACK_CACHE, XDG_CACHE_HOME or HOME the caller handed over, so it is judged like uv's
# managed-Python directory: itself, every directory above it and every entry below it, or, before the
# first unpack, the nearest directory that exists.
pwshpy_cache_distrust() {
    pwshpy_python_dir_distrust "$1"
}

# Exits 127 when a root run may not use the cache directory "$PWSHPY_CACHE" (pwshpy_cache_distrust).
pwshpy_require_trusted_cache() {
    _rc_why=$(pwshpy_cache_distrust "$PWSHPY_CACHE")
    [ -n "$_rc_why" ] || return 0
    _rc_why="$_rc_why (as root the script is unpacked and run only from a directory no other account can"
    _rc_why="$_rc_why modify, nor anything below it): set PWSHPY_PACK_CACHE to a directory only root can"
    _rc_why="$_rc_why modify, or make the path named above root's alone (chown root, chmod go-w)"
    pwshpy_die "refusing the cache directory $PWSHPY_CACHE: $_rc_why" 127
}

# Sets PWSHPY_PYTHON to the uv-managed Python a root run hands `uv run --python`, or exits. uv run
# probes a .venv in its working directory and EVERY directory above it, and under only-managed even
# runs the script on that venv's python (measured with uv 0.13.0), so neither the caller's directory
# nor any ancestor of the anchor may decide; `uv python find --system` probes no .venv at all, and
# `--python <path>` stops uv run probing. None found: installed the way uv run would download one
# (not when UV_PYTHON_DOWNLOADS is never or manual, where uv run would not either), then found again.
# The path must pass the same check as uv itself: root owns it and every directory it passes through.
pwshpy_resolve_managed_python() {
    if PWSHPY_PYTHON=$(pwshpy_uv_python find --system --python-preference only-managed); then
        :
    else
        _rp_status=$?
        case ${UV_PYTHON_DOWNLOADS:-} in
            never | manual) exit "$_rp_status" ;;
        esac
        # --no-bin: uv run's own download links no pythonX.Y into ~/.local/bin, so neither does this.
        pwshpy_uv_python install --no-bin >&2 || exit $?
        PWSHPY_PYTHON=$(pwshpy_uv_python find --system --python-preference only-managed) || exit $?
    fi
    case $PWSHPY_PYTHON in
        /*) ;;
        *) pwshpy_die "uv named no absolute path for its Python: $PWSHPY_PYTHON" 70 ;;
    esac
    _rp_why=$(pwshpy_uv_distrust "$PWSHPY_PYTHON")
    if [ -n "$_rp_why" ]; then
        _rp_why="$_rp_why (as root only a Python no other account can modify is run): $PWSHPY_PYTHON_REMEDY"
        pwshpy_die "refusing the Python uv found at $PWSHPY_PYTHON: $_rp_why" 127
    fi
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
    if [ "$PWSHPY_GIT_SOURCE" = "1" ]; then
        printf 'git source : %s\n' "yes (a root run needs git 2.32 or later)"
    else
        printf 'git source : %s\n' "no"
    fi
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
    # Only the -v prompts ([136-sh]): the relaunch runs as sudo -n, which never asks, so a sudo that
    # keeps no credential after -v (timestamp_timeout=0) would prompt a second time without -n. Such a
    # setup is told by sudo -n -v failing, and declined like a refused prompt.
    if ! sudo -n -v; then
        _el_why="sudo keeps no credential after the password prompt, so it would ask again (timestamp_timeout=0)"
        printf '%s\n' "pwshpy: elevation declined ($_el_why)" >&2
        exit 77
    fi
    # A sudo that grants -v but then refuses the command itself (sudoers allowing only -v) exits with
    # that sudo's own code, which no wrapper can tell from the script's.
    # The pin goes in front of the script's arguments as two quoted words, so a path with a space
    # survives; the value-less switches carry no values, so splitting their unquoted list is
    # exactly what is wanted.
    if [ -n "$PWSHPY_UV_PIN" ]; then
        set -- --pwshpy-uv "$PWSHPY_UV_PIN" -- "$@"
    else
        set -- -- "$@"
    fi
    # shellcheck disable=SC2086
    exec sudo -n /bin/sh "$0" $PWSHPY_FWD "$@"
fi

# --- unpack -----------------------------------------------------------------------------

# As root the cache is judged before anything is removed or written in it, and the tree an unpack wrote
# is judged again ([98]). Absolute first: the walk refuses a relative path, and a relative
# PWSHPY_PACK_CACHE names the caller's directory.
if [ "$PWSHPY_AS_ROOT" = "1" ]; then
    case $PWSHPY_CACHE in
        /*) ;;
        *)
            pwshpy_caller_cwd
            PWSHPY_CACHE=${PWSHPY_CALLER_CWD%/}/$PWSHPY_CACHE
            ;;
    esac
    pwshpy_require_trusted_cache
fi
if [ "$PWSHPY_CLEAN" = "1" ]; then
    rm -rf "$PWSHPY_CACHE"
fi
if ! pwshpy_verify_tree; then
    pwshpy_unpack_locked
    if [ "$PWSHPY_AS_ROOT" = "1" ]; then
        pwshpy_require_trusted_cache
    fi
fi

# --- uv ---------------------------------------------------------------------------------

# What to say when no usable uv is left: as root, also how to provide one the trust rule accepts.
PWSHPY_UV_HINT="Install it from https://astral.sh/uv"
if [ "$PWSHPY_AS_ROOT" = "1" ]; then
    PWSHPY_UV_HINT="As root only a uv no other account can modify is run: install uv where only root can write"
    PWSHPY_UV_HINT="$PWSHPY_UV_HINT (root's own ~/.local/bin, the installer's default), or take group and other"
    PWSHPY_UV_HINT="$PWSHPY_UV_HINT write off the directory named above (chmod go-w); a --pwshpy-uv pin must pass"
    PWSHPY_UV_HINT="$PWSHPY_UV_HINT the same check"
fi

if [ -n "$PWSHPY_UV_PIN" ]; then
    # A pin is the whole answer: no search and no install behind it, so a refused pin fails here.
    if [ ! -f "$PWSHPY_UV_PIN" ] || [ ! -x "$PWSHPY_UV_PIN" ]; then
        pwshpy_die "no executable uv at the path given with --pwshpy-uv: $PWSHPY_UV_PIN" 127
    fi
    if [ "$PWSHPY_AS_ROOT" = "1" ]; then
        PWSHPY_PIN_WHY=$(pwshpy_uv_distrust "$PWSHPY_UV_PIN")
        if [ -n "$PWSHPY_PIN_WHY" ]; then
            printf '%s: %s. %s\n' "pwshpy: refusing the uv given with --pwshpy-uv: $PWSHPY_UV_PIN" \
                "$PWSHPY_PIN_WHY" "$PWSHPY_UV_HINT" >&2
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
    # planted in a PATH directory another account can write would run as root. As root the runner
    # names the interpreter itself (pwshpy_resolve_managed_python: a Python uv manages, user
    # decision); project discovery moves to the anchor, which only root can write, and uv itself
    # starts there (--directory), so nothing in the caller's directory is read. So uv runs the
    # generated shim, which changes back to the caller's directory (handed over in
    # @@PWSHPY_CWD_ENV@@) before the entry runs; every path the caller gave relative to that
    # directory (a --with path, a relative PWSHPY_PACK_CACHE) is made absolute first.
    # Asked while the helpers still come from the fixed PATH.
    PWSHPY_DATA_HOME=$(pwshpy_account_data_home) \
        || pwshpy_die "cannot read the home directory of uid $(pwshpy_euid) from the passwd database" 70
    PWSHPY_CACHE_HOME=$(pwshpy_account_cache_home) \
        || pwshpy_die "cannot read the home directory of uid $(pwshpy_euid) from the passwd database" 70
    pwshpy_caller_cwd
    PWSHPY_CALLER_PREFIX=${PWSHPY_CALLER_CWD%/}/
    case $PWSHPY_CACHE in
        /*) ;;
        *) PWSHPY_CACHE=$PWSHPY_CALLER_PREFIX$PWSHPY_CACHE ;;
    esac
    set -- "$PWSHPY_CACHE/$PWSHPY_SHIM" "$@"
    eval "set -- $PWSHPY_UV_ARGS_ANCHORED_RAW \"\$@\""
    pwshpy_drop_interpreter_variables
    # With every GIT_ variable gone git still reads its global files, $HOME/.gitconfig and
    # $XDG_CONFIG_HOME/git/config, from the HOME and XDG_CONFIG_HOME this run got (a sudo that keeps
    # HOME, or sudo -E), and a core.fsmonitor, core.sshCommand or credential.helper there runs a command
    # as root when uv fetches a git source. A set GIT_CONFIG_GLOBAL replaces both from git 2.32 on (an
    # older git ignores it, so for a script with a git source one is refused below); /dev/null is a file
    # only root can change. Set after the drop, which would remove it. The system file (/etc/gitconfig) is
    # root's and still applies.
    GIT_CONFIG_GLOBAL=/dev/null
    export GIT_CONFIG_GLOBAL
    printf '%s\n' "pwshpy: setting GIT_CONFIG_GLOBAL=/dev/null: as root git reads no caller's global config" >&2
    export "@@PWSHPY_CWD_ENV@@=$PWSHPY_CALLER_CWD"
    # The shim keeps its own arguments only when no out-of-band argument vector is set; one inherited
    # from the caller's environment must not replace them.
    unset @@PWSHPY_ARGV_ENV@@
    # Named outright after the caller's own choice was dropped: left to itself uv would look in the
    # data home XDG_DATA_HOME names, which is the caller's to set too.
    UV_PYTHON_INSTALL_DIR=$PWSHPY_DATA_HOME/uv/python
    export UV_PYTHON_INSTALL_DIR
    # The cache too, for the same reason: uv's default follows XDG_CACHE_HOME or HOME, which sudo -E or
    # env_keep can hand over from the caller, and cached script environments and wheels run as root.
    UV_CACHE_DIR=$PWSHPY_CACHE_HOME/uv
    export UV_CACHE_DIR
    printf '%s\n' "pwshpy: uv caches in $UV_CACHE_DIR: as root uv uses the running account's own cache directory" >&2
    # While the helpers still come from the fixed PATH: the checks of the directory and of the Python's
    # path run ls. The directory first: uv runs every Python it finds there.
    PWSHPY_PYTHON_DIR_WHY=$(pwshpy_python_dir_distrust "$UV_PYTHON_INSTALL_DIR")
    if [ -n "$PWSHPY_PYTHON_DIR_WHY" ]; then
        PWSHPY_PYTHON_DIR_WHY="$PWSHPY_PYTHON_DIR_WHY (as root uv searches only a directory no other account can"
        PWSHPY_PYTHON_DIR_WHY="$PWSHPY_PYTHON_DIR_WHY modify, nor anything below it): $PWSHPY_PYTHON_REMEDY"
        pwshpy_die "refusing uv's managed-Python directory $UV_PYTHON_INSTALL_DIR: $PWSHPY_PYTHON_DIR_WHY" 127
    fi
    # uv sees only the caller's PATH directories no other account can modify (computed while the
    # helpers still come from the fixed PATH); the packed script gets the caller's whole PATH back
    # from the shim, through @@PWSHPY_PATH_ENV@@ ([107]). The shim is handed uv's PATH too
    # (@@PWSHPY_UV_PATH_ENV@@): uv puts its own bin directory in front of it, and the shim keeps
    # what precedes it in front of the caller's PATH, as an unelevated run has it.
    PWSHPY_UV_PATH=$(pwshpy_trusted_path "$PWSHPY_CALLER_PATH")
    # A git dependency would fail with uv's bare "git not found" when the only gits are in directories
    # left off; name the cause.
    PWSHPY_UV_GIT=$(pwshpy_git_on "$PWSHPY_UV_PATH")
    PWSHPY_CALLER_GIT=$(pwshpy_git_on "$PWSHPY_CALLER_PATH")
    if [ -z "$PWSHPY_UV_GIT" ] && [ -n "$PWSHPY_CALLER_GIT" ]; then
        PWSHPY_GIT_WHY="$PWSHPY_CALLER_GIT and any other git on the caller's PATH are in directories another"
        PWSHPY_GIT_WHY="$PWSHPY_GIT_WHY account can modify; a git dependency fails as root until git is"
        printf '%s\n' "pwshpy: no git on uv's PATH: $PWSHPY_GIT_WHY installed in a directory only root can modify" >&2
    fi
    # The git uv will run, asked only for a script that needs one; with none, uv fails on its own.
    # Before uv is asked for a managed Python, which it may download: a run refused here calls no uv.
    if [ "$PWSHPY_GIT_SOURCE" = "1" ] && [ -n "$PWSHPY_UV_GIT" ]; then
        pwshpy_require_git_config_global "$PWSHPY_UV_GIT"
    fi
    pwshpy_resolve_managed_python
    export "@@PWSHPY_PATH_ENV@@=$PWSHPY_CALLER_PATH"
    export "@@PWSHPY_UV_PATH_ENV@@=$PWSHPY_UV_PATH"
    PATH=$PWSHPY_UV_PATH
    export PATH
    # --no-config: a uv.toml (system-wide under XDG_CONFIG_DIRS or /etc, or the user's own) can name a
    # cache directory, a package index or a Python download mirror, each a way to choose what runs as
    # root, and its location is the caller's to set; with the variables that name the same dropped
    # above, a root run takes uv's defaults plus the flags here (user decision). --no-env-file: no
    # .env file either, whatever UV_NO_ENV_FILE says.
    set -- --no-config --python-preference only-managed --no-env-file --python "$PWSHPY_PYTHON" \
        --project "$PWSHPY_ANCHOR" --directory "$PWSHPY_ANCHOR" "$@"
else
    set -- "$PWSHPY_CACHE/$PWSHPY_ENTRY" "$@"
    eval "set -- $PWSHPY_UV_ARGS_RAW \"\$@\""
fi
exec "$PWSHPY_UV" run --no-project "$@"
