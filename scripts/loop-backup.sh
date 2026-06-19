#!/usr/bin/env bash
# loop-backup.sh — daily LAYER-FLOOR + safety-net execution for ops-loop depts.
#
# Why (Joris 2026-06-01 → 2026-06-04): each dept runs a persistent `/loop`
# session. If that session dies for ANY reason (auth lapse, crash, OOM, parked
# after a restart, …) the dept silently stops working while systemd still says
# "active". This script is the FLOOR + SAFETY NET, independent of the live loop.
#
# Two modes:
#
#   1. LAYER-FLOOR mode  (`--layer N`, N in 1..4):
#      Run ITS OODA layer (L1/L2/L3/L4) for EVERY eligible dept that is STALE.
#      Four cron units (loop-layer1..4.timer) each invoke this with their layer
#      at a fixed time (L1 07:00, L2 12:00, L3 16:00, L4 19:00 Europe/Paris) so
#      every layer fires >=1x/day per dept even if the live /loop is dead. The
#      forced layer bypasses decide_dispatch — the tick prompt instructs the
#      dept to run Layer N per its CLAUDE.md protocol step 3.
#
#   2. GENERIC mode  (no `--layer`):
#      The original behavior — run ONE decide_dispatch tick (the dispatcher
#      picks the layer, almost always L1) for each stale dept. Kept for
#      backward-compat / a pure "loop fully dead" net.
#
# In BOTH modes, for each dept it either
#   - SKIPS (the live loop is healthy — recent heartbeat → no double-tick), or
#   - runs ONE tick via `claude -p` (the loop is dead/parked).
#
# It is NOT a second loop and NOT a re-arm. One tick, then exit. A flock
# mutex guarantees the backup tick never overlaps a live tick, so the
# dept's queue is never double-processed.
#
# Dept set: auto-discovered at RUNTIME by globbing $AGENTS_ROOT/bubble-ops-*
# (no hardcoded list — a NEW dept is picked up with ZERO config). A discovered
# dept is SKIPPED unless its `ops-loop-<slug>.service` exists AND is enabled
# (so paused depts like cgp and the test fixture are not ticked), and — in
# layer-floor mode — unless it has a `layers/<N>/PROMPT.md` for the forced
# layer. `BUBBLE_BACKUP_DEPTS="maya tony"` overrides discovery (for tests).
#
# Deploy: part of the bubble-ops-loop install package (see deploy/ +
# scripts/install-loop-backup.sh). Runs as the `claude` user via the
# loop-layer{1,2,3,4}.timer units.
#
# Per-dept requirements (already true for live depts):
#   - WorkingDirectory   = /home/claude/agents/bubble-ops-<slug>
#   - env file           = /run/claude-agent-<slug>/env  (has CLAUDE_CODE_OAUTH_TOKEN)
#   - outputs/<date>/heartbeat.log  (the liveness signal)
#   - ops-loop-<slug>.service        (enabled = live dept)
#   - layers/<N>/PROMPT.md           (the per-layer mission, floor mode)

set -euo pipefail

# ── arg parse: --layer N ─────────────────────────────────────────────────────
# When given, force OODA layer N (1..4) — bypass decide_dispatch and instruct
# the dept to run Layer N per its CLAUDE.md protocol. When omitted, FORCE_LAYER
# stays empty and the generic decide_dispatch tick runs (original behavior).
FORCE_LAYER=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --layer)
            FORCE_LAYER="${2:-}"
            shift 2 || { echo "ERR: --layer needs an argument (1-4)" >&2; exit 2; }
            ;;
        --layer=*)
            FORCE_LAYER="${1#--layer=}"
            shift
            ;;
        *)
            echo "ERR: unknown argument '$1' (only --layer N is supported)" >&2
            exit 2
            ;;
    esac
done
if [[ -n "$FORCE_LAYER" && ! "$FORCE_LAYER" =~ ^[1-4]$ ]]; then
    echo "ERR: --layer must be 1, 2, 3 or 4 (got '$FORCE_LAYER')" >&2
    exit 2
fi

