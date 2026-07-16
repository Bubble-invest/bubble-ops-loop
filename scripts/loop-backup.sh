#!/usr/bin/env bash
# loop-backup.sh — daily LAYER-FLOOR + safety-net execution for ops-loop depts.
#
# Why ({{OPERATOR}} 2026-06-01 → 2026-06-04): each dept runs a persistent `/loop`
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
# Holds the mission id of the CURRENT headless tick in mission-granular floor
# mode (card #518), empty in legacy/generic mode. Set by the per-dept loop
# before each do_one_tick call; read by notify_backup_fired to tag the fired
# ping with WHICH mission ran. Init for set -u.
CURRENT_MISSION_ID=""
# Per-dept workdir base + flock dir. Overridable (BUBBLE_BACKUP_AGENTS_ROOT /
# BUBBLE_BACKUP_LOCK_DIR) so the test harness can run hermetically inside a
# tmpdir; production defaults are unchanged.
AGENTS_ROOT="${BUBBLE_BACKUP_AGENTS_ROOT:-/home/claude/agents}"
LOCK_DIR="${BUBBLE_BACKUP_LOCK_DIR:-/run/lock}"
# systemctl, overridable so the test harness can stub `is-enabled` without a
# real systemd (BUBBLE_BACKUP_SYSTEMCTL="$STUB"). Default = the real binary.
SYSTEMCTL="${BUBBLE_BACKUP_SYSTEMCTL:-systemctl}"

# ── flock portability shim (#675) ───────────────────────────────────────────
# Production always runs on Linux (util-linux `flock` present) — this branch
# is a NO-OP there (real `flock` wins) and only engages the mkdir-based
# fallback where the binary is genuinely missing (e.g. running the harness
# locally on macOS, which has no `flock`). Interface: `bkp_flock -n FD LOCKPATH`
# (non-blocking acquire) and `bkp_flock -u FD` (release) — callers pass the
# lock's own path explicitly since a portable shim can't always recover a
# filename from an fd (no /proc on macOS).
if command -v flock >/dev/null 2>&1; then
    bkp_flock() {
        local opt="$1" fd="$2"
        command flock "$opt" "$fd"
    }
else
    # mkdir is atomic on every POSIX filesystem, making it a safe substitute
    # for the non-blocking acquire; release just removes the marker dir. This
    # approximates flock's per-lockfile mutex closely enough for this script's
    # one-lock-per-dept use, but is NOT a general replacement.
    bkp_flock() {
        local opt="$1" _fd="$2" lockpath="${3:-}"
        local mkdir_lock="${lockpath}.d"
        case "$opt" in
            -n)
                mkdir "$mkdir_lock" 2>/dev/null
                ;;
            -u)
                rmdir "$mkdir_lock" 2>/dev/null || true
                return 0
                ;;
            *)
                return 1
                ;;
        esac
    }
fi

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
# ({{OPERATOR}} msg 1171). Keep in sync with console.settings.BACKUP_LOG_PATH.
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
#   - Hybrid local/VPS agent ({{OPERATOR}} msg 4258, 2026-06-11): a dept can declare
#     host: local (e.g. Miranda on {{OPERATOR_2}}'s Mac) so its /loop runs on its OWN
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

