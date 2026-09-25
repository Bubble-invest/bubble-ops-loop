#!/usr/bin/env bash
# Sync merged main branches into idle, clean VPS checkouts without rewriting work.
#
# Safety contract:
# - Git always runs as the repository directory owner. No safe.directory changes.
# - Only a clean, exact `main`, zero-ahead checkout may fast-forward.
# - Dirty, ahead, detached, or non-main checkouts are preserved and deferred.
# - Active/activating/deactivating primary agents are never changed on disk here;
#   their existing loop owns its self-pull.
# - This script never stashes, resets, rolls back, stops, starts, or restarts.
#
# Exit: 0 for updated/current/active-primary deferrals, and for preserved Git
# state that merely needs eventual operator review (deferred_review — local
# work was preserved exactly, nothing is broken); 1 for genuine operational
# failures. deferred_review is logged loudly (a DEFER_REVIEW line per checkout
# plus the deferred_review=N count in the DONE summary) but must never trip
# systemd's failure state / OnFailure= alarm — it is not an error. #1305.
set -uo pipefail
ORIGINAL_ARGS=("$@")

SOURCE_INFRA_DIR="${BUBBLE_DEPLOY_SOURCE_INFRA_DIR:-/opt/bubble-ops-loop}"
CONSOLE_INFRA_DIR="${BUBBLE_DEPLOY_CONSOLE_INFRA_DIR:-/home/claude/bubble-ops-loop}"
AGENTS_ROOT="${BUBBLE_DEPLOY_AGENTS_ROOT:-/srv/agents}"
LEGACY_AGENTS_ROOT="${BUBBLE_DEPLOY_LEGACY_AGENTS_ROOT:-/home/claude/agents}"
UNIT_PREFIX="${BUBBLE_DEPLOY_UNIT_PREFIX:-bubble-agent@}"
LEGACY_UNIT_PREFIX="${BUBBLE_DEPLOY_LEGACY_UNIT_PREFIX:-ops-loop-}"
LOCK_FILE="${BUBBLE_DEPLOY_LOCK_FILE:-/run/bubble-deploy.lock}"
# The unprivileged user the console service itself runs as (User=bubble-console
# in bubble-ops-console.service). The console-deps import smoke test runs as
# THIS user, never as root (this script's own User=root) — root-run import-time
# code (app/session/settings init) could otherwise leave root-owned files the
# console user can't later write. #1503 checker review.
CONSOLE_SERVICE_USER="${BUBBLE_CONSOLE_USER:-bubble-console}"
DRY_RUN=0
INFRA_ONLY=0
ONE_DEPT=""

while (($#)); do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --infra-only) INFRA_ONLY=1; shift ;;
        --dept)
            [[ $# -ge 2 && "$2" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
                echo "usage: $0 [--dry-run] [--infra-only] [--dept slug]" >&2
                exit 2
            }
            ONE_DEPT="$2"; shift 2 ;;
        *)
            echo "usage: $0 [--dry-run] [--infra-only] [--dept slug]" >&2
            exit 2 ;;
    esac
done

UPDATED=0
WOULD_UPDATE=0
CURRENT=0
DEFERRED_ACTIVE=0
DEFERRED_REVIEW=0
SKIPPED=0
FAILED=0

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [deploy] $*"; }

# Bash redirection follows symlinks and truncates before metadata checks. Use a
# tiny stdlib-only launcher to open without following links, verify the inode,
# lock its open-file description, then exec this same script with fd 9 held.
if [[ "${BUBBLE_DEPLOY_LOCK_REEXEC:-}" != "1" ]]; then
    exec python3 - "$LOCK_FILE" "$0" "${ORIGINAL_ARGS[@]}" <<'PY'
import fcntl
import os
import stat
import sys

path, script, *args = sys.argv[1:]
default_path = path == "/run/bubble-deploy.lock"
parent = os.path.dirname(path) or "."
try:
    parent_stat = os.stat(parent, follow_symlinks=False)
    if default_path and (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != 0
        or parent_stat.st_mode & 0o022
    ):
        raise RuntimeError("unsafe default lock parent")
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    file_stat = os.fstat(fd)
    expected_uid = 0 if default_path else os.geteuid()
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != expected_uid
        or file_stat.st_nlink != 1
        or file_stat.st_mode & 0o077
    ):
        raise RuntimeError("unsafe deploy lock inode")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if fd != 9:
        os.dup2(fd, 9)
        os.close(fd)
    os.set_inheritable(9, True)
    env = dict(os.environ)
    env["BUBBLE_DEPLOY_LOCK_REEXEC"] = "1"
    os.execve("/bin/bash", ["bash", script, *args], env)
