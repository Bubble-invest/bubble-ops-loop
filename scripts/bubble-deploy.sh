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
# Exit: 0 for updated/current/active-primary deferrals; 2 when operator review is
# required for preserved Git state; 1 for operational failures.
set -uo pipefail

SOURCE_INFRA_DIR="${BUBBLE_DEPLOY_SOURCE_INFRA_DIR:-/opt/bubble-ops-loop}"
CONSOLE_INFRA_DIR="${BUBBLE_DEPLOY_CONSOLE_INFRA_DIR:-/home/claude/bubble-ops-loop}"
AGENTS_ROOT="${BUBBLE_DEPLOY_AGENTS_ROOT:-/srv/agents}"
LEGACY_AGENTS_ROOT="${BUBBLE_DEPLOY_LEGACY_AGENTS_ROOT:-/home/claude/agents}"
UNIT_PREFIX="${BUBBLE_DEPLOY_UNIT_PREFIX:-bubble-agent@}"
LEGACY_UNIT_PREFIX="${BUBBLE_DEPLOY_LEGACY_UNIT_PREFIX:-ops-loop-}"
LOCK_FILE="${BUBBLE_DEPLOY_LOCK_FILE:-/run/lock/bubble-deploy.lock}"
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

mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
exec 9>"$LOCK_FILE" || { log "FAIL cannot open deploy lock"; exit 1; }
if ! flock -n 9; then
    log "FAIL another deploy process holds $LOCK_FILE"
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

inspect_repo_state() {
    local dir="$1" owner="$2" branch dirty ahead behind porcelain
    branch=$(g "$dir" "$owner" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    [[ "$branch" == "main" ]] || { printf 'non-main:%s\n' "${branch:-detached}"; return; }
    porcelain=$(g "$dir" "$owner" status --porcelain=v1 --untracked-files=normal 2>/dev/null) \
        || { printf 'invalid\n'; return; }
    if [[ -n "$porcelain" ]]; then
        dirty=$(printf '%s\n' "$porcelain" | awk 'END { print NR+0 }')
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
else
    # The source checkout feeds timers/floors. The console checkout is the
    # current interactive working directory. Updating its files on disk does
    # not claim that an already-running console has hot-reloaded them.
    sync_repo_safe_ff "framework-source" "$SOURCE_INFRA_DIR" ""
    sync_repo_safe_ff "framework-console-disk" "$CONSOLE_INFRA_DIR" ""
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
((FAILED == 0)) || exit 1
((DEFERRED_REVIEW == 0)) || exit 2
exit 0