# select_forced_layer_missions <slug> <workdir>: mission-granular enumeration
# for the LAYER-FLOOR path (card #518). Sets two CALLER-visible globals (this
# function must be called directly, NEVER inside a `< <(...)` process
# substitution or any other subshell — bash runs the substituted command in a
# subshell, so a variable assignment inside it is invisible to the parent
# shell once the subshell exits; that subtlety bit an earlier version of this
# function, see the M6 test):
#   _MISSIONS_DEFINED_ON_LAYER — "1" iff dept.yaml declares ANY
#     recurring_missions on $FORCE_LAYER (a dept that hasn't migrated to the
#     mission-centric model, e.g. a legacy fixture with only
#     layers/<N>/PROMPT.md, leaves this "0").
#   MISSION_IDS / MISSION_PROMPTS — parallel arrays (RESET by this call) of
#     the missions on $FORCE_LAYER that are still due per their OWN
#     per-mission .last-run marker.
#
# Contract with the caller (the per-dept loop):
#   - _MISSIONS_DEFINED_ON_LAYER=0 → fall back to the single generic
#     "run Layer N" tick — zero regression for non-migrated depts.
#   - _MISSIONS_DEFINED_ON_LAYER=1 and MISSION_IDS empty → every mission on
#     this layer already fired today — the caller must SKIP (no tick), never
#     fall back to the generic prompt (that would re-run an already-fired
#     mission).
#   - MISSION_IDS non-empty → the caller runs ONE backup tick PER mission,
#     each with build_mission_tick_prompt naming that specific mission.
#
# Never-fatal: any python error yields no missions and
# _MISSIONS_DEFINED_ON_LAYER=0, so the caller safely falls back to the legacy
# generic tick rather than silently skipping a dept that needs one.
select_forced_layer_missions() {
    local slug="$1" workdir="$2"
    _MISSIONS_DEFINED_ON_LAYER=0
    MISSION_IDS=(); MISSION_PROMPTS=()
    local out
    out="$("$PY" - "$workdir" "$FORCE_LAYER" <<'PYEOF' 2>/dev/null || true
import sys
from datetime import datetime, timezone
sys.path.insert(0, "/home/claude/bubble-ops-loop")
from scripts.lib.dispatch_helpers import (
    select_due_missions_for_forced_layer, resolve_mission_prompt,
)
import yaml
from pathlib import Path
workdir, layer = sys.argv[1], int(sys.argv[2])
repo = Path(workdir)
dept_yaml = repo / "dept.yaml"
defined = False
if dept_yaml.exists():
    try:
        dept = yaml.safe_load(dept_yaml.read_text(encoding="utf-8")) or {}
        missions = dept.get("recurring_missions") or []
        defined = any(int(m.get("layer", 0)) == layer for m in missions)
    except Exception:
        defined = False
print("DEFINED\t" + ("1" if defined else "0"))
if defined:
    due = select_due_missions_for_forced_layer(repo, layer, now_utc=datetime.now(timezone.utc))
    for m in due:
        prompt_path = resolve_mission_prompt(repo, m)
        print(f"{m.get('id', '')}\t{prompt_path}")
PYEOF
)"
    [[ -n "$out" ]] || return 0
    local first=1 line _mid _mprompt
    while IFS= read -r line; do
        if [[ "$first" == "1" ]]; then
            first=0
            [[ "$line" == "DEFINED"$'\t'"1" ]] && _MISSIONS_DEFINED_ON_LAYER=1
            continue
        fi
        IFS=$'\t' read -r _mid _mprompt <<<"$line"
        [[ -n "$_mid" ]] || continue
        MISSION_IDS+=("$_mid")
        MISSION_PROMPTS+=("$_mprompt")
    done <<<"$out"
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
# fresh loop), ping {{OPERATOR}} on Telegram so he knows a primary loop was down and
# the net caught it. Rides the SAME shared send path as WS3's per-layer pings:
# the promoted, dept-agnostic `scripts/lib/notify.py` TelegramBackend (direct
# api.telegram.org POST, reads TELEGRAM_BOT_TOKEN from env, per-channel failure
# isolation). We deliberately do NOT use WS3's `notify_layer_fired` because its
# message shape is layer-specific (`🔁 <dept> · L<N> fired`); this is a distinct
# safety-net event (`🛟`).
#
# Recipient: {{OPERATOR}}'s Telegram chat_id. tony/cgp have no config.yaml (only maya
# does), so we can't lean on per-dept `resolve_recipients`; the backup net pings
# ONE human ({{OPERATOR}}) regardless of dept. chat_id is overridable via
# BUBBLE_BACKUP_TELEGRAM_CHAT_ID (default = {{OPERATOR}}, confirmed msg 1946).
#
# TELEGRAM_BOT_TOKEN must be in the environment — the caller sources the dept
# envfile (which carries it) before invoking this. Never fatal: a notify failure
# must not abort the safety net (mirrors emit_event's posture).
BACKUP_CHAT_ID="${BUBBLE_BACKUP_TELEGRAM_CHAT_ID:-${BUBBLE_OPERATOR_CHAT_ID:-}}"

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
    # Mission-granular mode (card #518) additionally names the SPECIFIC mission
    # that fired (CURRENT_MISSION_ID, set by the per-dept loop before calling
    # do_one_tick) — so a human reading the ping knows WHICH same-layer mission
    # ran, not just that "L<N> fired" (ambiguous when a layer has 2+ missions).
    local what="backup tick"
    [[ -n "$FORCE_LAYER" ]] && what="L${FORCE_LAYER} floor tick"
    [[ -n "${CURRENT_MISSION_ID:-}" ]] && what="${what} (mission=${CURRENT_MISSION_ID})"
    local msg="🛟 ${what} fired for ${slug} (primary loop stale ${age_h}) — exit=${exit_code}"
    # Append the tick's work summary so {{OPERATOR}} sees WHAT the layer mission did,
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