except BlockingIOError:
    print("[deploy] FAIL another deploy process holds the lock", file=sys.stderr)
except Exception as exc:
    print(
        "[deploy] FAIL secure deploy lock unavailable (" + type(exc).__name__ + ")",
        file=sys.stderr,
    )
sys.exit(1)
PY
fi
unset BUBBLE_DEPLOY_LOCK_REEXEC
if ! python3 - "$LOCK_FILE" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
try:
    locked = os.fstat(9)
    named = os.stat(path, follow_symlinks=False)
    expected_uid = 0 if path == "/run/bubble-deploy.lock" else os.geteuid()
    valid = (
        stat.S_ISREG(locked.st_mode)
        and stat.S_ISREG(named.st_mode)
        and (locked.st_dev, locked.st_ino) == (named.st_dev, named.st_ino)
        and locked.st_uid == expected_uid
        and locked.st_nlink == 1
        and not locked.st_mode & 0o077
    )
except Exception:
    valid = False
sys.exit(0 if valid else 1)
PY
then
    log "FAIL inherited secure deploy lock metadata is invalid"
    exit 1
fi
if ! flock -n 9; then
    log "FAIL inherited secure deploy lock is unavailable"
    exit 1
fi

repo_owner() {
    local owner
    owner=$(stat -c '%U' -- "$1" 2>/dev/null) || return 1
    [[ "$owner" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || return 1
    printf '%s\n' "$owner"
}

g() {
    local dir="$1" owner="$2"
    shift 2
    if [[ "$EUID" == "0" ]]; then
        if [[ "$owner" == "root" ]]; then
            git -C "$dir" "$@"
        else
            runuser -u "$owner" -- git -C "$dir" "$@"
        fi
    else
        [[ "$(id -un)" == "$owner" ]] || return 126
        git -C "$dir" "$@"
    fi
}

git_fetch_retry() {
    local dir="$1" owner="$2" attempt=1
    while ! g "$dir" "$owner" fetch origin main --quiet; do
        if ((attempt >= 3)); then
            return 1
        fi
        log "retry fetch after attempt $attempt"
        sleep $((attempt * 2))
        attempt=$((attempt + 1))
    done
}

primary_state() {
    local unit="$1" load state
    [[ -n "$unit" ]] || { printf 'none\n'; return; }
    load=$(systemctl show "$unit" -p LoadState --value 2>/dev/null || true)
    [[ "$load" == "loaded" ]] || { printf 'none\n'; return; }
    state=$(systemctl is-active "$unit" 2>/dev/null || true)
    printf '%s\n' "${state:-unknown}"
}

unit_is_loaded() {
    [[ -n "$1" ]] || return 1
    [[ "$(systemctl show "$1" -p LoadState --value 2>/dev/null || true)" == "loaded" ]]
}

defer_review() {
    DEFERRED_REVIEW=$((DEFERRED_REVIEW + 1))
    log "DEFER_REVIEW $1: $2; preserved exactly"
}

# is_known_fleet_artifact $path — true for the handful of UNTRACKED files the
# fleet itself scatters into every dept checkout as a side effect of routine
# ops (not real work): a generated CLAUDE.md alias, vendoring's own backup
# files, and a runtime harness-switch note. #1187: these three patterns are
# the ONLY thing this predicate matches — anything else (including a tracked
# modification to one of these very files) still counts as genuine dirt below.
is_known_fleet_artifact() {
    local path="$1" base="${1##*/}"
    case "$base" in
        AGENTS.md|HARNESS_HANDOFF.md) return 0 ;;
        *.pre-vendor-*) return 0 ;;
    esac
    return 1
}

