#!/usr/bin/env bash
# =============================================================================
# fleet-drift-report.sh — per-dept clone drift report (HEAD-vs-origin/main
# behind-count + dirty-file-count). Deploy: /home/claude/scripts/fleet-drift-report.sh
# (claude:claude 0755), via install-fleet-drift-report.sh. Run via
# fleet-drift-report.timer.
#
# TWO outputs (agent-native-infra doctrine: silence must be impossible to
# mistake for success, AND dept agents must be able to know THEMSELVES when
# they're stale — not just the human operator):
#   (a) one Telegram line to the operator, only when something IS drifted
#       (quiet-by-default, like the other freshness watchdogs).
#   (b) a machine-readable JSON state file at STATE_FILE (default
#       /run/bubble-fleet/drift.json), written EVERY run (fresh or not), so a
#       dept's own loop-start check can read it and know it's running stale
#       code without waiting on a human to relay a Telegram message.
#
# Dept discovery: auto-glob $AGENTS_ROOT/bubble-ops-* (same convention as
# bubble-deploy.sh / sync-local-dept-clones.sh) — NO hardcoded dept list.
#
# Config (env, no hardcoded company facts):
#   BUBBLE_OPERATOR_CHAT_ID     Telegram chat_id to alert (default: empty -> skip TG)
#   BUBBLE_FLEET_AGENTS_ROOT    base dir holding bubble-ops-<slug> clones
#                                (default /home/claude/agents)
#   BUBBLE_FLEET_STATE_FILE     JSON state file path (default /run/bubble-fleet/drift.json)
#   BUBBLE_FLEET_BEHIND_ALERT_THRESHOLD  commits-behind that counts as "drifted"
#                                          for the Telegram line (default 1)
# =============================================================================
set -uo pipefail

ENV_FILE=/run/claude-agent/env
AGENTS_ROOT="${BUBBLE_FLEET_AGENTS_ROOT:-/home/claude/agents}"
STATE_FILE="${BUBBLE_FLEET_STATE_FILE:-/run/bubble-fleet/drift.json}"
BEHIND_ALERT_THRESHOLD="${BUBBLE_FLEET_BEHIND_ALERT_THRESHOLD:-1}"
LOG_TAG="fleet-drift-report"

log() { logger -t "$LOG_TAG" "$*" 2>/dev/null; echo "[$LOG_TAG] $*" >&2; }

g() { git -C "$1" "${@:2}" 2>/dev/null; }

now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true

json_entries=()
drifted_lines=()

for dd in "$AGENTS_ROOT"/bubble-ops-*; do
    [[ -d "$dd" ]] || continue
    [[ -L "$dd" ]] && continue   # skip symlink aliases (e.g. bubble-ops-fixture -> fixture)
    [[ -d "$dd/.git" ]] || continue
    slug="$(basename "$dd")"; slug="${slug#bubble-ops-}"

    g "$dd" config --global --get-all safe.directory 2>/dev/null | grep -qx "$dd" \
        || g "$dd" config --global --add safe.directory "$dd" >/dev/null 2>&1 || true

    if ! g "$dd" fetch origin main --quiet; then
        log "WARN fetch failed for $slug — skipping this tick, not zeroing its last-known state"
        continue
    fi

    behind="$(g "$dd" rev-list --count HEAD..origin/main 2>/dev/null || echo -1)"
    ahead="$(g "$dd" rev-list --count origin/main..HEAD 2>/dev/null || echo -1)"
    dirty="$(g "$dd" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
    branch="$(g "$dd" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    head_sha="$(g "$dd" rev-parse --short HEAD 2>/dev/null || echo '?')"

    json_entries+=("$(printf '{"slug":"%s","branch":"%s","head":"%s","behind":%s,"ahead":%s,"dirty_files":%s}' \
        "$slug" "$branch" "$head_sha" "${behind:-0}" "${ahead:-0}" "${dirty:-0}")")

    if [[ "${behind:-0}" =~ ^[0-9]+$ ]] && (( behind >= BEHIND_ALERT_THRESHOLD )); then
        drifted_lines+=("${slug}: ${behind} behind, ${dirty} dirty (branch=${branch})")
    fi
done

# (b) machine-readable state — written every run, fresh or not.
{
    printf '{\n  "generated_at": "%s",\n  "agents_root": "%s",\n  "depts": [\n' "$now_iso" "$AGENTS_ROOT"
    n=${#json_entries[@]}
    for i in "${!json_entries[@]}"; do
        sep=","
        (( i == n - 1 )) && sep=""
        printf '    %s%s\n' "${json_entries[$i]}" "$sep"
    done
    printf '  ]\n}\n'
} > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
chmod 0644 "$STATE_FILE" 2>/dev/null || true
log "wrote state for ${#json_entries[@]} depts to $STATE_FILE"

# (a) Telegram line — only when something is actually drifted (quiet-by-default).
if [[ "${#drifted_lines[@]}" -eq 0 ]]; then
    log "OK — no dept behind threshold (${BEHIND_ALERT_THRESHOLD}); quiet."
    exit 0
fi

log "DRIFT — ${#drifted_lines[@]} dept(s) behind threshold"

BOT_TOKEN=""
[[ -r "$ENV_FILE" ]] && BOT_TOKEN="$(awk -F= '/^TELEGRAM_BOT_TOKEN=/{print $2; exit}' "$ENV_FILE" 2>/dev/null)"
OPERATOR_CHAT_ID="${BUBBLE_OPERATOR_CHAT_ID:-}"

if [[ -z "$BOT_TOKEN" || -z "$OPERATOR_CHAT_ID" ]]; then
    log "no TELEGRAM_BOT_TOKEN or BUBBLE_OPERATOR_CHAT_ID configured — drift alert NOT sent"
    exit 0
fi

MSG="🟡 fleet drift: $(IFS='; '; echo "${drifted_lines[*]}")"
curl -s --max-time 10 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
     -d chat_id="$OPERATOR_CHAT_ID" --data-urlencode text="$MSG" >/dev/null 2>&1 \
    && log "drift alert sent to chat_id=$OPERATOR_CHAT_ID" \
    || log "warn — telegram alert curl failed (non-fatal)"

exit 0