# Depts to back up. UNSET/empty → auto-discover (glob $AGENTS_ROOT/bubble-ops-*).
# Override with BUBBLE_BACKUP_DEPTS="maya tony" for testing / pinning a subset.
STALE_AFTER_SEC="${BUBBLE_BACKUP_STALE_SEC:-5400}"   # 90 min
BUDGET_USD="${BUBBLE_BACKUP_BUDGET_USD:-3.00}"
MODEL="${BUBBLE_BACKUP_MODEL:-sonnet}"
# The claude binary. Overridable (BUBBLE_BACKUP_CLAUDE_BIN) so the test harness
# can substitute a stub and exercise the run-branch WITHOUT spending a real tick.
CLAUDE_BIN="${BUBBLE_BACKUP_CLAUDE_BIN:-/usr/bin/claude}"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-/home/claude/bubble-ops-loop}"
PY="${REPO_ROOT}/venv/bin/python"

# Holds the most recent backup tick's work summary (final assistant message,
# extracted from the claude --output-format json envelope) so the caller can
# relay it to Telegram. Reset per dept by run_backup_tick. Init for set -u.
LAST_TICK_SUMMARY=""
# Per-dept workdir base + flock dir. Overridable (BUBBLE_BACKUP_AGENTS_ROOT /
# BUBBLE_BACKUP_LOCK_DIR) so the test harness can run hermetically inside a
# tmpdir; production defaults are unchanged.
AGENTS_ROOT="${BUBBLE_BACKUP_AGENTS_ROOT:-/home/claude/agents}"
LOCK_DIR="${BUBBLE_BACKUP_LOCK_DIR:-/run/lock}"
# systemctl, overridable so the test harness can stub `is-enabled` without a
# real systemd (BUBBLE_BACKUP_SYSTEMCTL="$STUB"). Default = the real binary.
SYSTEMCTL="${BUBBLE_BACKUP_SYSTEMCTL:-systemctl}"

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

# LOUD startup banner: the run mode (floor L<N> vs generic) + which dry-run knob
# won + the resolved 0/1. The mode line makes a misfiring cron unit obvious in
# the journal; the dry-run line is the antidote to the historical "DRY_RUN=1 was
# ignored" footgun — the effective state is always on the record.
if [[ -n "$FORCE_LAYER" ]]; then
    MODE_LABEL="layer-${FORCE_LAYER}"
    log "MODE: layer-floor — forcing Layer $FORCE_LAYER for every eligible dept"
else
    MODE_LABEL="generic-dispatch"
    log "MODE: generic — decide_dispatch picks the layer per dept"
fi
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

# ── Dept discovery + eligibility ─────────────────────────────────────────────
# discover_depts: when BUBBLE_BACKUP_DEPTS is unset/empty, the dept set is the
# basenames (minus the bubble-ops- prefix) of $AGENTS_ROOT/bubble-ops-* dirs.
# When BUBBLE_BACKUP_DEPTS is set, it wins verbatim (test / pin override).
discover_depts() {
    if [[ -n "${BUBBLE_BACKUP_DEPTS:-}" ]]; then
        printf '%s\n' ${BUBBLE_BACKUP_DEPTS}
        return 0
    fi
    local d slug
    for d in "${AGENTS_ROOT}"/bubble-ops-*; do
        [[ -d "$d" ]] || continue          # no match → glob stays literal; -d guards it
        slug="$(basename "$d")"
        slug="${slug#bubble-ops-}"
        printf '%s\n' "$slug"
    done
}

