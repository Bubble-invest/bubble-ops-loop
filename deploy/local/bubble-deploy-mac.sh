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
# - Never executes anything from the pulled tree EXCEPT one explicit,
#   hard-coded, allow-listed installer: scripts/install-boot-rearm.sh (board
#   #1488). Everything else this script does to $SUPPORT_DIR stays a plain
#   `install`/`cp` (copy, never execute):
#     - bubble-session-rotate-mac.sh — vendored copy, never run.
#     - deploy/hooks/rearm-loop-on-compact.py — compare-and-installed into
#       $SUPPORT_DIR/hooks/ ONLY when that path already exists there (i.e.
#       the Mac already opted in by having it vendored once); never created
#       from scratch, and a timestamped .bak is kept on every overwrite.
#   Board #1488: all 3 Macs had boot_rearm.ts stuck on June code (missed
#   #850) because this updater fast-forwarded the framework checkout but
#   never re-ran the installers that actually keep the live telegram-plugin
#   copy and the vendored compact hook in sync with it — so "the framework
#   is current" silently stopped meaning "the Mac's plugins are current".
#   Joris approved widening the "never execute from the pulled tree"
#   contract specifically for this one vetted, idempotent installer
#   (board #1488, Telegram: "ok, not manual") — it is intentionally NOT a
#   blanket allow to run arbitrary vendored scripts. install-boot-rearm.sh
#   itself never restarts anything (Rick controls restarts), and this
#   script only invokes it — it never restarts anything either.
#
# Usage: bubble-deploy-mac.sh [--repo-dir DIR] [--support-dir DIR]
# Exit codes: 0 = current or updated, installers clean; 1 = alert
# (dirty/wrong-branch/diverged/fetch or merge failure/unreadable state, OR a
# post-ff installer failure) — a successful fast-forward is NEVER rolled
# back because a later installer step failed; only the installer step itself
# is reported as failed via the non-zero exit.
set -uo pipefail

REPO_DIR="${BUBBLE_DEPLOY_MAC_REPO_DIR:-$HOME/claude-workspaces/bubble-ops-loop}"
SUPPORT_DIR="${BUBBLE_DEPLOY_MAC_SUPPORT_DIR:-$HOME/Library/Application Support/bubble-ops-loop}"
ROTATE_REL="deploy/local/bubble-session-rotate-mac.sh"
BOOT_REARM_INSTALLER_REL="scripts/install-boot-rearm.sh"
BOOT_REARM_SRC_REL="deploy/telegram-plugin/boot_rearm.ts"
COMPACT_HOOK_REL="deploy/hooks/rearm-loop-on-compact.py"

# Mac defaults for the allow-listed installer's own env knobs (board #1488).
# Overridable via env for tests/alternate boxes — these are passed straight
# through to install-boot-rearm.sh, which reads the exact same variable
# names, so an operator override (e.g. a launchd EnvironmentVariables entry)
# applies identically to this script's own pre-check and to the installer.
BOOT_REARM_PLUGIN_GLOB="${BOOT_REARM_PLUGIN_GLOB:-$HOME/.claude/plugins/cache/claude-plugins-official/telegram/*/}"
BOOT_REARM_BUN="${BOOT_REARM_BUN:-$HOME/.bun/bin/bun}"
[[ -x "$BOOT_REARM_BUN" ]] || BOOT_REARM_BUN="$(command -v bun 2>/dev/null || echo "$HOME/.bun/bin/bun")"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo-dir) REPO_DIR="${2:?--repo-dir needs a value}"; shift 2 ;;
        --repo-dir=*) REPO_DIR="${1#--repo-dir=}"; shift ;;
        --support-dir) SUPPORT_DIR="${2:?--support-dir needs a value}"; shift 2 ;;
        --support-dir=*) SUPPORT_DIR="${1#--support-dir=}"; shift ;;
        -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
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

resolve_plugin_dir() {
    # Mirrors scripts/install-boot-rearm.sh's own glob resolution exactly
    # (newest version wins via `sort -V`) so this pre-check and the
    # installer it may invoke always agree on which plugin copy is
    # "installed". Read-only: never mutates anything.
    local glob="$1" d dir=""
    for d in $(ls -d $glob 2>/dev/null | sort -V); do
        [[ -d "$d" ]] && dir="$d"
    done
    printf '%s\n' "${dir%/}"
}