# ── Floor-fired L1/L4 brief delivery (board #521, cause 3) ──────────────────
# The floor prompt (build_tick_prompt, FORCE_LAYER branch) explicitly tells
# the tick "Do NOT send your own Telegram message — the backup wrapper
# relays your final message" — so when the SAFETY NET (not the live loop)
# fires L1 or L4, the dept's own CLAUDE.md STEP F (which would normally call
# tools/notify_layer.py) never runs. Only the generic 🛟 "backup tick fired"
# line went out (notify_backup_fired above), carrying the tick's raw CHAT
# reply (LAST_TICK_SUMMARY, capped ~1500 chars) — NOT the actual brief
# artifact the tick just wrote to disk. Joris got the safety-net ping but
# never the L1/L4 brief itself.
#
# Fix: after a successful (exit=0) floor tick for layer 1 or 4, reuse the
# SAME canonical send path every dept already uses
# (tools/notify_layer.py -> scripts/lib/loop_notify.notify_layer_fired) to
# deliver the real brief body, config-driven exactly like the live-loop path
# (brief_artifacts in config.yaml / dept.yaml, safe fallback to the summary
# heading when nothing is configured/found — no dept regresses). This is a
# SEPARATE message from the 🛟 safety-net ping (which stays as-is, so the
# "loop was down" signal is never lost); it is skipped for a DEGRADED L4
# (no fresh brief was written — the dept only wrote a carried-over debrief,
# and it already gets a real dispatch on the LIVE loop's next good tick).
notify_floor_layer_brief() {
    local slug="$1" workdir="$2" envfile="$3" layer="$4" exit_code="$5"
    [[ "$layer" == "1" || "$layer" == "4" ]] || return 0
    [[ "$exit_code" == "0" ]] || { log "$slug: skip floor L${layer} brief relay (tick exit=${exit_code})"; return 0; }

    local today; today="$(date -u +%Y-%m-%d)"
    local summary_path="${workdir}/outputs/${today}/${layer}/summary.md"

    # Test seam: mirrors BUBBLE_BACKUP_NOTIFY_CMD's shape so the bash harness
    # can assert this fired without a real notify_layer.py subprocess.
    if [[ -n "${BUBBLE_BACKUP_BRIEF_NOTIFY_CMD:-}" ]]; then
        "${BUBBLE_BACKUP_BRIEF_NOTIFY_CMD}" "$slug" "$layer" "$summary_path" \
            || log "$slug: warn — floor brief notify stub failed"
        return 0
    fi

    if [[ ! -f "$workdir/tools/notify_layer.py" ]]; then
        log "$slug: skip floor L${layer} brief relay — tools/notify_layer.py not vendored"
        return 0
    fi
    (
        set -a
        # shellcheck disable=SC1090
        [[ -f "$envfile" ]] && . "$envfile"
        set +a
        cd "$workdir" || exit 1
        "$PY" tools/notify_layer.py fired --layer "$layer" --summary "$summary_path"
    ) 2>&1 | while IFS= read -r _l; do log "$slug: [floor-brief] $_l"; done
}

# ── Auto-restart dead DEPARTMENTS (Rick 2026-06-19, {{OPERATOR}}-approved) ──────────
# When the backup tick could NOT revive a dead dept (tick exited non-zero — the
# loop is down AND the safety net couldn't run a layer), restart the dept's
# systemd unit. EXACT {{OPERATOR}}-approved scope (enforced by scripts/lib/auto_restart.py):
#   • ONLY departments (tony, ben, maya, accountant); NEVER concierges
#     (morty, claudette) — {{OPERATOR}} msg 4636. The concierge exclusion is a GUARD in
#     the decision module (refuses anything not a dept), not just this default.
#   • Guardrail: max 3 restarts/rolling-hour/dept; the 4th ESCALATES to Telegram.
#   • DEFAULT-ON for the 4 depts; opt-out per dept via
#     BUBBLE_AUTORESTART_OPTOUT="<slug> <slug>"; the whole feature can be turned
#     off with BUBBLE_AUTORESTART=0.
# The restart history lives in its own state file so the guardrail survives across
# floor invocations. systemctl is the same overridable $SYSTEMCTL the eligibility
# gate uses (test harness stubs it). Never fatal — a restart/escalation failure
# must not abort the floor.
AUTORESTART_ENABLED="${BUBBLE_AUTORESTART:-1}"
AUTORESTART_STATE="${BUBBLE_AUTORESTART_STATE:-${REPO_ROOT}/state/auto-restart.jsonl}"
AUTORESTART_MAX_PER_HOUR="${BUBBLE_AUTORESTART_MAX_PER_HOUR:-3}"
AUTORESTART_OPTOUT="${BUBBLE_AUTORESTART_OPTOUT:-}"