# dept_host <slug>: where does this dept's loop RUN? Reads the `host:` field from
# its onboarding/STATE.yaml. Echoes "vps" (default) or "local".
#   - Hybrid local/VPS agent (Joris msg 4258, 2026-06-11): a dept can declare
#     host: local (e.g. Miranda on Jade's Mac) so its /loop runs on its OWN
#     machine (real Chrome/tools), NOT on the VPS floor. The VPS still SEES it
#     (the synced read-only clone stays on disk for the cockpit) but must NOT
#     try to execute its layer here — the VPS cannot run a local dept's loop.
#   - FAIL-SAFE: a missing/unreadable/malformed STATE.yaml, or any host value
#     that isn't exactly "local", resolves to "vps" — i.e. the existing
#     execute-on-the-floor behaviour. A bad STATE must NEVER crash the floor and
#     must NEVER silently mute a real vps dept.
dept_host() {
    local slug="$1"
    local state="${AGENTS_ROOT}/bubble-ops-${slug}/onboarding/STATE.yaml"
    [[ -f "$state" ]] || { echo "vps"; return 0; }
    # Top-level `host:` only (anchored, no leading space) so a nested key can't
    # be mistaken for the dept host. Tolerate quotes + trailing comment. A grep
    # miss (no such line / unreadable) → empty → defaults to vps below.
    local val
    val="$(grep -E '^host:[[:space:]]*' "$state" 2>/dev/null | head -n1 \
            | sed -E 's/^host:[[:space:]]*//; s/[[:space:]]*(#.*)?$//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/')"
    if [[ "$val" == "local" ]]; then
        echo "local"
    else
        echo "vps"
    fi
}

# dept_eligible <slug>: is this dept a LIVE, tickable dept?
#   - its ops-loop-<slug>.service must EXIST and be ENABLED (so paused depts
#     like cgp and the disabled test fixture are skipped); `is-enabled` exits 0
#     only for enabled, 1 for disabled, 4/non-zero for not-found.
#   - in layer-floor mode, it must have layers/<FORCE_LAYER>/PROMPT.md (so a
#     dept that doesn't run that layer — e.g. a fixture missing layer 1 — is
#     skipped rather than handed a missing-mission tick).
# Prints a skip-reason to stdout and returns 1 when ineligible; returns 0 (no
# output) when eligible.
dept_eligible() {
    local slug="$1"
    if ! "$SYSTEMCTL" is-enabled "ops-loop-${slug}.service" >/dev/null 2>&1; then
        echo "service ops-loop-${slug}.service not enabled (paused/absent)"
        return 1
    fi
    if [[ -n "$FORCE_LAYER" ]]; then
        local prompt="${AGENTS_ROOT}/bubble-ops-${slug}/layers/${FORCE_LAYER}/PROMPT.md"
        if [[ ! -f "$prompt" ]]; then
            echo "no layers/${FORCE_LAYER}/PROMPT.md (dept doesn't run L${FORCE_LAYER})"
            return 1
        fi
    fi
    return 0
}

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

