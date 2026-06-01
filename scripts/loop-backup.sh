#!/usr/bin/env bash
# loop-backup.sh — twice-daily BACKUP execution for ops-loop depts.
#
# Why (Joris 2026-06-01): each dept runs a persistent `/loop` session. If
# that session dies for ANY reason (auth lapse, crash, OOM, parked after a
# restart, …) the dept silently stops working while systemd still says
# "active". This is a SAFETY NET, independent of the live loop: it fires on
# a schedule, and for each dept either
#   - SKIPS (the live loop is healthy — recent heartbeat), or
#   - runs ONE dispatch tick via `claude -p` (the loop is dead/parked).
#
# It is NOT a second loop and NOT a re-arm. One tick, then exit. A flock
# mutex guarantees the backup tick never overlaps a live tick, so the
# dept's queue is never double-processed.
#
# Deploy: part of the bubble-ops-loop install package (see deploy/ +
# scripts/install-loop-backup.sh). Runs as the `claude` user via the
# loop-backup.timer (08:00 + 14:00 Europe/Paris).
#
# Per-dept requirements (already true for live depts):
#   - WorkingDirectory   = /home/claude/agents/bubble-ops-<slug>
#   - env file           = /run/claude-agent-<slug>/env  (has CLAUDE_CODE_OAUTH_TOKEN)
#   - outputs/<date>/heartbeat.log  (the liveness signal)

set -euo pipefail

# Depts to back up. Override with BUBBLE_BACKUP_DEPTS="maya tony" for testing.
DEPTS=(${BUBBLE_BACKUP_DEPTS:-maya tony cgp})
STALE_AFTER_SEC="${BUBBLE_BACKUP_STALE_SEC:-5400}"   # 90 min
BUDGET_USD="${BUBBLE_BACKUP_BUDGET_USD:-3.00}"
MODEL="${BUBBLE_BACKUP_MODEL:-sonnet}"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-/home/claude/bubble-ops-loop}"
PY="${REPO_ROOT}/venv/bin/python"
LOCK_DIR="/run/lock"
DRY_RUN="${BUBBLE_BACKUP_DRY_RUN:-0}"

TS() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(TS)] [loop-backup] $*"; }

# The single-tick prompt. Explicitly ONE tick, no /loop.
read -r -d '' TICK_PROMPT <<'PROMPT' || true
You are running as a BACKUP tick because your persistent /loop appears
to have stopped. Execute EXACTLY ONE dispatch tick per your CLAUDE.md
operating protocol — git pull, decide_dispatch, spawn the chosen layer
subagent (if any), validate its output, commit+push, and notify Joris on
Telegram only if a gate was created or a subagent failed. Then STOP. Do
NOT start a /loop. Do NOT run more than one tick. If decide_dispatch
returns heartbeat, just write the heartbeat line and exit.
PROMPT

run_backup_tick() {
    local slug="$1" workdir="$2" envfile="$3"
    local lock="${LOCK_DIR}/ops-loop-${slug}.tick.lock"

    if [[ "$DRY_RUN" == "1" ]]; then
        log "$slug: DRY_RUN — would run one backup tick (lock=$lock)"
        return 0
    fi

    # flock -n: if the live loop (or a prior backup) holds the lock, skip —
    # do NOT block or overlap. The live tick must take the same lock for this
    # to be airtight; until then, the freshness gate is the primary guard and
    # flock prevents two BACKUP ticks from overlapping.
    exec 9>"$lock"
    if ! flock -n 9; then
        log "$slug: lock held (a tick is already running) — skipping backup"
        return 0
    fi

    log "$slug: running ONE backup tick (model=$MODEL budget=\$$BUDGET_USD)"
    local runlog; runlog="$(mktemp)"
    # Source the dept env (brings CLAUDE_CODE_OAUTH_TOKEN + per-dept vars) in a
    # subshell so it doesn't leak across depts.
    (
        set -a
        # shellcheck disable=SC1090
        [[ -f "$envfile" ]] && . "$envfile"
        set +a
        cd "$workdir" || exit 1
        /usr/bin/claude \
            --print \
            --no-session-persistence \
            --setting-sources user \
            --model "$MODEL" \
            --max-budget-usd "$BUDGET_USD" \
            --output-format json \
            --dangerously-skip-permissions \
            "$TICK_PROMPT"
    ) >"$runlog" 2>&1
    local exit=$?
    log "$slug: backup tick exit=$exit"
    rm -f "$runlog"
    flock -u 9 || true
    return $exit
}

OVERALL=0
for slug in "${DEPTS[@]}"; do
    workdir="/home/claude/agents/bubble-ops-${slug}"
    envfile="/run/claude-agent-${slug}/env"
    if [[ ! -d "$workdir" ]]; then
        log "$slug: SKIP — workdir $workdir not found"
        continue
    fi
    # Pure decision (heartbeat freshness).
    decision="$("$PY" - "$workdir/outputs" "$STALE_AFTER_SEC" <<'PYEOF'
import sys, time
sys.path.insert(0, "/home/claude/bubble-ops-loop")
from scripts.lib.loop_backup import latest_heartbeat_epoch, backup_decision
outputs, stale = sys.argv[1], int(sys.argv[2])
hb = latest_heartbeat_epoch(outputs)
d = backup_decision(hb, time.time(), stale)
print(d["action"] + "\t" + d["reason"])
PYEOF
)"
    action="${decision%%$'\t'*}"
    reason="${decision#*$'\t'}"
    if [[ "$action" == "skip" ]]; then
        log "$slug: skip — $reason"
        continue
    fi
    log "$slug: $reason"
    run_backup_tick "$slug" "$workdir" "$envfile" || OVERALL=1
done

log "done (depts=${DEPTS[*]})"
exit $OVERALL
