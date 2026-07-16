#!/usr/bin/env bash
# =============================================================================
# bubble-deploy.sh — sync merged GitHub main into the live VPS.
#
# THE GAP IT CLOSES: merging a PR on GitHub does NOT update the box. The
# layer-floor tick runs $INFRA_DIR/scripts/*, and each dept
# runs from $AGENTS_ROOT/bubble-ops-<slug>/ — all git clones that only
# advance when pulled. This makes "deploy" one explicit, idempotent command.
#
# WHAT IT DOES (idempotent, safe to re-run):
#   1. bubble-ops-loop (shared infra: loop-backup.sh, dispatch_directives.py,
#      skills, templates) → fetch + reset --hard origin/main.
#   2. Each dept clone under agents/bubble-ops-* → fetch; ff to origin/main.
#      Dept loops also self-pull at tick start, but a live loop RACES a ff on the
#      working tree, so we STOP the loop, ff, restart (loop re-arms on start).
#      Skipped cleanly if a dept is ahead (unpushed work) — never clobbers.
#      After restart: verify is-active + an import-smoke of the dept entrypoint;
#      on failure, roll back to the pre-ff SHA and restart again.
#
# CONFIG (no hardcoded company facts — override via env for another org/client):
#   BUBBLE_DEPLOY_INFRA_DIR     default: /home/claude/bubble-ops-loop
#   BUBBLE_DEPLOY_AGENTS_ROOT   default: /home/claude/agents
#   BUBBLE_DEPLOY_DEPT_PREFIX   default: bubble-ops-   (dept dir/unit naming prefix)
#   BUBBLE_DEPLOY_UNIT_PREFIX   default: ops-loop-     (systemd unit naming prefix)
#   BUBBLE_DEPLOY_ENTRYPOINT    default: main.py       (relative path probed for
#                                import-smoke; skipped if not found — not every
#                                dept/client entrypoint is a plain script)
#
# EXIT CODE: 0 only if every infra/dept operation that was attempted succeeded
# or was a clean no-op skip (already current, or ahead with unpushed work).
# Any fetch failure, ff failure, or failed health-check-and-rollback → exit 1.
# A oneshot systemd unit's reported success is only meaningful if this contract
# holds (agent-native-infra-doctrine: silence must never read as success).
#
# Usage:
#   bubble-deploy.sh                 # deploy everything
#   bubble-deploy.sh --infra-only    # just bubble-ops-loop (no dept restarts)
#   bubble-deploy.sh --dept <slug>   # one dept + infra
#   bubble-deploy.sh --dry-run       # report what WOULD sync, change nothing
# =============================================================================
set -uo pipefail

INFRA_DIR="${BUBBLE_DEPLOY_INFRA_DIR:-/home/claude/bubble-ops-loop}"
AGENTS_ROOT="${BUBBLE_DEPLOY_AGENTS_ROOT:-/home/claude/agents}"
DEPT_PREFIX="${BUBBLE_DEPLOY_DEPT_PREFIX:-bubble-ops-}"
UNIT_PREFIX="${BUBBLE_DEPLOY_UNIT_PREFIX:-ops-loop-}"
ENTRYPOINT="${BUBBLE_DEPLOY_ENTRYPOINT:-main.py}"
DRY_RUN=0; INFRA_ONLY=0; ONE_DEPT=""
for a in "$@"; do case "$a" in
  --dry-run) DRY_RUN=1 ;;
  --infra-only) INFRA_ONLY=1 ;;
  --dept) ONE_DEPT="__next__" ;;
  *) [[ "$ONE_DEPT" == "__next__" ]] && ONE_DEPT="$a" ;;
esac; done

# Tracks whether ANY real failure occurred across the whole run. A clean skip
# (already current / ahead-with-unpushed-work) never sets this. Everything
# else that isn't a plain success does.
FAILED=0

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
  if g "$d" reset --hard origin/main >/dev/null; then
    log "$d reset to origin/main ($behind applied)"
    return 0
  else
    log "FAIL $d: git reset --hard origin/main failed"
    return 1
  fi
}

# health_check_dept $slug $dir $unit — is-active + a python import-smoke of the
# dept entrypoint. Mirrors the verify pattern in deploy-to-morty.sh (systemctl
# is-active check post-start). Returns 0 healthy, 1 unhealthy.
health_check_dept(){
  local slug="$1" d="$2" unit="$3"
  local active; active=$(systemctl is-active "$unit" 2>/dev/null || echo inactive)
  if [[ "$active" != "active" ]]; then
    log "HEALTH FAIL $slug: unit not active ($active)"
    return 1
  fi
  if [[ -f "$d/$ENTRYPOINT" ]]; then
    if ! sudo -u claude python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$d/$ENTRYPOINT" 2>/dev/null; then
      log "HEALTH FAIL $slug: import-smoke (ast parse) failed on $ENTRYPOINT"
      return 1
    fi
  fi
  log "$slug healthy (unit active, entrypoint smoke ok)"
  return 0
}

