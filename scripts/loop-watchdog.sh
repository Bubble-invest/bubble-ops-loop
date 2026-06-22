#!/usr/bin/env bash
# loop-watchdog.sh — escalate to Telegram if ops-loop-fixture heartbeat stale.
#
# QA-AUDIT-J2 finding #4 + cross-cutting #3: only 1 heartbeat in 11 min was
# observed during audit; need a watchdog that escalates when the loop
# silently dies.
#
# Cadence: timer runs every 40 min on Morty (= 2× the 20-min /loop tick).
# Threshold: heartbeat file mtime > 40 min ago → alert.
#
# Design choices:
#   - We use the fixture's OWN Telegram bot (FIXTURE_TELEGRAM_BOT_TOKEN),
#     not morty's. Keeps blast radius scoped to the fixture dept.
#   - We do NOT alert when the heartbeat file is missing entirely. That can
#     happen legitimately right after a reboot before the first tick lands.
#     If a full day passes with no heartbeat, the daily backup cron will
#     catch the missing outputs directory.
#   - We do NOT touch ops-loop-fixture.service — alerting only, no
#     auto-restart. {{OPERATOR}} decides whether to intervene.
#   - We re-alert every 40 min while stale (idempotent: same message). This
#     is intentional pressure to act, not annoyance: Telegram dedupes
#     identical recent messages visually in the UI.

set -euo pipefail

HEARTBEAT="/home/claude/agents/fixture/outputs/$(date -u +%Y-%m-%d)/heartbeat.log"
STALE_THRESHOLD_SEC=$((40 * 60))  # 2× the 20-min /loop cadence
CHAT_ID="${BUBBLE_OPERATOR_CHAT_ID:-}"   # operator ({{OPERATOR}})

if [ ! -f "$HEARTBEAT" ]; then
  # No heartbeat file yet today. Two cases:
  # (a) Just past midnight UTC; loop hasn't ticked yet → skip alert.
  # (b) Loop is dead and has been all day → caught by daily backup audit.
  # Either way, this watchdog stays quiet (don't be the boy who cried wolf).
  exit 0
fi

LAST_MTIME=$(stat -c %Y "$HEARTBEAT")
NOW=$(date +%s)
AGE=$((NOW - LAST_MTIME))

if [ "$AGE" -le "$STALE_THRESHOLD_SEC" ]; then
  # Healthy — log and exit. Log line lets `journalctl -u
  # ops-loop-watchdog.service` show recent OK ticks.
  echo "ok: heartbeat age=${AGE}s (threshold=${STALE_THRESHOLD_SEC}s)"
  exit 0
fi

# Stale — alert via fixture Telegram bot.
ENV_FILE=/run/claude-agent-fixture/env
if [ ! -r "$ENV_FILE" ]; then
  echo "warn: $ENV_FILE not readable (perms?). Watchdog cannot alert." >&2
  exit 2
fi

# shellcheck disable=SC1090
. "$ENV_FILE"

# The fixture's env file uses TELEGRAM_BOT_TOKEN (not FIXTURE_TELEGRAM_BOT_TOKEN
# which is the SOPS-store key). When sourced from this file, the value here
# is the FIXTURE bot's token, NOT morty's — the /run/claude-agent-fixture/env
# is dept-scoped (see STEP-7-DEPLOYMENT-RESULTS.md §"TELEGRAM_STATE_DIR
# propagation").
TOKEN_VAL="${TELEGRAM_BOT_TOKEN:-${FIXTURE_TELEGRAM_BOT_TOKEN:-}}"
if [ -z "$TOKEN_VAL" ]; then
  echo "warn: neither TELEGRAM_BOT_TOKEN nor FIXTURE_TELEGRAM_BOT_TOKEN set in $ENV_FILE." >&2
  exit 3
fi

LAST_HUMAN=$(date -u -d "@${LAST_MTIME}" -Iseconds)
MSG=$(printf 'WARN ops-loop-fixture watchdog: heartbeat stale.\nAge: %ss (threshold: %ss)\nLast tick: %s\nCheck: sudo journalctl -u ops-loop-fixture.service -n 50' \
  "$AGE" "$STALE_THRESHOLD_SEC" "$LAST_HUMAN")

# Best-effort POST; do not fail the unit if Telegram is down.
HTTP_CODE=$(curl -s -o /tmp/_wd_resp -w '%{http_code}' \
  "https://api.telegram.org/bot${TOKEN_VAL}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${MSG}" || echo "000")

if [ "$HTTP_CODE" = "200" ]; then
  echo "alert sent: heartbeat age=${AGE}s, telegram=200"
  rm -f /tmp/_wd_resp
  exit 0
else
  echo "alert FAILED: heartbeat age=${AGE}s, telegram=${HTTP_CODE}" >&2
  echo "telegram response:" >&2
  cat /tmp/_wd_resp >&2 || true
  rm -f /tmp/_wd_resp
  exit 4
fi