# sync_console_requirements $label $dir — board #1503: bubble-deploy.sh syncs
# the /opt/bubble-ops-loop code every 15 minutes but never installed
# console/requirements.txt into that checkout's venv, so a merged dependency
# bump (e.g. #492's `cryptography`) only surfaced as a crash on the next
# console restart, potentially hours/days later. This installs into the venv
# THAT SITS INSIDE $dir (the one the console's ExecStart actually runs:
# $dir/venv/bin/python -m uvicorn console.main:app) whenever
# console/requirements.txt's content changed since the last successful
# install, verifies the install with an import smoke test, and only then
# records the new hash — so a failed install/import is retried every run and
# keeps failing the deploy loudly instead of being silently forgotten.
#
# This function never stops, starts, or restarts anything (same contract as
# the rest of this script) — it only makes sure that whenever the console
# NEXT restarts (systemd Restart=on-failure, a manual restart, a reboot), the
# already-synced code on disk has matching deps already installed and
# import-verified.
sync_console_requirements() {
    local label="$1" dir="$2"
    local reqs="$dir/console/requirements.txt"
    local venv_python="$dir/venv/bin/python"
    local state_file="$dir/venv/.requirements.sha256"
    local want have

    [[ -f "$reqs" ]] || return 0
    if [[ "$DRY_RUN" == "1" ]]; then
        log "DRY_RUN $label-console-deps: would check $reqs against $state_file"
        return 0
    fi
    if [[ ! -x "$venv_python" ]]; then
        FAILED=$((FAILED + 1))
        log "FAIL $label-console-deps: venv python not found or not executable at $venv_python"
        return 1
    fi
    want=$(sha256sum -- "$reqs" 2>/dev/null | awk '{print $1}') || want=""
    if [[ -z "$want" ]]; then
        FAILED=$((FAILED + 1))
        log "FAIL $label-console-deps: cannot hash $reqs"
        return 1
    fi
    have=""
    [[ -f "$state_file" ]] && have=$(<"$state_file")
    if [[ "$want" == "$have" ]]; then
        log "CURRENT $label-console-deps: $reqs unchanged since last install"
        return 0
    fi

    log "INSTALL $label-console-deps: $reqs changed, installing into $dir/venv"
    # Board #1503 gotcha: $dir/venv/bin/pip was copied from a DIFFERENT venv
    # (a stale /home/claude/bubble-ops-loop/venv baked into its shebang when
    # bin/pip was copied rather than created in place — venv/ itself is a real
    # venv, pyvenv.cfg present). Always invoke pip as a module of THIS venv's
    # own interpreter, never the pip script directly.
    if ! "$venv_python" -m pip install --quiet -r "$reqs"; then
        FAILED=$((FAILED + 1))
        log "FAIL $label-console-deps: pip install -r $reqs failed; state file left unwritten so this is retried next run; console NOT restarted"
        return 1
    fi

    # Checker review (#1503): this script's own service runs as root, but
    # `import console.main` executes import-time code (create_app(), settings,
    # session-db connect/mkdir paths, hash computations) that can create files.
    # Root-owned files from a root-run import could later be unwritable by the
    # console's own unprivileged service user. So the smoke test itself runs
    # AS that user via runuser, with a scrubbed environment and a throwaway
    # cwd — never as root, and never falls back to root if runuser or the
    # user is missing (that's a loud FAIL instead).
    if ! command -v runuser >/dev/null 2>&1; then
        FAILED=$((FAILED + 1))
        log "FAIL $label-console-deps: runuser not found; refusing to smoke-test as root; state file left unwritten so this is retried next run; console NOT restarted"
        return 1
    fi
    if ! id -u "$CONSOLE_SERVICE_USER" >/dev/null 2>&1; then
        FAILED=$((FAILED + 1))
        log "FAIL $label-console-deps: console service user '$CONSOLE_SERVICE_USER' not found; refusing to smoke-test as root; state file left unwritten so this is retried next run; console NOT restarted"
        return 1
    fi
    local smoke_workdir
    smoke_workdir=$(mktemp -d) || {
        FAILED=$((FAILED + 1))
        log "FAIL $label-console-deps: cannot create smoke-test working dir; state file left unwritten so this is retried next run; console NOT restarted"
        return 1
    }
    if ! runuser -u "$CONSOLE_SERVICE_USER" -- \
        env -i PATH=/usr/bin:/bin HOME=/nonexistent PYTHONPATH="$dir" \
        bash -c 'cd -- "$1" && exec "$2" -c "import console.main"' -- \
        "$smoke_workdir" "$venv_python" >/dev/null 2>&1
    then
        FAILED=$((FAILED + 1))
        log "FAIL $label-console-deps: post-install smoke test failed (import console.main as $CONSOLE_SERVICE_USER); state file left unwritten so this is retried next run; console NOT restarted"
        rm -rf -- "$smoke_workdir"
        return 1
    fi
    rm -rf -- "$smoke_workdir"
    if ! printf '%s\n' "$want" >"$state_file.tmp" || ! mv -f "$state_file.tmp" "$state_file"; then
        FAILED=$((FAILED + 1))
        log "FAIL $label-console-deps: install+smoke test succeeded but could not persist $state_file; will reinstall next run"
        return 1
    fi
    log "UPDATED $label-console-deps: installed + import-smoke-tested ($want)"
}

