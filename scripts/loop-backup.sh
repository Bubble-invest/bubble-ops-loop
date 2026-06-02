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
# The claude binary. Overridable (BUBBLE_BACKUP_CLAUDE_BIN) so the test harness
# can substitute a stub and exercise the run-branch WITHOUT spending a real tick.
CLAUDE_BIN="${BUBBLE_BACKUP_CLAUDE_BIN:-/usr/bin/claude}"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-/home/claude/bubble-ops-loop}"
PY="${REPO_ROOT}/venv/bin/python"
# Per-dept workdir base + flock dir. Overridable (BUBBLE_BACKUP_AGENTS_ROOT /
# BUBBLE_BACKUP_LOCK_DIR) so the test harness can run hermetically inside a
# tmpdir; production defaults are unchanged.
AGENTS_ROOT="${BUBBLE_BACKUP_AGENTS_ROOT:-/home/claude/agents}"
LOCK_DIR="${BUBBLE_BACKUP_LOCK_DIR:-/run/lock}"

# ── DRY_RUN resolution (footgun fix) ────────────────────────────────────────
# Historical incident: someone set `DRY_RUN=1` expecting the safety net to no-op,
# but the script only honored `BUBBLE_BACKUP_DRY_RUN` — so `DRY_RUN` was silently
# ignored and 3 real backup ticks fired (cgp exited 1). Accept BOTH names now.
#
# Precedence: the canonical `BUBBLE_BACKUP_DRY_RUN`, if SET (even to 0), wins —
# it is the explicit, namespaced knob. Only when the canonical var is UNSET do
# we honor the bare `DRY_RUN` that people actually type. The effective state +
# its source are logged LOUDLY on startup (see below) so an ignored knob can
# never again pass silently.
if [[ -n "${BUBBLE_BACKUP_DRY_RUN+x}" ]]; then
    DRY_RUN="${BUBBLE_BACKUP_DRY_RUN}"
    DRY_RUN_SOURCE="BUBBLE_BACKUP_DRY_RUN"
elif [[ -n "${DRY_RUN+x}" ]]; then
    DRY_RUN="${DRY_RUN}"
    DRY_RUN_SOURCE="DRY_RUN"
else
    DRY_RUN="0"
    DRY_RUN_SOURCE="default"
fi
# Normalize: treat anything other than an explicit truthy value as 0, so a
# stray `DRY_RUN=yes`/`true` still means "don't run" (fail-safe toward dry).
case "${DRY_RUN,,}" in
    1|true|yes|on) DRY_RUN="1" ;;
    *)             DRY_RUN="0" ;;
esac

# Event log the cockpit reads to surface this safety net in the front end
# (Joris msg 1171). Keep in sync with console.settings.BACKUP_LOG_PATH.
BACKUP_LOG="${BUBBLE_BACKUP_LOG:-${REPO_ROOT}/state/loop-backup.jsonl}"

TS() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(TS)] [loop-backup] $*"; }

# LOUD startup banner: which dry-run knob won, and the resolved 0/1. This is
# the antidote to the historical "DRY_RUN=1 was ignored" footgun — the effective
# state is always on the record.
log "DRY_RUN resolved to $DRY_RUN from $DRY_RUN_SOURCE"
# If the canonical var is unset but SOME other DRY_RUN*-ish env var is set
# (e.g. a typo like DRYRUN or BACKUP_DRY_RUN), warn — a near-miss knob that is
# NOT being honored should never pass silently again.
if [[ "$DRY_RUN_SOURCE" != "BUBBLE_BACKUP_DRY_RUN" ]]; then
    while IFS= read -r _dr_var; do
        case "$_dr_var" in
            BUBBLE_BACKUP_DRY_RUN|DRY_RUN_SOURCE) continue ;;  # canonical / our own
            DRY_RUN) [[ "$DRY_RUN_SOURCE" == "DRY_RUN" ]] && continue ;;  # already honored
        esac
        log "WARN: env var '$_dr_var' looks dry-run-ish but is NOT honored — use BUBBLE_BACKUP_DRY_RUN (or DRY_RUN)."
    done < <(compgen -v | grep -i 'dry.*run' || true)
fi

# Append one event to the cockpit log. Never fatal — a logging failure must
# not abort the safety net itself.
emit_event() {
    local slug="$1" action="$2" reason="$3" age="${4:-}" exit_code="${5:-}"
    "$PY" - "$BACKUP_LOG" "$slug" "$action" "$reason" "$age" "$exit_code" <<'PYEOF' || log "$slug: warn — could not write event to $BACKUP_LOG"
import sys
sys.path.insert(0, "/home/claude/bubble-ops-loop")
from scripts.lib.loop_backup import format_event, append_event
path, slug, action, reason, age, exit_code = sys.argv[1:7]
ev = format_event(
    slug, action, reason,
    age_sec=int(age) if age not in ("", "None") else None,
    exit_code=int(exit_code) if exit_code not in ("", "None") else None,
)
append_event(path, ev)
PYEOF
}