# ── Truthful external heartbeat (Rick 2026-06-19) ────────────────────────────
# The fleet liveness signal is a free-text line each dept writes ITSELF into
# outputs/<date>/heartbeat.log; every consumer (watchdog, this floor's freshness
# gate, cockpit) only checks the timestamp FRESHNESS, never the truth. So when a
# session dies the thing that writes the heartbeat dies with it → a SILENT hole,
# no "I'm down" signal (Maya 2026-06-18: 13h hole; Ben 2026-06-18: 0 heartbeat
# lines — the degraded honesty lived only in state/loop-backup.jsonl, which the
# watchdog ignores).
#
# When the floor intervenes on a STALE dept, it (the external observer that
# already detected the staleness) becomes the AUTHORITATIVE writer of a TRUTHFUL
# line into the dept's OWN heartbeat.log, encoding the real OUTCOME:
#   loop stale + backup ran OK     → `tick BACKUP-RAN-FOR-DEPT layer=N exit=0`
#   loop stale + backup FAILED     → `tick BACKUP-FAILED exit=N — dept DOWN`
#   degraded L4 carried-over       → `tick DEGRADED-L4 carried-over`
# This collapses the two channels (heartbeat freshness vs loop-backup.jsonl
# truth) into ONE signal a downstream consumer can read straight off the tail.
# The line keeps the `<iso> tick ...` shape so latest_heartbeat_epoch and every
# freshness reader keep working unchanged. Never fatal — a write failure must
# not abort the safety net.
write_external_heartbeat() {
    local slug="$1" outcome="$2" layer="${3:-}" exit_code="${4:-}"
    local hb="${AGENTS_ROOT}/bubble-ops-${slug}/outputs/$(date -u +%Y-%m-%d)/heartbeat.log"
    "$PY" - "$hb" "$outcome" "$layer" "$exit_code" <<'PYEOF' || log "$slug: warn — could not write truthful heartbeat to $hb"
import sys
sys.path.insert(0, "/home/claude/bubble-ops-loop")
from scripts.lib.loop_backup import append_external_heartbeat
hb, outcome, layer, exit_code = sys.argv[1:5]
line = append_external_heartbeat(
    hb, outcome,
    layer=int(layer) if layer not in ("", "None") else None,
    exit_code=int(exit_code) if exit_code not in ("", "None") else None,
)
print(line)
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
    local slug="$1" age="${2:-}" exit_code="${3:-}" summary="${4:-}"
    local age_h
    if [[ -n "$age" && "$age" != "None" ]]; then
        age_h="$(( age / 60 ))m"
    else
        age_h="unknown"
    fi
    # Floor mode names the layer that fired; generic mode keeps the original
    # bare line. Either way the prefix stays 🛟 so the cockpit/console keys off it.
    local what="backup tick"
    [[ -n "$FORCE_LAYER" ]] && what="L${FORCE_LAYER} floor tick"
    local msg="🛟 ${what} fired for ${slug} (primary loop stale ${age_h}) — exit=${exit_code}"
    # Append the tick's work summary so Joris sees WHAT the layer mission did,
    # not just that the net fired. Empty summary (parse failed / heartbeat with
    # no text) falls back to the bare line above.
    if [[ -n "$summary" && "$summary" != "None" ]]; then
        msg="${msg}"$'\n\n'"${summary}"
    fi

    # Test seam: a stub command can capture the would-be send without HTTP.
    if [[ -n "${BUBBLE_BACKUP_NOTIFY_CMD:-}" ]]; then
        "${BUBBLE_BACKUP_NOTIFY_CMD}" "$slug" "$BACKUP_CHAT_ID" "$msg" \
            || log "$slug: warn — notify stub failed"
        return 0
    fi

    # The python sender takes subject + body separately and re-joins; pass the
    # bare fired-line as the subject and the tick summary as the body.
    local fired_line="🛟 ${what} fired for ${slug} (primary loop stale ${age_h}) — exit=${exit_code}"
    "$PY" - "$BACKUP_CHAT_ID" "$fired_line" "$summary" <<'PYEOF' || log "$slug: warn — could not send backup-fired Telegram ping"
import sys
sys.path.insert(0, "/home/claude/bubble-ops-loop")
from scripts.lib.notify import TelegramBackend, NotificationPayload
chat_id, subject = sys.argv[1], sys.argv[2]
body = sys.argv[3] if len(sys.argv) > 3 else ""
if body in ("", "None"):
    body = ""
receipt = TelegramBackend({}).send(
    NotificationPayload(subject=subject, markdown_body=body,
                        metadata={"kind": "backup_fired"}),
    chat_id,
)
if not receipt.success:
    # Non-zero so the bash caller logs a warn line (but never aborts).
    sys.stderr.write(f"backup-fired ping not delivered: {receipt.error}\n")
    sys.exit(1)
PYEOF
}