inspect_repo_state() {
    local dir="$1" owner="$2" branch dirty ahead behind porcelain filtered
    branch=$(g "$dir" "$owner" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    [[ "$branch" == "main" ]] || { printf 'non-main:%s\n' "${branch:-detached}"; return; }
    porcelain=$(g "$dir" "$owner" status --porcelain=v1 --untracked-files=normal 2>/dev/null) \
        || { printf 'invalid\n'; return; }
    if [[ -n "$porcelain" ]]; then
        # #1187: known, harmless, UNTRACKED fleet artifacts (AGENTS.md,
        # *.pre-vendor-*, HARNESS_HANDOFF.md) never count as dirt — every
        # other porcelain line (any tracked change, or any other untracked
        # file) still does, so genuine unexpected dirt still defers exactly
        # as before.
        filtered=$(printf '%s\n' "$porcelain" | while IFS= read -r line; do
            [[ -n "$line" ]] || continue
            if [[ "${line:0:2}" == "??" ]] && is_known_fleet_artifact "${line:3}"; then
                continue
            fi
            printf '%s\n' "$line"
        done)
        if [[ -n "$filtered" ]]; then
            dirty=$(printf '%s\n' "$filtered" | awk 'END { print NR+0 }')
        else
            dirty=0
        fi
    else
        dirty=0
    fi
    [[ "$dirty" == "0" ]] || { printf 'dirty:%s\n' "$dirty"; return; }
    ahead=$(g "$dir" "$owner" rev-list --count origin/main..HEAD 2>/dev/null || echo invalid)
    behind=$(g "$dir" "$owner" rev-list --count HEAD..origin/main 2>/dev/null || echo invalid)
    [[ "$ahead" =~ ^[0-9]+$ && "$behind" =~ ^[0-9]+$ ]] || { printf 'invalid\n'; return; }
    ((ahead == 0)) || { printf 'ahead:%s\n' "$ahead"; return; }
    printf 'safe:%s\n' "$behind"
}

sync_repo_safe_ff() {
    local label="$1" dir="$2" unit="${3:-}" owner root state behind check final_head target
    if [[ -L "$dir" || ! -e "$dir/.git" ]]; then
        SKIPPED=$((SKIPPED + 1))
        log "SKIP $label: no direct Git checkout at $dir"
        return 0
    fi
    owner=$(repo_owner "$dir") || {
        FAILED=$((FAILED + 1)); log "FAIL $label: cannot resolve repository owner"; return 1;
    }
    root=$(g "$dir" "$owner" rev-parse --show-toplevel 2>/dev/null || true)
    if [[ "$(readlink -f -- "$root" 2>/dev/null)" != "$(readlink -f -- "$dir" 2>/dev/null)" ]]; then
        FAILED=$((FAILED + 1)); log "FAIL $label: checkout root mismatch"; return 1
    fi

    # Preserve local state before even changing remote-tracking references.
    check=$(inspect_repo_state "$dir" "$owner")
    case "$check" in
        non-main:*) defer_review "$label" "branch ${check#non-main:}"; return 0 ;;
        dirty:*) defer_review "$label" "${check#dirty:} dirty paths"; return 0 ;;
        ahead:*) defer_review "$label" "${check#ahead:} commits ahead"; return 0 ;;
        invalid) FAILED=$((FAILED + 1)); log "FAIL $label: unreadable Git state"; return 1 ;;
    esac

    # Do not even update remote-tracking refs under a running primary. Its
    # existing self-pull path owns both fetch and worktree advancement.
    state=$(primary_state "$unit")
    case "$state" in
        active|activating|reloading|deactivating)
            DEFERRED_ACTIVE=$((DEFERRED_ACTIVE + 1))
            log "DEFER_ACTIVE $label: primary $unit is $state; self-pull retains fetch and update ownership"
            return 0 ;;
        inactive|failed|none) ;;
        *)
            FAILED=$((FAILED + 1))
            log "FAIL $label: cannot prove primary $unit is inactive (state=$state)"
            return 1 ;;
    esac

    if ! git_fetch_retry "$dir" "$owner"; then
        FAILED=$((FAILED + 1)); log "FAIL $label: fetch origin/main failed after 3 attempts"; return 1
    fi
    check=$(inspect_repo_state "$dir" "$owner")
    case "$check" in
        non-main:*) defer_review "$label" "branch changed to ${check#non-main:}"; return 0 ;;
        dirty:*) defer_review "$label" "${check#dirty:} dirty paths appeared"; return 0 ;;
        ahead:*) defer_review "$label" "${check#ahead:} commits ahead"; return 0 ;;
        invalid) FAILED=$((FAILED + 1)); log "FAIL $label: unreadable Git state after fetch"; return 1 ;;
        safe:*) behind=${check#safe:} ;;
    esac
    if ((behind == 0)); then
        CURRENT=$((CURRENT + 1)); log "CURRENT $label"; return 0
    fi

    state=$(primary_state "$unit")
    case "$state" in
        active|activating|reloading|deactivating)
            DEFERRED_ACTIVE=$((DEFERRED_ACTIVE + 1))
            log "DEFER_ACTIVE $label: primary $unit is $state; self-pull retains ownership ($behind behind)"
            return 0 ;;
        inactive|failed|none) ;;
        *)
            FAILED=$((FAILED + 1))
            log "FAIL $label: cannot prove primary $unit is inactive (state=$state)"
            return 1 ;;
    esac
    if [[ "$DRY_RUN" == "1" ]]; then
        WOULD_UPDATE=$((WOULD_UPDATE + 1))
        log "DRY_RUN $label: would fast-forward $behind commits as $owner; primary state=$state"
        return 0
    fi

    # Recheck immediately before the only worktree mutation. merge --ff-only
    # provides the final ancestry guard; there is deliberately no rollback.
    check=$(inspect_repo_state "$dir" "$owner")
    [[ "$check" == "safe:$behind" ]] || {
        defer_review "$label" "Git state changed before fast-forward"; return 0;
    }
    if [[ -n "$unit" ]]; then
        state=$(primary_state "$unit")
        case "$state" in
            active|activating|reloading|deactivating)
                DEFERRED_ACTIVE=$((DEFERRED_ACTIVE + 1))
                log "DEFER_ACTIVE $label: primary became $state before fast-forward"
                return 0 ;;
            inactive|failed) ;;
            *)
                FAILED=$((FAILED + 1))
                log "FAIL $label: primary state became unprovable before fast-forward ($state)"
                return 1 ;;
        esac
    fi
    target=$(g "$dir" "$owner" rev-parse origin/main 2>/dev/null || true)
    if ! g "$dir" "$owner" merge --ff-only origin/main >/dev/null 2>&1; then
        FAILED=$((FAILED + 1)); log "FAIL $label: fast-forward refused; no rollback attempted"; return 1
    fi
    final_head=$(g "$dir" "$owner" rev-parse HEAD 2>/dev/null || true)
    check=$(inspect_repo_state "$dir" "$owner")
    if [[ "$final_head" != "$target" || "$check" != "safe:0" ]]; then
        FAILED=$((FAILED + 1)); log "FAIL $label: post-fast-forward verification failed"; return 1
    fi
    UPDATED=$((UPDATED + 1))
    log "UPDATED $label: fast-forwarded $behind commits as $owner; primary state=$state"
}