# ── Notify-on-fire ──────────────────────────────────────────────────────────
# When the safety net ACTUALLY RUNS a backup tick for a dept (NOT on a skip /
# fresh loop), ping Joris on Telegram so he knows a primary loop was down and
# the net caught it. Rides the SAME shared send path as WS3's per-layer pings:
# the promoted, dept-agnostic `scripts/lib/notify.py` TelegramBackend (direct
# api.telegram.org POST, reads TELEGRAM_BOT_TOKEN from env, per-channel failure
# isolation). We deliberately do NOT use WS3's `notify_layer_fired` because its
# message shape is layer-specific (`🔁 <dept> · L<N> fired`); this is a distinct
# safety-net event (`🛟`).
#
# Recipient: Joris's Telegram chat_id. tony/cgp have no config.yaml (only maya
# does), so we can't lean on per-dept `resolve_recipients`; the backup net pings
# ONE human (Joris) regardless of dept. chat_id is overridable via
# BUBBLE_BACKUP_TELEGRAM_CHAT_ID (default = Joris, confirmed msg 1946).
#
# TELEGRAM_BOT_TOKEN must be in the environment — the caller sources the dept
# envfile (which carries it) before invoking this. Never fatal: a notify failure
# must not abort the safety net (mirrors emit_event's posture).
BACKUP_CHAT_ID="${BUBBLE_BACKUP_TELEGRAM_CHAT_ID:-6532205130}"

notify_backup_fired() {
    local slug="$1" age="${2:-}" exit_code="${3:-}"
    local age_h
    if [[ -n "$age" && "$age" != "None" ]]; then
        age_h="$(( age / 60 ))m"
    else
        age_h="unknown"
    fi
    local msg="🛟 backup tick fired for ${slug} (primary loop stale ${age_h}) — exit=${exit_code}"

    # Test seam: a stub command can capture the would-be send without HTTP.
    if [[ -n "${BUBBLE_BACKUP_NOTIFY_CMD:-}" ]]; then
        "${BUBBLE_BACKUP_NOTIFY_CMD}" "$slug" "$BACKUP_CHAT_ID" "$msg" \
            || log "$slug: warn — notify stub failed"
        return 0
    fi

    "$PY" - "$BACKUP_CHAT_ID" "$msg" <<'PYEOF' || log "$slug: warn — could not send backup-fired Telegram ping"
import sys
sys.path.insert(0, "/home/claude/bubble-ops-loop")
from scripts.lib.notify import TelegramBackend, NotificationPayload
chat_id, msg = sys.argv[1], sys.argv[2]
receipt = TelegramBackend({}).send(
    NotificationPayload(subject=msg, markdown_body="",
                        metadata={"kind": "backup_fired"}),
    chat_id,
)
if not receipt.success:
    # Non-zero so the bash caller logs a warn line (but never aborts).
    sys.stderr.write(f"backup-fired ping not delivered: {receipt.error}\n")
    sys.exit(1)
PYEOF
}

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
        return 99   # sentinel: skipped (no tick ran) → caller must NOT notify
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
        "$CLAUDE_BIN" \
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
    workdir="${AGENTS_ROOT}/bubble-ops-${slug}"
    envfile="/run/claude-agent-${slug}/env"
    if [[ ! -d "$workdir" ]]; then
        log "$slug: SKIP — workdir $workdir not found"
        continue
    fi
    # Pure decision (heartbeat freshness). Emits action, reason, age_sec
    # (tab-separated) so we can record age in the cockpit event.
    decision="$("$PY" - "$workdir/outputs" "$STALE_AFTER_SEC" <<'PYEOF'
import sys, time
sys.path.insert(0, "/home/claude/bubble-ops-loop")
from scripts.lib.loop_backup import latest_heartbeat_epoch, backup_decision
outputs, stale = sys.argv[1], int(sys.argv[2])
hb = latest_heartbeat_epoch(outputs)
d = backup_decision(hb, time.time(), stale)
print(d["action"] + "\t" + d["reason"] + "\t" + ("" if d["age_sec"] is None else str(d["age_sec"])))
PYEOF
)"
    action="$(cut -f1 <<<"$decision")"
    reason="$(cut -f2 <<<"$decision")"
    age="$(cut -f3 <<<"$decision")"
    if [[ "$action" == "skip" ]]; then
        log "$slug: skip — $reason"
        emit_event "$slug" "skip" "$reason" "$age"
        continue
    fi
    log "$slug: $reason"
    if [[ "$DRY_RUN" == "1" ]]; then
        # Record the decision even in dry-run so a smoke test of the schedule
        # shows up in the cockpit, without spending a real tick.
        emit_event "$slug" "skip" "DRY_RUN — would run a backup tick ($reason)" "$age"
        continue
    fi
    tick_exit=0
    run_backup_tick "$slug" "$workdir" "$envfile" || tick_exit=$?
    if [[ "$tick_exit" == "99" ]]; then
        # Lock held by a concurrent (live) tick → no backup tick ran. Record a
        # skip and do NOT ping — nothing fired.
        emit_event "$slug" "skip" "lock held (concurrent tick) — $reason" "$age"
        continue
    fi
    emit_event "$slug" "run" "$reason" "$age" "$tick_exit"
    [[ "$tick_exit" == "0" ]] || OVERALL=1
    # Notify-on-fire: a backup tick ACTUALLY RAN for this dept (covers every
    # dept in DEPTS, both success and failure exit). Source the dept env in a
    # subshell so TELEGRAM_BOT_TOKEN is available to the send without leaking
    # across depts. Skips/lock-held are handled by `continue` above → no ping
    # on a fresh/healthy loop.
    (
        set -a
        # shellcheck disable=SC1090
        [[ -f "$envfile" ]] && . "$envfile"
        set +a
        notify_backup_fired "$slug" "$age" "$tick_exit"
    )
done

log "done (depts=${DEPTS[*]})"
exit $OVERALL
