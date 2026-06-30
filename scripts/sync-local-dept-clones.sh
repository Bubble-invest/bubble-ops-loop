#!/usr/bin/env bash
# sync-local-dept-clones.sh — keep the VPS's READ-ONLY clones of host:local depts
# fresh from GitHub, so the disk-mode cockpit renders their latest gates / state /
# heartbeat.
#
# Why (Hybrid local/VPS agent, {{OPERATOR}} msg 4258, 2026-06-11): a dept may run its
# /loop on its OWN machine (e.g. Miranda's bubble-ops-content on {{OPERATOR_2}}'s Mac —
# real Chrome/tools) by declaring `host: local` in onboarding/STATE.yaml. That
# dept NEVER executes on the VPS (see loop-backup.sh host-skip), but the cockpit
# is disk-mode and renders from disk — so the VPS keeps a READ-ONLY MIRROR of the
# dept's repo and pulls it on a cadence. The dept pushes its outputs/gates/state
# to GitHub from its own machine; this script `git pull --ff-only`s them down so
# the operator sees the current picture. Pure mirror: NO commit, NO push, NO
# execution here.
#
# Behaviour:
#   - Glob $AGENTS_ROOT/bubble-ops-* and, for each whose STATE.yaml says
#     host: local, run `git -C <dir> pull --ff-only`.
#   - host: vps / host-absent / malformed STATE → SKIP (not a local mirror;
#     fail-safe to "not local", never pull a vps dept by accident).
#   - FAIL-SAFE: a pull conflict / error on one dept LOGS + SKIPS it and the
#     loop CONTINUES to the next dept — one bad mirror must never wedge the
#     others. The script still exits 0 on a transient pull miss so the systemd
#     timer doesn't flap; a genuinely broken mirror surfaces in the journal.
#   - Idempotent: a clean mirror re-pulls to "Already up to date." (no-op).
#
# Usage: sync-local-dept-clones.sh [--agents-root <dir>]
#   --agents-root  base dir holding bubble-ops-<slug> clones (default
#                  /home/claude/agents). Parameterized so the harness can run
#                  hermetically inside a tmpdir.
#
# Deploy: paired with deploy/templates/sync-local-dept-clones.{service,timer}
# (every ~15 min on the VPS, which has systemd).
set -uo pipefail

AGENTS_ROOT="${BUBBLE_SYNC_AGENTS_ROOT:-/home/claude/agents}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --agents-root) AGENTS_ROOT="${2:?--agents-root needs a value}"; shift 2 ;;
        --agents-root=*) AGENTS_ROOT="${1#--agents-root=}"; shift ;;
        *) echo "ERR: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

TS()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(TS)] [sync-local-dept-clones] $*"; }

# dept_host <dir>: echo the top-level host: from <dir>/onboarding/STATE.yaml,
# or "vps" if the field/file is absent, unreadable or not exactly "local"
# (fail-safe: never treat a vps dept as a local mirror by accident).
dept_host() {
    local dir="$1"
    local state="${dir}/onboarding/STATE.yaml"
    [[ -f "$state" ]] || { echo "vps"; return 0; }
    local val
    val="$(grep -E '^host:[[:space:]]*' "$state" 2>/dev/null | head -n1 \
            | sed -E 's/^host:[[:space:]]*//; s/[[:space:]]*(#.*)?$//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/')"
    [[ "$val" == "local" ]] && echo "local" || echo "vps"
}

log "START agents_root=${AGENTS_ROOT}"

