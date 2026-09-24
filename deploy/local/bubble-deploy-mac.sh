#!/usr/bin/env bash
# bubble-deploy-mac.sh — keep a Mac clone of the bubble-ops-loop FRAMEWORK
# checkout itself current (board #1477). Mac twin of scripts/bubble-deploy.sh
# --infra-only / bubble-deploy-infra.timer, simplified for a single,
# non-systemd Mac checkout: no repo owner switching, no systemd primary-unit
# defer logic (a Mac framework clone has no "primary agent unit" to yield to).
#
# Safety contract (mirrors scripts/bubble-deploy.sh):
# - Only a clean, exact `main`, zero-ahead checkout may fast-forward.
# - Dirty tree, non-main branch, or a diverged (ahead) checkout are left
#   untouched: this script logs an ALERT line and exits non-zero so the
#   launchd job's own exit status (and its stderr log) surface the problem —
#   it never stashes, resets, rebases, or force-pushes.
# - Never executes anything from the pulled tree. The one exception is a
#   plain `install` (copy, not execute) of the vendored session-rotate script
#   into Application Support when its source changed in the fast-forward —
#   the same copy install-session-rotate-mac.sh does today, just kept current
#   automatically instead of requiring a manual re-run after every merge.
#
# Usage: bubble-deploy-mac.sh [--repo-dir DIR] [--support-dir DIR]
# Exit codes: 0 = current or updated; 1 = alert (dirty/wrong-branch/diverged/
# fetch or merge failure/unreadable state) — nothing was touched on disk.
set -uo pipefail

REPO_DIR="${BUBBLE_DEPLOY_MAC_REPO_DIR:-$HOME/claude-workspaces/bubble-ops-loop}"
SUPPORT_DIR="${BUBBLE_DEPLOY_MAC_SUPPORT_DIR:-$HOME/Library/Application Support/bubble-ops-loop}"
ROTATE_REL="deploy/local/bubble-session-rotate-mac.sh"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo-dir) REPO_DIR="${2:?--repo-dir needs a value}"; shift 2 ;;
        --repo-dir=*) REPO_DIR="${1#--repo-dir=}"; shift ;;
        --support-dir) SUPPORT_DIR="${2:?--support-dir needs a value}"; shift 2 ;;
        --support-dir=*) SUPPORT_DIR="${1#--support-dir=}"; shift ;;
        -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "usage: $0 [--repo-dir DIR] [--support-dir DIR]" >&2; exit 2 ;;
    esac
done

TS() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log()   { echo "[$(TS)] [bubble-deploy-mac] $*"; }
alert() { log "ALERT: $*"; }

g() { git -C "$REPO_DIR" "$@"; }

repo_state() {
    # Prints one of: nogit | non-main:<branch> | invalid | dirty:<n> |
    # ahead:<n> | safe:<behind>. Never mutates anything.
    local branch porcelain dirty ahead behind
    branch=$(g symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    [[ "$branch" == "main" ]] || { printf 'non-main:%s\n' "${branch:-detached HEAD}"; return; }
    porcelain=$(g status --porcelain=v1 --untracked-files=normal 2>/dev/null) \
        || { printf 'invalid\n'; return; }
    if [[ -n "$porcelain" ]]; then
        dirty=$(printf '%s\n' "$porcelain" | awk 'END { print NR+0 }')
        printf 'dirty:%s\n' "$dirty"
        return
    fi
    ahead=$(g rev-list --count origin/main..HEAD 2>/dev/null || echo invalid)
    behind=$(g rev-list --count HEAD..origin/main 2>/dev/null || echo invalid)
    [[ "$ahead" =~ ^[0-9]+$ && "$behind" =~ ^[0-9]+$ ]] || { printf 'invalid\n'; return; }
    ((ahead == 0)) || { printf 'ahead:%s\n' "$ahead"; return; }
    printf 'safe:%s\n' "$behind"
}

vendor_rotate_script_if_changed() {
    local src="$REPO_DIR/$ROTATE_REL" dst
    [[ -f "$src" ]] || return 0
    dst="$SUPPORT_DIR/bubble-session-rotate-mac.sh"
    mkdir -p "$SUPPORT_DIR" || { alert "cannot create support dir $SUPPORT_DIR"; return 1; }
    if [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst"; then
        install -m 0755 "$src" "$dst" || { alert "could not reinstall vendored rotate script"; return 1; }
        log "VENDORED: reinstalled $dst from $src (copy only — never executed)"
    fi
    return 0
}

if [[ -L "$REPO_DIR" || ! -e "$REPO_DIR/.git" ]]; then
    alert "no direct Git checkout at $REPO_DIR"
    exit 1
fi

check="$(repo_state)"
case "$check" in
    non-main:*) alert "checkout is on branch '${check#non-main:}', expected main — not touching the worktree"; exit 1 ;;
    dirty:*)    alert "working tree is dirty (${check#dirty:} paths) — not touching the worktree"; exit 1 ;;
    ahead:*)    alert "local HEAD is ${check#ahead:} commit(s) ahead of origin/main (diverged) — not touching the worktree"; exit 1 ;;
    invalid)    alert "unreadable Git state at $REPO_DIR"; exit 1 ;;
esac

if ! g fetch origin main --quiet; then
    alert "git fetch origin main failed"
    exit 1
fi

# Re-check AFTER fetch: fetch never mutates the worktree, but the tree or
# branch could have changed underneath us (e.g. an operator editing live) in
# the window between the two checks. Recheck before the only mutation below.
check="$(repo_state)"
case "$check" in
    non-main:*) alert "branch changed to '${check#non-main:}' during fetch — not touching the worktree"; exit 1 ;;
    dirty:*)    alert "working tree became dirty during fetch (${check#dirty:} paths) — not touching the worktree"; exit 1 ;;
    ahead:*)    alert "local HEAD is now ${check#ahead:} commit(s) ahead of origin/main (diverged) — not touching the worktree"; exit 1 ;;
    invalid)    alert "unreadable Git state after fetch"; exit 1 ;;
    safe:*)     behind=${check#safe:} ;;
esac

if ((behind == 0)); then
    log "CURRENT: $REPO_DIR already at origin/main ($(g rev-parse --short HEAD 2>/dev/null))"
    vendor_rotate_script_if_changed || exit 1
    exit 0
fi

target="$(g rev-parse origin/main 2>/dev/null || true)"
if ! g merge --ff-only origin/main >/dev/null 2>&1; then
    alert "fast-forward refused ($behind commit(s) behind) — no rollback attempted"
    exit 1
fi
final="$(g rev-parse HEAD 2>/dev/null || true)"
check="$(repo_state)"
if [[ "$final" != "$target" || "$check" != "safe:0" ]]; then
    alert "post-fast-forward verification failed (HEAD=$final target=$target state=$check)"
    exit 1
fi
log "UPDATED: fast-forwarded $REPO_DIR by $behind commit(s) to $final"

vendor_rotate_script_if_changed || exit 1
exit 0