# ── Tick prompt ──────────────────────────────────────────────────────────────
# Two shapes. In layer-floor mode the prompt FORCES Layer N (no decide_dispatch);
# in generic mode it runs the dispatcher's choice. Both end with the concise
# report the wrapper relays to Joris on Telegram.
build_tick_prompt() {
    if [[ "${DEGRADED_L4:-0}" == "1" ]]; then
        cat <<PROMPT
You are running as a DEGRADED Layer 4 FLOOR tick. Your persistent /loop was
DOWN earlier today, so Layers 1/2/3 did NOT all run — there is no fresh daily
cycle to debrief. Do NOT fabricate trades, KPIs, prices or research.

Write a SHORT, HONEST degraded L4 export so the CEO (Tony) still gets a signal
instead of silence:
  • State plainly: the loop was down today (stale); L1/L2/L3 did not all fire.
  • Summarize from LAST KNOWN state only (yesterday's export / current
    positions / open gates already on disk) — clearly labelled as carried-over,
    not today's work.
  • Flag anything time-sensitive that the downtime may have delayed.
Still WRITE the L4 artifact (outputs/<today>/4/management-export.yaml +
risk-brief.md) marked degraded=true, so Tony's morning brief can read it.
Execute EXACTLY ONE tick, then: 1. If your /loop cron is NOT armed, arm it NOW (CronCreate, every 1h). 2. Write heartbeat. 3. STOP.

Do NOT send your own Telegram message — the backup wrapper relays your final
message to Joris automatically. END your turn with a concise report (max ~6
lines): confirm the degraded L4, what carried-over state you summarized, and
any delayed time-sensitive item.
PROMPT
    elif [[ -n "$FORCE_LAYER" ]]; then
        cat <<PROMPT
You are running as the daily Layer ${FORCE_LAYER} FLOOR tick (one of four
per-layer cron units that guarantee each OODA layer fires at least once a
day even if your persistent /loop is down or parked).

Run Layer ${FORCE_LAYER} NOW, per your CLAUDE.md operating protocol step 3:
git pull; read layers/${FORCE_LAYER}/PROMPT.md; spawn the Layer ${FORCE_LAYER}
subagent(s); VERIFY their output; validate; commit+push. Do NOT run
decide_dispatch and do NOT run any other layer — this tick is Layer
${FORCE_LAYER} specifically. Execute EXACTLY ONE tick, then:
  1. If your /loop cron is NOT armed: arm it NOW (CronCreate, every 1h).
  2. Write heartbeat to outputs/<today>/heartbeat.log.
  3. Then STOP.

Do NOT send your own Telegram message — the backup wrapper relays your
final message to Joris automatically. Instead, END your turn with a
concise report (max ~6 lines) the wrapper will forward verbatim:
  • Confirm you ran Layer ${FORCE_LAYER}.
  • What the layer mission actually DID — the concrete result/output
    (e.g. "scored 4 prospects, 1 promoted to draft"; "no new signals";
    "risk check: 2 positions flagged"). Be specific, not "ran L${FORCE_LAYER}".
  • Any gate created or subagent failure (and what it needs from Joris).
  • If there was nothing for this layer to do, say so in one line and why.
PROMPT
    else
        cat <<'PROMPT'
You are running as a BACKUP tick because your persistent /loop appears
to have stopped. Execute EXACTLY ONE dispatch tick per your CLAUDE.md
operating protocol — git pull, decide_dispatch, spawn the chosen layer
subagent (if any), validate its output, commit+push. Then STOP. Do
NOT start a /loop. Do NOT run more than one tick.

Do NOT send your own Telegram message — the backup wrapper relays your
final message to Joris automatically. Instead, END your turn with a
concise report (max ~6 lines) the wrapper will forward verbatim:
  • Which layer dispatch chose (L1/L2/L3/L4 or heartbeat).
  • What the layer mission actually DID — the concrete result/output
    (e.g. "scored 4 prospects, 1 promoted to draft"; "no new signals";
    "risk check: 2 positions flagged"). Be specific, not "ran L2".
  • Any gate created or subagent failure (and what it needs from Joris).
  • If decide_dispatch returned heartbeat: say so in one line and why
    (e.g. "queues empty, L1 already ran today").
PROMPT
    fi
}
TICK_PROMPT="$(build_tick_prompt)"