# maybe_auto_restart <slug> — called ONLY when a backup tick failed to revive the
# dept (tick_exit != 0). Consults the pure decision (concierge guard + guardrail),
# then on ACT_RESTART runs `systemctl restart ops-loop-<slug>.service` and records
# the restart; on ACT_ESCALATE pings a human; on any refuse it just logs. The
# Telegram env must already be sourced by the caller (same posture as notify).
maybe_auto_restart() {
    local slug="$1"
    [[ "$AUTORESTART_ENABLED" == "1" ]] || { log "$slug: auto-restart disabled (BUBBLE_AUTORESTART=0)"; return 0; }

    # opt-out check (space-separated slug list)
    local opted_out=0 o
    for o in $AUTORESTART_OPTOUT; do [[ "$o" == "$slug" ]] && opted_out=1; done

    # Pure decision (concierge guard + per-dept rolling-hour guardrail).
    local decision action reason
    decision="$("$PY" - "$AUTORESTART_STATE" "$slug" "$AUTORESTART_MAX_PER_HOUR" "$opted_out" 2>/dev/null <<'PYEOF'
import sys, time
sys.path.insert(0, "/home/claude/bubble-ops-loop")
from scripts.lib.auto_restart import decide_restart, read_restart_events
state, slug, maxph, opted = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4] == "1"
hist = read_restart_events(state)
d = decide_restart(slug, hist, time.time(), max_per_hour=maxph, opted_out=opted)
print(d["action"] + "\t" + d["reason"])
PYEOF
)"
    # Fail-closed: a python/decision error yields an empty result → refuse (never
    # restart on an unparseable decision).
    [[ -n "$decision" ]] || decision=$'refuse-not-dept\tdecision error (fail-closed)'
    action="$(cut -f1 <<<"$decision")"
    reason="$(cut -f2 <<<"$decision")"

    case "$action" in
        restart)
            log "$slug: AUTO-RESTART — $reason"
            if "$SYSTEMCTL" restart "ops-loop-${slug}.service" ; then
                _record_restart_event "$slug" "restart" "$reason"
                # Surface the restart on Telegram (best-effort) so a human knows
                # the dept was revived by force, not by the gentle backup tick.
                notify_autorestart "$slug" "restarted" "$reason"
            else
                log "$slug: AUTO-RESTART FAILED — systemctl restart returned non-zero"
                _record_restart_event "$slug" "restart-failed" "$reason"
                notify_autorestart "$slug" "restart-FAILED" "systemctl restart ops-loop-${slug}.service failed — needs a human"
            fi
            ;;
        escalate)
            log "$slug: AUTO-RESTART GUARDRAIL TRIPPED — $reason"
            _record_restart_event "$slug" "escalate" "$reason"
            notify_autorestart "$slug" "GUARDRAIL — human needed" "$reason"
            ;;
        refuse-concierge)
            log "$slug: auto-restart REFUSED (concierge — safety invariant): $reason"
            ;;
        *)
            log "$slug: auto-restart not applied ($action): $reason"
            ;;
    esac
}

_record_restart_event() {
    local slug="$1" action="$2" reason="$3"
    "$PY" - "$AUTORESTART_STATE" "$slug" "$action" "$reason" <<'PYEOF' 2>/dev/null || log "$slug: warn — could not record restart event"
import sys
sys.path.insert(0, "/home/claude/bubble-ops-loop")
from scripts.lib.auto_restart import append_restart_event, format_restart_event
state, slug, action, reason = sys.argv[1:5]
append_restart_event(state, format_restart_event(slug, action, reason))
PYEOF
}

# notify_autorestart <slug> <subject-tail> <reason> — Telegram ping for an
# auto-restart / escalation. Reuses the same TelegramBackend + chat_id as the
# backup-fired ping (caller has the env sourced). Never fatal.
notify_autorestart() {
    local slug="$1" what="$2" reason="$3"
    local subject="🔁 auto-restart [${slug}] — ${what}"
    if [[ -n "${BUBBLE_BACKUP_NOTIFY_CMD:-}" ]]; then
        "${BUBBLE_BACKUP_NOTIFY_CMD}" "$slug" "$BACKUP_CHAT_ID" "${subject}"$'\n'"${reason}" \
            || log "$slug: warn — auto-restart notify stub failed"
        return 0
    fi
    "$PY" - "$BACKUP_CHAT_ID" "$subject" "$reason" <<'PYEOF' || log "$slug: warn — could not send auto-restart Telegram ping"
import sys
sys.path.insert(0, "/home/claude/bubble-ops-loop")
from scripts.lib.notify import TelegramBackend, NotificationPayload
chat_id, subject = sys.argv[1], sys.argv[2]
body = sys.argv[3] if len(sys.argv) > 3 else ""
receipt = TelegramBackend({}).send(
    NotificationPayload(subject=subject, markdown_body=body,
                        metadata={"kind": "auto_restart"}),
    chat_id,
)
if not receipt.success:
    sys.stderr.write(f"auto-restart ping not delivered: {receipt.error}\n")
    sys.exit(1)
PYEOF
}

# ── Tick prompt ──────────────────────────────────────────────────────────────
# Two shapes. In layer-floor mode the prompt FORCES Layer N (no decide_dispatch);
# in generic mode it runs the dispatcher's choice. Both end with the concise
# report the wrapper relays to {{OPERATOR}} on Telegram.
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
Execute EXACTLY ONE tick, then: 1. If no /loop wake is armed, arm ONE self-paced next wake via CronCreate (CronList first; dedupe) — toward the next due layer if work remains, a longer cadence (e.g. 0 */2 * * *) if quiet but more may come, else a one-shot tomorrow 08:03 Paris (3 8 * * *). Never hardcode an hourly cron. The CronCreate prompt must be your full tick protocol (STEP A-F), never a bare slash-command like /loop-now (it delivers as a malformed inbound that can trip the deaf-watchdog). 2. Write heartbeat. 3. STOP.

