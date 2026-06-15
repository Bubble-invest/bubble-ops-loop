#!/usr/bin/env bash
# =============================================================================
# local-loop-backup-runner.sh — the per-tick body of the Mac BACKUP FLOOR.
#
# Invoked by the launchd backup agent (com.bubble.ops-loop-backup-<slug>) on its
# StartInterval. The Mac twin of the VPS loop-backup.sh runner, for ONE local
# dept. It:
#   1. Reads the dept's heartbeat staleness via the shared is_heartbeat_stale
#      (which reuses scripts/lib/loop_backup.py — same staleness def as the VPS).
#   2. If FRESH (the main /loop is ticking): log + exit 0 (no double-tick).
#   3. If STALE (main loop wedged / Mac just woke / never ticked): force ONE tick
#      of the dept's /loop, then exit.
#
# FAIL-SAFE: a staleness-check error is treated as STALE (tick) by the lib, never
# a crash. A force-tick that itself fails logs but still exits 0 (a backstop must
# not flap the launchd agent into a fast-respawn loop).
#
# TEST-SAFE: WITHOUT --activate-tick it only DECIDES + PRINTS (no `claude`
# launched). The real force-tick runs only under --activate-tick, so a test can
# exercise the decision path with zero side effects.
#
# Usage:
#   local-loop-backup-runner.sh --dept-dir <path> --slug <slug>
#                               [--stale-sec <sec>] [--claude-bin <path>]
#                               [--activate-tick]
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/local_loop_lib.sh
. "$SCRIPT_DIR/lib/local_loop_lib.sh"

DEPT_DIR=""
SLUG=""
STALE_SEC="$LOCAL_LOOP_STALE_SEC_DEFAULT"
CLAUDE_BIN="${LOCAL_LOOP_CLAUDE_BIN:-claude}"
ACTIVATE_TICK=0

die() { echo "ERR: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dept-dir)       DEPT_DIR="${2:?--dept-dir needs a value}"; shift 2 ;;
        --dept-dir=*)     DEPT_DIR="${1#--dept-dir=}"; shift ;;
        --slug)           SLUG="${2:?--slug needs a value}"; shift 2 ;;
        --slug=*)         SLUG="${1#--slug=}"; shift ;;
        --stale-sec)      STALE_SEC="${2:?--stale-sec needs a value}"; shift 2 ;;
        --stale-sec=*)    STALE_SEC="${1#--stale-sec=}"; shift ;;
        --claude-bin)     CLAUDE_BIN="${2:?}"; shift 2 ;;
        --claude-bin=*)   CLAUDE_BIN="${1#--claude-bin=}"; shift ;;
        --activate-tick)  ACTIVATE_TICK=1; shift ;;
        -h|--help)        sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "unknown argument '$1'" ;;
    esac
done

[[ -n "$DEPT_DIR" ]] || die "--dept-dir is required"
[[ -n "$SLUG" ]] || die "--slug is required"

TS()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(TS)] [local-loop-backup:${SLUG}] $*"; }

STATE="$(is_heartbeat_stale "$DEPT_DIR" "$STALE_SEC")"

if [[ "$STATE" == "fresh" ]]; then
    log "heartbeat FRESH (≤ ${STALE_SEC}s) — main /loop alive, no backup tick"
    exit 0
fi

log "heartbeat STALE (> ${STALE_SEC}s, or missing) — main /loop wedged/asleep; backup force-tick"

if [[ "$ACTIVATE_TICK" != "1" ]]; then
    log "(dry) would force-tick: cd '$DEPT_DIR' && '$CLAUDE_BIN' (pass --activate-tick to actually run)"
    exit 0
fi

# Force ONE tick of the dept's /loop. The dept's CLAUDE.md drives STEP A-F.
# Fail-open: a tick error logs but exits 0 so launchd doesn't fast-respawn.
if cd "$DEPT_DIR" 2>/dev/null; then
    "$CLAUDE_BIN" --dangerously-skip-permissions || log "force-tick exited non-zero (logged, not fatal)"
else
    log "could not cd into dept-dir '$DEPT_DIR' (logged, not fatal)"
fi
exit 0
