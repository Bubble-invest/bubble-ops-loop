#!/usr/bin/env bash
# =============================================================================
# bubble-deploy.sh — sync merged GitHub main into the live VPS.
#
# THE GAP IT CLOSES: merging a PR on GitHub does NOT update the box. The
# layer-floor tick runs /home/claude/bubble-ops-loop/scripts/*, and each dept
# runs from /home/claude/agents/bubble-ops-<slug>/ — all git clones that only
# advance when pulled. This makes "deploy" one explicit, idempotent command.
#
# WHAT IT DOES (idempotent, safe to re-run):
#   1. bubble-ops-loop (shared infra: loop-backup.sh, dispatch_directives.py,
#      skills, templates) → fetch + reset --hard origin/main.
#   2. Each dept clone under agents/bubble-ops-* → fetch; ff to origin/main.
#      Dept loops also self-pull at tick start, but a live loop RACES a ff on the
#      working tree, so we STOP the loop, ff, restart (loop re-arms on start).
#      Skipped cleanly if a dept is ahead (unpushed work) — never clobbers.
#
# Usage:
#   bubble-deploy.sh                 # deploy everything
#   bubble-deploy.sh --infra-only    # just bubble-ops-loop (no dept restarts)
#   bubble-deploy.sh --dept <slug>   # one dept + infra
#   bubble-deploy.sh --dry-run       # report what WOULD sync, change nothing
# =============================================================================
set -uo pipefail

INFRA_DIR=/home/claude/bubble-ops-loop
AGENTS_ROOT=/home/claude/agents
DRY_RUN=0; INFRA_ONLY=0; ONE_DEPT=""
for a in "$@"; do case "$a" in
  --dry-run) DRY_RUN=1 ;;
  --infra-only) INFRA_ONLY=1 ;;
  --dept) ONE_DEPT="__next__" ;;
  *) [[ "$ONE_DEPT" == "__next__" ]] && ONE_DEPT="$a" ;;
esac; done

log(){ echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [deploy] $*"; }
g(){ sudo -u claude git -C "$1" "${@:2}"; }

sync_repo_reset(){ # $1=dir  — hard-reset to origin/main (infra: no local work expected)
  local d="$1"
  g "$d" config --global --get-all safe.directory 2>/dev/null | grep -qx "$d" \
    || g "$d" config --global --add safe.directory "$d" 2>/dev/null || true
  g "$d" fetch origin main --quiet || { log "FAIL fetch $d"; return 1; }
  local behind; behind=$(g "$d" rev-list --count HEAD..origin/main 2>/dev/null || echo "?")
  local ahead;  ahead=$(g "$d" rev-list --count origin/main..HEAD 2>/dev/null || echo "?")
  if [[ "$ahead" != "0" ]]; then log "WARN $d is $ahead AHEAD — has local commits; reset would lose them. SKIPPING."; return 2; fi
  if [[ "$behind" == "0" ]]; then log "$d already current"; return 0; fi
  if [[ "$DRY_RUN" == "1" ]]; then log "[dry-run] would reset $d ($behind behind)"; return 0; fi
  g "$d" reset --hard origin/main >/dev/null && log "$d reset to origin/main ($behind applied)"
}

sync_dept_ff(){ # $1=slug — stop loop, ff, restart (avoids the live-loop race)
  local slug="$1" d="$AGENTS_ROOT/bubble-ops-$slug" unit="ops-loop-$slug.service"
  [[ -d "$d/.git" ]] || { log "skip $slug (no clone)"; return 0; }
  g "$d" config --global --get-all safe.directory 2>/dev/null | grep -qx "$d" \
    || g "$d" config --global --add safe.directory "$d" 2>/dev/null || true
  g "$d" fetch origin main --quiet || { log "FAIL fetch $slug"; return 1; }
  local behind ahead; behind=$(g "$d" rev-list --count HEAD..origin/main 2>/dev/null||echo "?")
  ahead=$(g "$d" rev-list --count origin/main..HEAD 2>/dev/null||echo "?")
  if [[ "$ahead" != "0" ]]; then log "$slug: $ahead ahead (unpushed) — loop will pull itself; not forcing"; return 0; fi
  if [[ "$behind" == "0" ]]; then log "$slug already current"; return 0; fi
  if [[ "$DRY_RUN" == "1" ]]; then log "[dry-run] would stop/ff/restart $slug ($behind behind)"; return 0; fi
  local was_active; was_active=$(systemctl is-active "$unit" 2>/dev/null||echo inactive)
  [[ "$was_active" == "active" ]] && systemctl stop "$unit"
  # discard working-tree drift that's identical-or-runtime (ff refuses on any dirty overlap)
  g "$d" checkout -- . 2>/dev/null || true
  if g "$d" merge --ff-only origin/main >/dev/null 2>&1; then
    log "$slug ff to origin/main ($behind applied)"
  else
    log "WARN $slug ff blocked (dirty tree); leaving for the loop's own pull"
  fi
  [[ "$was_active" == "active" ]] && { systemctl start "$unit"; log "$slug loop restarted"; }
}

log "=== bubble-deploy START (dry_run=$DRY_RUN infra_only=$INFRA_ONLY dept=${ONE_DEPT:-all}) ==="

# 1. shared infra
sync_repo_reset "$INFRA_DIR"; INFRA_RC=$?

# 2. depts
if [[ "$INFRA_ONLY" != "1" ]]; then
  if [[ -n "$ONE_DEPT" ]]; then
    sync_dept_ff "$ONE_DEPT"
  else
    for dd in "$AGENTS_ROOT"/bubble-ops-*; do
      [[ -d "$dd" ]] || continue
      slug=$(basename "$dd"); slug=${slug#bubble-ops-}
      # only depts with a live loop unit
      systemctl cat "ops-loop-$slug.service" >/dev/null 2>&1 && sync_dept_ff "$slug"
    done
  fi
fi

log "=== bubble-deploy DONE ==="