# ── Wake the LIVE session via bubble-inject, if it's alive (Joris msg 4045) ──
# A floor fire usually means the in-session cron lapsed, NOT that the session
# died. In that case the live --channels session is still up and can run the tick
# itself — cheaper (no new session) and keeps context. We detect "session alive"
# by a bun poller in the dept's systemd cgroup (same signal the watchdog uses),
# then drop "run your loop" into <state_dir>/inject (the bubble-inject patch
# delivers it as a message from Joris). We confirm it actually ticked by watching
# the heartbeat.log mtime advance; if not (session wedged/dead), the caller falls
# back to a `claude -p` backup tick.
inject_live_loop() {
    local slug="$1"
    local svc="ops-loop-${slug}.service"
    # session alive? = a bun process in this service's cgroup.
    local main_pid; main_pid=$(systemctl show "$svc" -p MainPID --value 2>/dev/null || echo 0)
    [[ "$main_pid" =~ ^[0-9]+$ ]] && (( main_pid > 0 )) || return 1
    local alive=1 pid
    for pid in $(pgrep -x bun 2>/dev/null); do
        grep -qs "$svc" "/proc/$pid/cgroup" 2>/dev/null && { alive=0; break; }
    done
    (( alive == 0 )) || return 1   # no live poller → can't inject

    local state_dir="/home/claude/.claude/channels/telegram-${slug}"
    local inject="${state_dir}/inject"
    [[ -d "$state_dir" ]] || return 1
    local hb="${AGENTS_ROOT}/bubble-ops-${slug}/outputs/$(date -u +%Y-%m-%d)/heartbeat.log"
    local before; before=$(stat -c %Y "$hb" 2>/dev/null || echo 0)

    log "$slug: live session alive — injecting 'run your loop' (no -p spawn)"
    printf 'Arm a /loop cron every 1h. Then run your full tick: STEP A (safe_pull) -> STEP B (read queues) -> STEP C (decide_dispatch) -> STEP D (dispatch chosen layer subagent) -> STEP E (commit+push runtime paths) -> STEP F (Telegram notify). Always write heartbeat to outputs/<today>/heartbeat.log.\n' >> "$inject" 2>/dev/null || return 1

    # Wait up to ~240s for the live session to tick (heartbeat mtime advances).
    # 90s was too short: the inject IS delivered but a quiet session can take a
    # couple minutes to wake + run a tick (esp. opus depts), so the floor fell back
    # to -p even when the inject would have worked (Joris 2026-06-08, all 3 depts).
    local i after
    for i in $(seq 1 48); do
        sleep 5
        after=$(stat -c %Y "$hb" 2>/dev/null || echo 0)
        (( after > before )) && { log "$slug: live session ticked from inject (heartbeat advanced)"; return 0; }
    done
    log "$slug: inject sent but no tick within window — falling back to backup -p"
    return 1
}

run_backup_tick() {
    local slug="$1" workdir="$2" envfile="$3"
    local lock="${LOCK_DIR}/ops-loop-${slug}.tick.lock"
    LAST_TICK_SUMMARY=""   # reset so a parse miss never relays a prior dept's summary

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
    # Extract the tick's final assistant message (the work summary) from the
    # `claude --output-format json` envelope so the caller can relay it to
    # Telegram. Never fatal — a parse failure just yields an empty summary and
    # the notify falls back to the bare fired-line. Stored in a module global
    # because the function already uses its return code for the exit status.
    LAST_TICK_SUMMARY="$(
        "$PY" - "$runlog" <<'PYEOF' 2>/dev/null || true
import sys, json
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    # The JSON envelope is the last well-formed object in the stream; --print
    # --output-format json emits a single top-level object, but tolerate any
    # leading log noise by scanning from the last '{'.
    obj = None
    try:
        obj = json.loads(raw)
    except Exception:
        start = raw.rfind('{"type"')
        if start == -1:
            start = raw.find("{")
        if start != -1:
            obj = json.loads(raw[start:])
    text = ""
    if isinstance(obj, dict):
        text = obj.get("result") or ""
    print(text.strip()[:1500])
except Exception:
    pass
PYEOF
    )"
    rm -f "$runlog"
    flock -u 9 || true
    return $exit
}

OVERALL=0
mapfile -t DEPTS < <(discover_depts)
if [[ -n "${BUBBLE_BACKUP_DEPTS:-}" ]]; then
    log "depts (override): ${DEPTS[*]:-<none>}"
else
    log "depts (auto-discovered from ${AGENTS_ROOT}/bubble-ops-*): ${DEPTS[*]:-<none>}"
fi