resolve_one_dept() {
    local slug="$1" dir unit
    if [[ -e "$AGENTS_ROOT/$slug/.git" ]]; then
        dir="$AGENTS_ROOT/$slug"; unit="${UNIT_PREFIX}${slug}.service"
    elif [[ -e "$AGENTS_ROOT/bubble-ops-$slug/.git" ]]; then
        dir="$AGENTS_ROOT/bubble-ops-$slug"; unit="${UNIT_PREFIX}${slug}.service"
    elif [[ -e "$LEGACY_AGENTS_ROOT/bubble-ops-$slug/.git" ]]; then
        dir="$LEGACY_AGENTS_ROOT/bubble-ops-$slug"; unit="${LEGACY_UNIT_PREFIX}${slug}.service"
    else
        SKIPPED=$((SKIPPED + 1)); log "SKIP $slug: no canonical or legacy checkout"
        return
    fi
    if ! unit_is_loaded "$unit"; then
        SKIPPED=$((SKIPPED + 1)); log "SKIP $slug: no loaded primary unit $unit"
        return
    fi
    sync_repo_safe_ff "$slug" "$dir" "$unit"
}

log "START dry_run=$DRY_RUN infra_only=$INFRA_ONLY dept=${ONE_DEPT:-all}"
if [[ -n "${BUBBLE_DEPLOY_INFRA_DIR+x}" ]]; then
    # Explicit override retains the historical one-checkout contract.
    sync_repo_safe_ff "framework" "$BUBBLE_DEPLOY_INFRA_DIR" ""
    sync_console_requirements "framework" "$BUBBLE_DEPLOY_INFRA_DIR"
