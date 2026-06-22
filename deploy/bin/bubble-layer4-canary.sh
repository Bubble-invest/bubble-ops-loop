#!/usr/bin/env bash
# bubble-layer4-canary.sh — verify a dept fixture produced Layer-4 outputs at the
# scheduled hour (e.g. 22:00 UTC) tonight and ping the operator on Telegram with
# the verdict. Idempotent (writes log only).
#
# CONFIG (env):
#   BUBBLE_FIXTURE_AGENT     agent dir under /home/claude/agents to check
#                            (default: fixture)
#   BUBBLE_OPERATOR_CHAT_ID  Telegram chat_id to notify (default: empty → skip TG)
#   TELEGRAM_BOT_TOKEN       sourced from /run/claude-agent/env at runtime
set -uo pipefail

FIXTURE_AGENT="${BUBBLE_FIXTURE_AGENT:-fixture}"
OPERATOR_CHAT_ID="${BUBBLE_OPERATOR_CHAT_ID:-}"

LOG=/var/log/bubble-security/layer4-canary-$(date -u +%Y-%m-%d).log
mkdir -p /var/log/bubble-security
{
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) layer4-canary check ==="

TODAY=$(date -u +%Y-%m-%d)
FIX=/home/claude/agents/$FIXTURE_AGENT
L4_DIR=$FIX/outputs/$TODAY/4

if [ ! -d "$L4_DIR" ]; then
  echo "VERDICT: ❌ MISSING — $L4_DIR does not exist (fixture did NOT run Layer 4 today)"
  STATUS="❌ MISSING — $FIXTURE_AGENT/outputs/$TODAY/4/ does not exist. Layer 4 did NOT fire at the scheduled hour."
elif [ ! -f "$L4_DIR/.last-run" ]; then
  echo "VERDICT: ⚠️ PARTIAL — $L4_DIR exists but no .last-run marker"
  STATUS="⚠️ PARTIAL — $L4_DIR exists but no .last-run marker. Dispatch fired but Layer-4 subagent didn t complete."
elif [ ! -f "$L4_DIR/risk-brief.md" ] || [ ! -f "$L4_DIR/risk-kpis.yaml" ]; then
  echo "VERDICT: ⚠️ INCOMPLETE — $L4_DIR exists, .last-run present, but missing risk-brief or risk-kpis"
  STATUS="⚠️ INCOMPLETE — Layer 4 ran but outputs incomplete (missing risk-brief or risk-kpis)."
else
  KPIS=$(head -20 "$L4_DIR/risk-kpis.yaml" | tr -d "\\n" | head -c 200)
  echo "VERDICT: ✅ FIRED — $L4_DIR has .last-run, risk-brief.md ($(wc -l <"$L4_DIR/risk-brief.md") lines), risk-kpis.yaml ($(wc -l <"$L4_DIR/risk-kpis.yaml") lines)"
  STATUS="✅ FIRED — Layer 4 produced outputs at $TODAY in $L4_DIR. risk-brief.md=$(wc -l <"$L4_DIR/risk-brief.md") lines, risk-kpis.yaml=$(wc -l <"$L4_DIR/risk-kpis.yaml") lines."
fi
echo "STATUS: $STATUS"

# Send Telegram message via the operator bot (TELEGRAM_BOT_TOKEN from runtime env).
if [ -z "$OPERATOR_CHAT_ID" ]; then
  echo "TELEGRAM: BUBBLE_OPERATOR_CHAT_ID not set, skip"
elif [ -r /run/claude-agent/env ]; then
  source /run/claude-agent/env 2>/dev/null
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    MSG="🎯 Layer-4 canary check for $FIXTURE_AGENT, $TODAY:%0A%0A$STATUS"
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${OPERATOR_CHAT_ID}" \
      --data-urlencode "text=${MSG}" > /dev/null 2>&1 \
      && echo "TELEGRAM: sent" || echo "TELEGRAM: failed (curl error)"
  else
    echo "TELEGRAM: TELEGRAM_BOT_TOKEN not in env, skip"
  fi
else
  echo "TELEGRAM: /run/claude-agent/env not readable, skip"
fi
} | sudo tee -a "$LOG" > /dev/null
echo "Check complete. Log at $LOG."