Do NOT send your own Telegram message — the backup wrapper relays your final
message to {{OPERATOR}} automatically. END your turn with a concise report (max ~6
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
  1. If no /loop wake is armed: arm ONE self-paced next wake via CronCreate (run CronList first and dedupe) — toward the next due layer if work remains, a longer cadence (e.g. 0 */2 * * *) if quiet but more may come, else a one-shot tomorrow 08:03 Paris (3 8 * * *). Never hardcode an hourly cron. The CronCreate prompt must be your full tick protocol (STEP A-F), never a bare slash-command like /loop-now (it delivers as a malformed inbound that can trip the deaf-watchdog).
  2. Write heartbeat to outputs/<today>/heartbeat.log.
  3. Then STOP.

Do NOT send your own Telegram message — the backup wrapper relays your
final message to {{OPERATOR}} automatically. Instead, END your turn with a
concise report (max ~6 lines) the wrapper will forward verbatim:
  • Confirm you ran Layer ${FORCE_LAYER}.
  • What the layer mission actually DID — the concrete result/output
    (e.g. "scored 4 prospects, 1 promoted to draft"; "no new signals";
    "risk check: 2 positions flagged"). Be specific, not "ran L${FORCE_LAYER}".
  • Any gate created or subagent failure (and what it needs from {{OPERATOR}}).
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
final message to {{OPERATOR}} automatically. Instead, END your turn with a
concise report (max ~6 lines) the wrapper will forward verbatim:
  • Which layer dispatch chose (L1/L2/L3/L4 or heartbeat).
  • What the layer mission actually DID — the concrete result/output
    (e.g. "scored 4 prospects, 1 promoted to draft"; "no new signals";
    "risk check: 2 positions flagged"). Be specific, not "ran L2".
  • Any gate created or subagent failure (and what it needs from {{OPERATOR}}).
  • If decide_dispatch returned heartbeat: say so in one line and why
    (e.g. "queues empty, L1 already ran today").
PROMPT
    fi
}
# Built ONCE at startup: the legacy generic prompt ("run Layer N" / plain
# decide_dispatch). Card #518 renamed this from the bare TICK_PROMPT global
# because TICK_PROMPT is now set FRESH per mission in mission-granular mode
# (see build_mission_tick_prompt below + the per-dept loop) — GENERIC_TICK_PROMPT
# is the fallback value the loop restores it to for a non-migrated dept.
GENERIC_TICK_PROMPT="$(build_tick_prompt)"
TICK_PROMPT="$GENERIC_TICK_PROMPT"