else
    # The source checkout feeds timers/floors. The console checkout is the
    # current interactive working directory. Updating its files on disk does
    # not claim that an already-running console has hot-reloaded them.
    sync_repo_safe_ff "framework-source" "$SOURCE_INFRA_DIR" ""
    sync_repo_safe_ff "framework-console-disk" "$CONSOLE_INFRA_DIR" ""
    # #1503: the console's own venv lives inside $SOURCE_INFRA_DIR (its
    # ExecStart is $SOURCE_INFRA_DIR/venv/bin/python -m uvicorn
    # console.main:app) — NOT inside $CONSOLE_INFRA_DIR, which is only a
    # claude-writable checkout for git operations. Only the source checkout's
    # venv needs its deps kept in sync.
    sync_console_requirements "framework-source" "$SOURCE_INFRA_DIR"
fi

if [[ "$INFRA_ONLY" != "1" ]]; then
    if [[ -n "$ONE_DEPT" ]]; then
        resolve_one_dept "$ONE_DEPT"
    else
        declare -A seen=()
        for dir in "$AGENTS_ROOT"/*; do
            [[ -d "$dir" && ! -L "$dir" && -e "$dir/.git" ]] || continue
            slug=$(basename "$dir")
            slug=${slug#bubble-ops-}
            [[ "$slug" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || continue
            [[ -z "${seen[$slug]:-}" ]] || continue
            seen[$slug]=1
            resolve_one_dept "$slug"
        done
        for dir in "$LEGACY_AGENTS_ROOT"/bubble-ops-*; do
            [[ -d "$dir" && ! -L "$dir" && -e "$dir/.git" ]] || continue
            slug=$(basename "$dir"); slug=${slug#bubble-ops-}
            [[ "$slug" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || continue
            [[ -z "${seen[$slug]:-}" ]] || continue
            seen[$slug]=1
            resolve_one_dept "$slug"
        done
    fi
fi

log "DONE updated=$UPDATED would_update=$WOULD_UPDATE current=$CURRENT deferred_active=$DEFERRED_ACTIVE deferred_review=$DEFERRED_REVIEW skipped=$SKIPPED failed=$FAILED"
# #1305: a genuine operational FAILED is the only outcome that should trip
# systemd's failure state (and the OnFailure= Telegram alarm chain). Fetch
# failures, unreadable Git state, refused fast-forwards etc. all still land
# here and still exit non-zero. This check stays FIRST and wins over any
# deferred_review count below, so a real failure can never be masked by an
# unrelated preserved checkout in the same run.
((FAILED == 0)) || exit 1
# deferred_review means local work was preserved exactly as designed — it is
# not a failure, so it must not alarm on every timer fire. It is already
# logged loudly per-checkout (DEFER_REVIEW ...) and summarized above
# (deferred_review=N); that is enough for a human to notice on their own
# schedule without a repeating siren. Emit one more explicit NOTE so the
# summary itself flags "this run needs eventual review" without failing it.
((DEFERRED_REVIEW == 0)) || log "NOTE deferred_review=$DEFERRED_REVIEW: local work preserved exactly, needs eventual human review; exiting 0 (not a failure, no alarm)"
exit 0