boot_rearm_needed() {
    # Cheap pre-check (cmp + grep only — no bun) so the installer's `bun
    # build` validation runs only when there is real drift to fix. This
    # updater ticks every 15 minutes forever; re-validating an unchanged,
    # already-wired plugin on every tick would be pure overhead for no
    # benefit. Needed when: the vendored boot_rearm.ts source differs from
    # (or is missing from) the installed plugin copy, OR server.ts isn't
    # wired yet. If the plugin dir can't be resolved at all, defer to the
    # installer so its own structural error gets reported (and logged) the
    # normal way instead of being silently swallowed by this pre-check.
    local src="$REPO_DIR/$BOOT_REARM_SRC_REL" plugin_dir dst server_ts
    [[ -f "$src" ]] || return 1
    plugin_dir="$(resolve_plugin_dir "$BOOT_REARM_PLUGIN_GLOB")"
    if [[ -z "$plugin_dir" || ! -d "$plugin_dir" ]]; then
        log "BOOT-REARM: no plugin dir matched $BOOT_REARM_PLUGIN_GLOB — deferring to installer"
        return 0
    fi
    dst="$plugin_dir/boot_rearm.ts"
    server_ts="$plugin_dir/server.ts"
    if [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst"; then
        return 0
    fi
    if [[ -f "$server_ts" ]] && ! grep -q "bootRearmNotification" "$server_ts"; then
        return 0
    fi
    return 1
}

run_boot_rearm_if_needed() {
    # Board #1488: the ONE allow-listed executable from the pulled tree —
    # everything else in this script stays copy-only. Only called after the
    # post-ff (or already-current) verification above has already confirmed
    # a clean, exact `main` checkout at origin/main. Never restarts anything.
    if ! boot_rearm_needed; then
        log "BOOT-REARM: source unchanged and already wired — skip"
        return 0
    fi
    local installer="${BUBBLE_DEPLOY_MAC_BOOT_REARM_INSTALLER:-$REPO_DIR/$BOOT_REARM_INSTALLER_REL}"
    [[ -f "$installer" ]] || { alert "boot-rearm installer missing: $installer"; return 1; }
    log "BOOT-REARM: change detected — running $installer"
    local out
    out="$(mktemp "${TMPDIR:-/tmp}/bubble-deploy-mac-boot-rearm.XXXXXX")"
    if BOOT_REARM_PLUGIN_GLOB="$BOOT_REARM_PLUGIN_GLOB" BOOT_REARM_BUN="$BOOT_REARM_BUN" \
        bash "$installer" >"$out" 2>&1
    then
        log "BOOT-REARM: installer OK ($(tail -1 "$out" 2>/dev/null))"
        rm -f "$out"
        return 0
    else
        local rc=$?
        alert "boot-rearm installer failed (exit $rc) — $(tail -3 "$out" 2>/dev/null | tr '\n' ' ')"
        rm -f "$out"
        return 1
    fi
}

vendor_compact_hook_if_present() {
    # Board #1488: copy-only, and ONLY when the Mac already opted in — i.e.
    # a human already vendored this hook into $SUPPORT_DIR/hooks/ once.
    # Never creates it from scratch (not every Mac dept runs the compact
    # re-arm hook), and a timestamped .bak is kept on every overwrite.
    local src="$REPO_DIR/$COMPACT_HOOK_REL"
    local dst="$SUPPORT_DIR/hooks/rearm-loop-on-compact.py"
    [[ -f "$src" ]] || return 0
    if [[ ! -f "$dst" ]]; then
        log "COMPACT-HOOK: not vendored on this Mac (opted out) — skip"
        return 0
    fi
    if cmp -s "$src" "$dst"; then
        log "COMPACT-HOOK: unchanged — skip"
        return 0
    fi
    local ts bak
    ts="$(date -u +%Y%m%d-%H%M%S)"
    bak="$dst.bak-$ts"
    cp "$dst" "$bak" || { alert "could not back up compact hook to $bak"; return 1; }
    if ! install -m 0755 "$src" "$dst"; then
        alert "could not reinstall compact hook (previous version kept at $bak)"
        return 1
    fi
    log "COMPACT-HOOK: reinstalled $dst from $src (backup: $bak)"
    return 0
}

run_widened_contract_steps() {
    # Board #1488: called once state is verified safe (CURRENT or a
    # successful ff). Order matters only for logging clarity — both steps
    # are independent and each is copy-only or the one allow-listed
    # installer.
    local rc=0
    run_boot_rearm_if_needed || rc=1
    vendor_compact_hook_if_present || rc=1
    return "$rc"
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
    run_widened_contract_steps || exit 1
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

# The successful ff above is NEVER rolled back by anything that follows: a
# failure from here on is reported via a non-zero exit (so the launchd job's
# own status + stderr log surface it) but the git state stays exactly where
# the ff left it.
vendor_rotate_script_if_changed || exit 1
run_widened_contract_steps || exit 1
exit 0