# ── Mission-granular floor prompt (card #518) ────────────────────────────────
# Companion to build_tick_prompt's FORCE_LAYER branch, used ONLY when
# select_forced_layer_missions (below) proves the dept has migrated to
# per-mission dept.yaml::recurring_missions on this layer. Names the ONE
# mission this tick must run (mission_id + its resolved prompt path) instead
# of the generic "read layers/<N>/PROMPT.md" instruction — so a late floor
# tick can dispatch a SPECIFIC still-pending mission (e.g. market_wrapup)
# without re-running a mission that already fired earlier today (e.g.
# risk_control), both on the same layer. Idempotence is the mission's OWN
# job (its prompt's STEP 1 stamps outs/<today>/missions/<id>/.last-run,
# exactly like the live-loop mission-centric path — see resolve_mission_prompt).
build_mission_tick_prompt() {
    local layer="$1" mission_id="$2" prompt_path="$3"
    cat <<PROMPT
You are running as the daily Layer ${layer} FLOOR tick (one of four per-layer
cron units that guarantee each OODA layer fires at least once a day even if
your persistent /loop is down or parked).

MISSION-GRANULAR floor tick (card #518): this Layer ${layer} has MORE THAN
ONE recurring mission in dept.yaml. Per-mission idempotence markers
(outputs/<today>/missions/<id>/.last-run) show mission \`${mission_id}\` is
still due — run ONLY that mission now, per ${prompt_path}. Do NOT run any
other Layer ${layer} mission this tick (a sibling mission that already fired
today must stay untouched; a sibling not yet due is not yours to run either
— the next floor tick, or the live loop, will pick it up on its own schedule).

Run mission \`${mission_id}\` NOW: git pull; read ${prompt_path}; spawn its
subagent; VERIFY the output; validate; commit+push. Do NOT run
decide_dispatch. Execute EXACTLY ONE tick, then:
  1. If no /loop wake is armed: arm ONE self-paced next wake via CronCreate (run CronList first and dedupe) — toward the next due layer/mission if work remains, a longer cadence (e.g. 0 */2 * * *) if quiet but more may come, else a one-shot tomorrow 08:03 Paris (3 8 * * *). Never hardcode an hourly cron. The CronCreate prompt must be your full tick protocol (STEP A-F), never a bare slash-command like /loop-now (it delivers as a malformed inbound that can trip the deaf-watchdog).
  2. Write heartbeat to outputs/<today>/heartbeat.log.
  3. Then STOP.

Do NOT send your own Telegram message — the backup wrapper relays your
final message to {{OPERATOR}} automatically. Instead, END your turn with a
concise report (max ~6 lines) the wrapper will forward verbatim:
  • Confirm you ran mission \`${mission_id}\` (Layer ${layer}).
  • What the mission actually DID — the concrete result/output. Be specific,
    not "ran L${layer}".
  • Any gate created or subagent failure (and what it needs from {{OPERATOR}}).
  • If there was nothing for this mission to do, say so in one line and why.
PROMPT
}


# ── Wake the LIVE session via bubble-inject, if it's alive ({{OPERATOR}} msg 4045) ──
# A floor fire usually means the in-session cron lapsed, NOT that the session
# died. In that case the live --channels session is still up and can run the tick
# itself — cheaper (no new session) and keeps context. We detect "session alive"
# by a bun poller in the dept's systemd cgroup (same signal the watchdog uses),
# then drop "run your loop" into <state_dir>/inject (the bubble-inject patch
# delivers it as a message from {{OPERATOR}}). We confirm it actually ticked by watching
# the heartbeat.log mtime advance; if not (session wedged/dead), the caller falls
# back to a `claude -p` backup tick.
inject_live_loop() {
    local slug="$1"
    local svc="ops-loop-${slug}.service"
    # session alive? = a bun process in this service's cgroup.
    local main_pid; main_pid=$("$SYSTEMCTL" show "$svc" -p MainPID --value 2>/dev/null || echo 0)
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
    printf 'Resume your OODA loop (self-paced). Run your full tick now: STEP A (safe_pull) -> STEP B (read queues) -> STEP C (decide_dispatch) -> STEP D (dispatch chosen layer subagent) -> STEP E (commit+push runtime paths) -> STEP F (Telegram notify). Always write heartbeat to outputs/<today>/heartbeat.log. Then arm your OWN next wake via a single CronCreate (CronList first, dedupe): toward the next due layer if work remains, a longer cadence (e.g. 0 */2 * * *) if quiet, or a one-shot tomorrow 08:03 Paris (3 8 * * *) if all 4 layers are done. Never hardcode an hourly cron. The CronCreate prompt must be your full tick protocol (STEP A-F), never a bare slash-command like /loop-now (it delivers as a malformed inbound that can trip the deaf-watchdog).\n' >> "$inject" 2>/dev/null || return 1

    # Wait up to ~240s for the live session to tick (heartbeat mtime advances).
    # 90s was too short: the inject IS delivered but a quiet session can take a
    # couple minutes to wake + run a tick (esp. opus depts), so the floor fell back
    # to -p even when the inject would have worked ({{OPERATOR}} 2026-06-08, all 3 depts).
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
    if ! bkp_flock -n 9 "$lock"; then
        log "$slug: lock held (a tick is already running) — skipping backup"
        return 99   # sentinel: skipped (no tick ran) → caller must NOT notify
    fi

    log "$slug: running ONE backup tick (model=$MODEL budget=\$$BUDGET_USD)"
    local runlog; runlog="$(mktemp)"
    # ── WEDGE FIX (Rick 2026-06-19): --strict-mcp-config on the headless spawn ──
    # Root cause of the backup tick exiting 1 (Maya/Ben 2026-06-18, MCP-WEDGE
    # path #2): `--setting-sources user` loads the dept's user settings.json,
    # which declares the telegram --channels plugin + its MCP server. When a
    # `claude --print` child loads that, it spins up a SECOND bun poller against
    # the SAME bot token + same bot.pid as the live --channels session. The two
    # pollers collide (stale-PID SIGTERM / orphan-watchdog reparent — see
    # MCP-WEDGE-ROOTCAUSE.md §"Recommended durable fixes" Fix E), the headless
    # MCP load hangs or aborts, and the spawn returns non-zero → the safety net
    # silently never executes a layer subagent.
    #   `--strict-mcp-config` = "only use MCP servers from --mcp-config, ignore
    # all other sources". We pass NO --mcp-config, so the headless backup tick
    # loads ZERO MCP servers — no telegram plugin, no second poller, no
    # collision. We KEEP `--setting-sources user` so hooks/permissions/CLAUDE.md
    # still load; we only strip MCP. A layer subagent tick needs Bash/Edit/Read,
    # not the Telegram channel (the wrapper relays the result to {{OPERATOR}} itself).
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
            --strict-mcp-config \
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
    bkp_flock -u 9 "$lock" || true
    return $exit
}

# do_one_tick <slug> <workdir> <envfile> <age> <reason>: run ONE headless
# backup tick using the CURRENT $TICK_PROMPT (either GENERIC_TICK_PROMPT or a
# mission-specific prompt from build_mission_tick_prompt — the caller sets
# TICK_PROMPT immediately before calling this) and handle its outcome exactly
# as the pre-#518 inline tail did: lock-held skip, event log, external
# heartbeat, Telegram fired-ping + brief relay, auto-restart-on-failure.
# Extracted from the per-dept loop so mission-granular mode can call it once
# per due mission without duplicating this bookkeeping N times.
do_one_tick() {
    local slug="$1" workdir="$2" envfile="$3" age="$4" reason="$5"
    local tick_exit=0
    run_backup_tick "$slug" "$workdir" "$envfile" || tick_exit=$?
    if [[ "$tick_exit" == "99" ]]; then
        # Lock held by a concurrent (live) tick → no backup tick ran. Record a
        # skip and do NOT ping — nothing fired.
        emit_event "$slug" "skip" "lock held (concurrent tick) — $reason" "$age"
        return 0
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
    # across depts. Lock-held returns above → no ping on a fresh/healthy loop.
    (
        set -a
        # shellcheck disable=SC1090
        [[ -f "$envfile" ]] && . "$envfile"
        set +a
        notify_backup_fired "$slug" "$age" "$tick_exit" "$LAST_TICK_SUMMARY"
        # Board #521 cause 3: on a floor-fired L1/L4 (not degraded), also
        # relay the REAL brief body — a SEPARATE message from the 🛟 ping
        # above, via the same canonical send path every dept's live loop
        # uses. Never blocks/fails the safety net (own subshell + own log
        # lines only).
        if [[ -n "$FORCE_LAYER" && "${DEGRADED_L4:-0}" != "1" ]]; then
            notify_floor_layer_brief "$slug" "$workdir" "$envfile" "$FORCE_LAYER" "$tick_exit"
        fi
        # Auto-restart the dead DEPT only when the backup tick FAILED to revive it
        # (tick_exit != 0 = loop down AND the safety net couldn't run a layer).
        # The pure decision enforces the concierge guard + the 3/hour guardrail.
        # A successful backup tick (exit 0) revived the dept's work for this cycle
        # → no restart needed. Runs in the same env-sourced subshell so the
        # escalation Telegram ping has the token.
        if [[ "$tick_exit" != "0" ]]; then
            maybe_auto_restart "$slug"
        fi
    )
}

OVERALL=0
mapfile -t DEPTS < <(discover_depts)
if [[ -n "${BUBBLE_BACKUP_DEPTS:-}" ]]; then
    log "depts (override): ${DEPTS[*]:-<none>}"
else
    log "depts (auto-discovered from ${AGENTS_ROOT}/bubble-ops-*): ${DEPTS[*]:-<none>}"
fi

# ── CEO directive dispatch (runs once per tick, BEFORE the per-dept loop) ─────
# Deliver {{OPERATOR}}-approved directives from the manager dept's outbound queue into
# the target children's queues/management/ inboxes. This is the ONLY actor that
# crosses repo boundaries (the manager is isolated to its own repo). Pure
# mechanical relay (NOT claude -p — template Ban #2). Gated: only directives with
# approved_by=operator AND status=approved ship. Idempotent + never-fatal so it is
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
    # Forced-layer BACKUP-OFFSET + PREREQUISITE gate ({{OPERATOR}} msgs 3904 + 3911,
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
# #454 FIX: this is a READ-ONLY eligibility probe (decides whether to wake
# the live session) — it must NOT materialize/pre-stamp mission markers as a
# side effect. materialize=True (the old default) pre-stamped shim-resolved
# missions' (e.g. Ben's data_update) per-mission .last-run here, ~9s before
# inject_live_loop woke the real session; the real session's own
# build_dispatch_ctx call then saw that stamp as a PRIOR-tick marker and
# silently vetoed the real dispatch (l1_fired=True -> heartbeat). See
# board #454 and the docstring on build_dispatch_ctx's `materialize` kwarg.
ctx = build_dispatch_ctx(workdir, now_utc=datetime.now(timezone.utc), materialize=False)
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
            # Tony with NO export and a false "dept silent" reading ({{OPERATOR}} 2026-06-07,
            # Ben/Maya 06-06) — run a DEGRADED L4: a short, honest "loop was down,
            # earlier layers did not run" debrief from last-known state. Tony always
            # gets SOMETHING; the dept never disappears from the morning brief.
            log "$slug: DEGRADED backup L4 — L1/L2/L3 not all fired today + loop stale; running a degraded debrief"
            emit_event "$slug" "degraded" "backup L4 degraded (L1/2/3 pending, loop stale)" "$age"
            DEGRADED_L4=1
        elif [[ "$layer_ok" == "ERR" && "$FORCE_LAYER" == "4" ]]; then
            # Board #529: the probe itself errored (e.g. a missing python dep
            # for dispatch_helpers.py) — for L4 specifically, that means we have
            # NO evidence L1/2/3 actually fired today. Failing OPEN here (as if
            # "OK") would let a genuinely-degraded L4 look like a normal one and
            # relay a brief with no fresh content behind it — the exact defect
            # board #529 caught (L5 in tests/test_loop_backup.sh). WHETHER to
            # tick still fails open (the dept must never go silent — run the
            # tick below as usual); only the "is this brief fresh enough to
            # relay" signal fails SAFE (degraded) when we can't verify it.
            # EARLY-vs-PREREQ for L1/2/3 doesn't carry this risk (no brief
            # relay is gated on them), so ERR stays fail-open there.
            log "$slug: DEGRADED backup L4 — eligibility probe errored (unknown L1/2/3 status); failing safe to degraded"
            emit_event "$slug" "degraded" "backup L4 degraded (eligibility probe error, treated as unverified prereqs)" "$age"
            DEGRADED_L4=1
        fi
        # "OK" (prerequisites verified met; tick proceeds as a normal floor
        # tick), or "ERR" on a non-L4 layer (fail-open: a check error runs the
        # tick as before — no brief-relay invariant depends on L1/2/3's PREREQ).
    fi

    # ── Mission-granular enumeration (card #518) ─────────────────────────────
    # Forced-layer mode ONLY, and only when we're actually about to run a
    # (non-degraded) backup tick — a degraded L4 needs its own single carried-
    # over debrief, not a per-mission split. Called DIRECTLY (never inside a
    # `< <(...)` process substitution) so its _MISSIONS_DEFINED_ON_LAYER /
    # MISSION_IDS / MISSION_PROMPTS assignments land in THIS shell, not a
    # subshell that vanishes on return — see the function's own docstring.
    MISSION_IDS=(); MISSION_PROMPTS=(); _MISSIONS_DEFINED_ON_LAYER=0
    if [[ -n "$FORCE_LAYER" && "${DEGRADED_L4:-0}" != "1" ]]; then
        select_forced_layer_missions "$slug" "$workdir"
        if [[ "$_MISSIONS_DEFINED_ON_LAYER" == "1" && "${#MISSION_IDS[@]}" == "0" ]]; then
            # dept.yaml declares mission(s) on this layer but NONE are due —
            # every mission already has a fresh per-mission marker today.
            # Do NOT fall back to the generic "run Layer N" prompt (that
            # would re-run an already-fired mission); just skip cleanly.
            log "$slug: skip — all Layer $FORCE_LAYER missions already fired today (per-mission markers present)"
            emit_event "$slug" "skip" "all L$FORCE_LAYER missions already fired today (per-mission)" "$age"
            continue
        fi
    fi

    if [[ "$DRY_RUN" == "1" ]]; then
        # Record the decision even in dry-run so a smoke test of the schedule
        # shows up in the cockpit, without spending a real tick.
        emit_event "$slug" "skip" "DRY_RUN — would run a backup tick ($reason)" "$age"
        continue
    fi

    # Prefer waking the LIVE session ONCE per dept (cheaper, keeps context) when
    # it's alive and this isn't a degraded-L4 (which needs its own headless
    # prompt). This is a per-DEPT decision, not per-mission — the live session's
    # own /loop protocol (STEP C, mission-centric per scaffold.py's canonical
    # dispatch) will pick up every pending mission itself once woken, so we must
    # NOT also spawn N headless mission ticks on top of it.
    if [[ "${DEGRADED_L4:-0}" != "1" ]] && inject_live_loop "$slug"; then
        emit_event "$slug" "run" "live-loop woken via inject — $reason" "$age" 0
        continue
    fi

    if [[ "${#MISSION_IDS[@]}" -gt 0 ]]; then
        # Mission-granular: one HEADLESS backup tick PER due mission, each with
        # its own mission-specific prompt (build_mission_tick_prompt) so the
        # dept runs EXACTLY the pending mission — never a sibling that already
        # fired, never the generic monolithic layer prompt.
        for _mi in "${!MISSION_IDS[@]}"; do
            TICK_PROMPT="$(build_mission_tick_prompt "$FORCE_LAYER" "${MISSION_IDS[$_mi]}" "${MISSION_PROMPTS[$_mi]}")"
            CURRENT_MISSION_ID="${MISSION_IDS[$_mi]}"
            do_one_tick "$slug" "$workdir" "$envfile" "$age" "$reason (mission=${MISSION_IDS[$_mi]})"
        done
    else
        # Legacy path: no mission-granular data for this layer (either generic
        # mode, or a forced layer whose dept.yaml has no recurring_missions on
        # it) — unchanged single generic tick, using the script-global
        # GENERIC_TICK_PROMPT built once at startup by build_tick_prompt.
        TICK_PROMPT="$GENERIC_TICK_PROMPT"
        CURRENT_MISSION_ID=""
        do_one_tick "$slug" "$workdir" "$envfile" "$age" "$reason"
    fi
done

log "done (mode=${MODE_LABEL} depts=${DEPTS[*]:-<none>})"
exit $OVERALL