LOCAL_COUNT=0
FAIL_COUNT=0
for dir in "${AGENTS_ROOT}"/bubble-ops-*; do
    [[ -d "$dir" ]] || continue          # no match → glob stays literal; -d guards it
    slug="$(basename "$dir")"; slug="${slug#bubble-ops-}"

    if [[ "$(dept_host "$dir")" != "local" ]]; then
        # vps / host-absent / malformed → not a local mirror; the VPS owns its
        # state directly. Nothing to pull.
        continue
    fi

    LOCAL_COUNT=$((LOCAL_COUNT + 1))
    if [[ ! -d "$dir/.git" ]]; then
        # The read-only clone hasn't been created yet (an activation-time step);
        # log + skip rather than error — never wedge the run on a missing mirror.
        log "skip ${slug}: no git clone at ${dir} yet (mirror not created — activation step)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        continue
    fi

    # Clean tracked working-tree dirt before the ff-pull. This is a declared
    # READ-ONLY mirror (the dept's authoritative copy lives on its own machine +
    # GitHub), but the dept's runtime loop writes INTO the mirror — creating /
    # deleting queues/gates/*.yaml + inbox/decisions/*.yaml. Those TRACKED
    # modifications/deletions make `git pull --ff-only` abort ("local changes
    # would be overwritten"), freezing the mirror behind origin/main. Restore the
    # tree to HEAD with `git checkout -- .` so the ff-pull can proceed.
    #
    # CRITICAL: `git checkout -- .` restores only TRACKED files (deletions +
    # modifications back to HEAD). It does NOT touch UNTRACKED files — any
    # un-pushed dept output (?? inbox/decisions/...) is preserved, never deleted.
    # We deliberately do NOT `git reset --hard`: that would also blow away local
    # COMMITS, and committed divergence must still be caught by --ff-only below
    # (logged + skipped), not silently discarded.
    #
    # porcelain lines NOT starting with "??" are tracked dirt (modified/deleted/
    # staged); untracked files ("?? ...") are intentionally ignored here.
    tracked_dirty="$(git -C "$dir" status --porcelain 2>/dev/null | grep -cv '^??' || true)"
    if [[ "${tracked_dirty:-0}" -gt 0 ]]; then
        git -C "$dir" checkout -- . 2>/dev/null || true
        log "${slug}: discarded ${tracked_dirty} local tracked change(s) in read-only mirror before pull (untracked output preserved)"
    fi

    # Second blocker (the one `checkout -- .` does NOT fix): an UNTRACKED file in
    # the mirror sitting at a path that an INCOMING commit will ADD. The dept loop
    # writes e.g. inbox/decisions/publish-*.yaml as untracked; the SAME file later
    # gets committed upstream from the dept's own machine. On pull git aborts with
    # "untracked working tree files would be overwritten by merge", and the mirror
    # freezes — even after the tracked-dirt cleanup above.
    #
    # Resolution for a READ-ONLY mirror: origin is authoritative, so remove ONLY
    # the untracked files that COLLIDE with an incoming tracked path. We fetch
    # first (no merge), diff HEAD..@{u} to learn exactly which paths the pull will
    # write, and delete a local file at one of those paths ONLY IF it is untracked.
    # SCOPED removal — never a blanket `git clean -df`, which would also delete
    # non-colliding un-pushed dept output (queues/gates/.held/, other inbox/*).
    if git -C "$dir" fetch --quiet origin 2>/dev/null; then
        upstream="$(git -C "$dir" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
        if [[ -n "$upstream" ]]; then
            collisions=0
            while IFS= read -r path; do
                [[ -z "$path" ]] && continue
                # exists locally, but is NOT tracked (untracked) → it would block the pull
                if [[ -e "${dir}/${path}" ]] && ! git -C "$dir" ls-files --error-unmatch -- "$path" >/dev/null 2>&1; then
                    rm -f "${dir}/${path}" 2>/dev/null && collisions=$((collisions + 1))
                fi
            done < <(git -C "$dir" diff --name-only "HEAD..${upstream}" 2>/dev/null)
            if [[ "$collisions" -gt 0 ]]; then
                log "${slug}: removed ${collisions} untracked file(s) colliding with incoming tracked paths (read-only mirror; origin authoritative; non-colliding untracked output preserved)"
            fi
        fi
    fi

    # Read-only fast-forward mirror. --ff-only guarantees we NEVER create a merge
    # commit or diverge: if the local mirror somehow has its own commits the pull
    # aborts (logged + skipped) instead of silently merging.
    if git -C "$dir" pull --ff-only >/tmp/.sync-local-$$ 2>&1; then
        log "pulled ${slug}: $(tail -n1 /tmp/.sync-local-$$ 2>/dev/null)"
    else
        # Fail-safe: log the failure + the git message, skip this dept, KEEP
        # going. A pull conflict on one mirror must never block the others.
        log "WARN ${slug}: git pull --ff-only failed — skipping (mirror left as-is): $(tail -n1 /tmp/.sync-local-$$ 2>/dev/null)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
    rm -f /tmp/.sync-local-$$
done

log "DONE local_depts=${LOCAL_COUNT} failures=${FAIL_COUNT}"
# Always exit 0: a transient pull miss must not flap the systemd timer. Genuine
# breakage is visible in the journal (the WARN lines + the failures= count).
exit 0