# ── CEO directive dispatch (runs once per tick, BEFORE the per-dept loop) ─────
# Deliver Joris-approved directives from the manager dept's outbound queue into
# the target children's queues/management/ inboxes. This is the ONLY actor that
# crosses repo boundaries (the manager is isolated to its own repo). Pure
# mechanical relay (NOT claude -p — template Ban #2). Gated: only directives with
# approved_by=joris AND status=approved ship. Idempotent + never-fatal so it is
# safe on every layer-floor moment. Disable with BUBBLE_DISPATCH_DIRECTIVES=0.
if [[ "${BUBBLE_DISPATCH_DIRECTIVES:-1}" == "1" ]]; then
    _dispatcher="$(dirname "${BASH_SOURCE[0]}")/dispatch_directives.py"
    if [[ -f "$_dispatcher" ]]; then
        log "dispatch: relaying approved CEO directives (manager=${BUBBLE_DISPATCH_MANAGER:-tony})"
        python3 "$_dispatcher" \
            --agents-root "$AGENTS_ROOT" \
            --manager "${BUBBLE_DISPATCH_MANAGER:-tony}" \
            ${DRY_RUN:+--dry-run} 2>&1 | while IFS= read -r _l; do log "$_l"; done || true
    fi
fi

for slug in "${DEPTS[@]}"; do
    [[ -n "$slug" ]] || continue
    workdir="${AGENTS_ROOT}/bubble-ops-${slug}"
    envfile="/run/claude-agent-${slug}/env"
    if [[ ! -d "$workdir" ]]; then
        log "$slug: SKIP — workdir $workdir not found"
        continue
    fi
    # Host gate (Hybrid local/VPS agent): a host: local dept runs its loop on its
    # OWN machine (real Chrome/tools), so the VPS floor must NOT execute its layer
    # — but it STAYS discovered (its synced clone remains on disk for the cockpit).
    # Record the skip so the cockpit shows the dept; do NOT tick, do NOT ping.
    if [[ "$(dept_host "$slug")" == "local" ]]; then
        log "skip $slug (host: local — runs on its own machine, not the VPS floor)"
        emit_event "$slug" "skip" "host: local (runs on its own machine, not the VPS floor)"
        continue
    fi
    # Eligibility gate: enabled service (+ layer prompt in floor mode). A skip
    # here is a structural skip (not a freshness skip) — record it so a paused/
    # layerless dept is visible in the cockpit, but do NOT ping.
    if ! elig_reason="$(dept_eligible "$slug")"; then
        log "$slug: SKIP — $elig_reason"
        emit_event "$slug" "skip" "$elig_reason"
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
    # Forced-layer BACKUP-OFFSET + PREREQUISITE gate (Joris msgs 3904 + 3911,
    # 2026-06-06). The backup cron is a SAFETY NET, not a replacement: it must
    # give the live /loop a head start, then only catch up on a STALE loop.
    #   (a) BACKUP OFFSET: a forced layer N is withheld until now >= its Paris
    #       min-time PLUS BUBBLE_BACKUP_LAYER_OFFSET_H (default 2h). Before that
    #       the live loop owns the layer; the backup never races it.
    #   (b) PREREQUISITE: L4 still requires L1/L2/L3 fired today (sequences the
    #       aggregator last even in the backup path).
    # decide_dispatch's constants are the single source of truth for min-times.
    DEGRADED_L4=0
    if [[ -n "$FORCE_LAYER" ]]; then
        layer_ok="$("$PY" - "$workdir" "$FORCE_LAYER" "${BUBBLE_BACKUP_LAYER_OFFSET_H:-2}" <<'PYEOF2' 2>/dev/null || echo "ERR"
import sys
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/home/claude/bubble-ops-loop")
from scripts.lib.dispatch_helpers import (
    build_dispatch_ctx, _LAYER_MIN_TIME, _to_paris, _layer_fired_today,
)
workdir, layer, offset_h = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
ctx = build_dispatch_ctx(workdir, now_utc=datetime.now(timezone.utc))
now_paris = _to_paris(ctx["now_utc"])
# min-time + backup offset, as a Paris-local datetime today
mt = _LAYER_MIN_TIME[layer]
earliest = now_paris.replace(hour=mt.hour, minute=mt.minute, second=0, microsecond=0) + timedelta(hours=offset_h)
if now_paris < earliest:
    print("EARLY"); sys.exit(0)