sync_dept_ff(){ # $1=slug — stop loop, ff, restart (avoids the live-loop race)
  local slug="$1"
  local d="$AGENTS_ROOT/${DEPT_PREFIX}${slug}" unit="${UNIT_PREFIX}${slug}.service"
  [[ -d "$d/.git" ]] || { log "skip $slug (no clone)"; return 0; }
  g "$d" config --global --get-all safe.directory 2>/dev/null | grep -qx "$d" \
    || g "$d" config --global --add safe.directory "$d" 2>/dev/null || true
  g "$d" fetch origin main --quiet || { log "FAIL fetch $slug"; FAILED=1; return 1; }
  local behind ahead; behind=$(g "$d" rev-list --count HEAD..origin/main 2>/dev/null||echo "?")
  ahead=$(g "$d" rev-list --count origin/main..HEAD 2>/dev/null||echo "?")
  if [[ "$ahead" != "0" ]]; then log "$slug: $ahead ahead (unpushed) — loop will pull itself; not forcing"; return 0; fi
  if [[ "$behind" == "0" ]]; then log "$slug already current"; return 0; fi
  if [[ "$DRY_RUN" == "1" ]]; then log "[dry-run] would stop/ff/restart $slug ($behind behind)"; return 0; fi

  local pre_sha; pre_sha=$(g "$d" rev-parse HEAD 2>/dev/null)
  local was_active; was_active=$(systemctl is-active "$unit" 2>/dev/null||echo inactive)
  [[ "$was_active" == "active" ]] && systemctl stop "$unit"

  # Working tree may be dirty (live agent work-in-progress). NEVER discard it
  # silently — stash it (recoverable) before attempting the ff. If the ff
  # can't proceed even after stashing, restore the stash and leave the dept
  # for the loop's own pull (unchanged prior behaviour, just no data loss).
  local stash_ref="" dirty=0
  if [[ -n "$(g "$d" status --porcelain 2>/dev/null)" ]]; then
    dirty=1
    local stash_msg
    stash_msg="bubble-deploy-autostash-$(date -u +%Y%m%dT%H%M%SZ)"
    if g "$d" stash push -u -m "$stash_msg" >/dev/null 2>&1; then
      stash_ref=$(g "$d" stash list 2>/dev/null | grep -F "$stash_msg" | head -1 | cut -d: -f1)
      log "$slug: dirty tree — stashed uncommitted work as '$stash_msg' ($stash_ref) — RECOVERABLE via git stash pop"
    else
      log "WARN $slug: dirty tree but stash failed — leaving for the loop's own pull (no ff attempted)"
      [[ "$was_active" == "active" ]] && { systemctl start "$unit"; log "$slug loop restarted"; }
      return 0
    fi
  fi

  local ff_ok=0
  if g "$d" merge --ff-only origin/main >/dev/null 2>&1; then
    ff_ok=1
    log "$slug ff to origin/main ($behind applied)"
  else
    log "WARN $slug ff blocked; leaving for the loop's own pull"
    [[ -n "$stash_ref" ]] && { g "$d" stash pop >/dev/null 2>&1 && log "$slug: restored stashed work after blocked ff"; }
  fi

  if [[ "$ff_ok" == "1" && "$dirty" == "1" && -n "$stash_ref" ]]; then
    if g "$d" stash pop >/dev/null 2>&1; then
      log "$slug: restored stashed work on top of ff'd HEAD"
    else
      log "WARN $slug: stash pop conflicted after ff — stash left in place ($stash_ref); needs manual reconciliation"
      FAILED=1
    fi
  fi

  if [[ "$was_active" == "active" ]]; then
    systemctl start "$unit"
    log "$slug loop restarted"
    if [[ "$ff_ok" == "1" ]]; then
      sleep 2
      if ! health_check_dept "$slug" "$d" "$unit"; then
        log "$slug: health check failed post-deploy — rolling back to pre-ff SHA $pre_sha"
        systemctl stop "$unit" 2>/dev/null || true
        if g "$d" reset --hard "$pre_sha" >/dev/null 2>&1; then
          systemctl start "$unit"
          sleep 2
          if health_check_dept "$slug" "$d" "$unit"; then
            log "$slug: ROLLED BACK to $pre_sha and restarted — now healthy again"
          else
            log "FAIL $slug: still unhealthy after rollback to $pre_sha — needs human"
          fi
        else
          log "FAIL $slug: rollback reset to $pre_sha failed — needs human"
        fi
        FAILED=1
        return 1
      fi
    fi
  fi
}

log "=== bubble-deploy START (dry_run=$DRY_RUN infra_only=$INFRA_ONLY dept=${ONE_DEPT:-all}) ==="

# 1. shared infra
sync_repo_reset "$INFRA_DIR"; INFRA_RC=$?
[[ "$INFRA_RC" == "1" ]] && FAILED=1   # rc=2 (ahead/skip) is a clean no-op, not a failure

# 2. depts
if [[ "$INFRA_ONLY" != "1" ]]; then
  if [[ -n "$ONE_DEPT" ]]; then
    sync_dept_ff "$ONE_DEPT"
  else
    for dd in "$AGENTS_ROOT"/"${DEPT_PREFIX}"*; do
      [[ -d "$dd" ]] || continue
      slug=$(basename "$dd"); slug=${slug#"$DEPT_PREFIX"}
      # only depts with a live loop unit
      systemctl cat "${UNIT_PREFIX}${slug}.service" >/dev/null 2>&1 && sync_dept_ff "$slug"
    done
  fi
fi

log "=== bubble-deploy DONE (failed=$FAILED) ==="
[[ "$FAILED" == "1" ]] && exit 1
exit 0