if layer == 4 and not all(_layer_fired_today(ctx, n) for n in (1, 2, 3)):
    print("PREREQ"); sys.exit(0)
print("OK")
PYEOF2
)"
        if [[ "$layer_ok" == "EARLY" ]]; then
            log "$slug: SKIP backup L$FORCE_LAYER — within the live-loop head-start window (min-time + ${BUBBLE_BACKUP_LAYER_OFFSET_H:-2}h)"
            emit_event "$slug" "skip" "backup L$FORCE_LAYER offset not reached (live loop owns it)" "$age"
            continue
        elif [[ "$layer_ok" == "PREREQ" ]]; then
            # L1/L2/L3 did NOT all fire today AND the loop is stale (we only reach
            # here for stale depts). Rather than skip L4 entirely — which leaves
            # Tony with NO export and a false "dept silent" reading (Joris 2026-06-07,
            # Ben/Maya 06-06) — run a DEGRADED L4: a short, honest "loop was down,
            # earlier layers did not run" debrief from last-known state. Tony always
            # gets SOMETHING; the dept never disappears from the morning brief.
            log "$slug: DEGRADED backup L4 — L1/L2/L3 not all fired today + loop stale; running a degraded debrief"
            emit_event "$slug" "degraded" "backup L4 degraded (L1/2/3 pending, loop stale)" "$age"
            DEGRADED_L4=1
        fi
        # "OK" or "ERR" (fail-open: a check error runs the tick as before).
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        # Record the decision even in dry-run so a smoke test of the schedule
        # shows up in the cockpit, without spending a real tick.
        emit_event "$slug" "skip" "DRY_RUN — would run a backup tick ($reason)" "$age"
        continue
    fi
    tick_exit=0
    # Prefer waking the LIVE session (cheaper, keeps context) when it's alive and
    # this isn't a degraded-L4 (which needs its own prompt). Fall back to -p.
    if [[ "${DEGRADED_L4:-0}" != "1" ]] && inject_live_loop "$slug"; then
        emit_event "$slug" "run" "live-loop woken via inject — $reason" "$age" 0
        continue
    fi
    run_backup_tick "$slug" "$workdir" "$envfile" || tick_exit=$?
    if [[ "$tick_exit" == "99" ]]; then
        # Lock held by a concurrent (live) tick → no backup tick ran. Record a
        # skip and do NOT ping — nothing fired.
        emit_event "$slug" "skip" "lock held (concurrent tick) — $reason" "$age"
        continue
    fi
    emit_event "$slug" "run" "$reason" "$age" "$tick_exit"
    [[ "$tick_exit" == "0" ]] || OVERALL=1
    # Truthful external heartbeat: encode the real OUTCOME of this intervention
    # into the dept's OWN heartbeat.log so a downstream consumer (watchdog,
    # cockpit) reading the tail sees whether the dept is actually up — the
    # "I'm down" signal that was missing when the session that writes the
    # heartbeat died with the loop (Maya/Ben 2026-06-18). Three cases:
    #   degraded L4              → DEGRADED-L4 carried-over
    #   backup ran, exit 0       → BACKUP-RAN-FOR-DEPT layer=N exit=0
    #   backup tick failed (≠0)  → BACKUP-FAILED exit=N — dept DOWN
    if [[ "${DEGRADED_L4:-0}" == "1" ]]; then
        write_external_heartbeat "$slug" "DEGRADED-L4"
    elif [[ "$tick_exit" == "0" ]]; then
        write_external_heartbeat "$slug" "BACKUP-RAN-FOR-DEPT" "${FORCE_LAYER:-}" "$tick_exit"
    else
        write_external_heartbeat "$slug" "BACKUP-FAILED" "${FORCE_LAYER:-}" "$tick_exit"
    fi
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
        notify_backup_fired "$slug" "$age" "$tick_exit" "$LAST_TICK_SUMMARY"
    )
done

log "done (mode=${MODE_LABEL} depts=${DEPTS[*]:-<none>})"
exit $OVERALL
