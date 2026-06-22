#!/usr/bin/env bash
# secrets-tmp-sweep.sh — alert if any plaintext secret file appears in /tmp, /var/tmp,
# or /home/claude. Reports FILE NAMES + PERMS ONLY (never contents — that would re-leak).
# Source-leak detector for the recurring sops/cred-broker /tmp plaintext class
# (incident 2026-05-29). Pairs with the script-level umask+trap fixes.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/run/claude-agent/env}"
LOG_DIR="${LOG_DIR:-/var/log/bubble-security}"
DATE_STAMP=$(date -u +"%Y-%m-%d")
TS_HUMAN=$(date -u +"%Y-%m-%d %H:%M UTC")
LOG_FILE="${LOG_DIR}/secrets-tmp-sweep-${DATE_STAMP}.log"
JORIS_TG_USER_ID="${BUBBLE_OPERATOR_CHAT_ID:?set BUBBLE_OPERATOR_CHAT_ID}"
SCAN_DIRS=("${SCAN_DIRS:-/tmp /var/tmp /home/claude}")

log() { printf "%s %s\n" "$(date -u +'%H:%M:%S')" "$*" >> "$LOG_FILE"; }

mkdir -p "$LOG_DIR" 2>/dev/null || true
log "sweep start ${TS_HUMAN}"

# Find plaintext secret leftovers. Exclude the encrypted .sops.env (legitimate),
# the 0600 plugin channel .env files (expected derived tokens), and gitignored
# .env.notify (derived, 0600). We only flag the LEAK patterns.
# shellcheck disable=SC2086
HITS=$(find ${SCAN_DIRS[*]} -maxdepth 4 -type f \
  \( -name "secrets*" -o -name "*.plain*" -o -name "*cred*.pem" -o -name "*-new.env" -o -name "*.plaintext" \) \
  ! -name "*.sops.env" \
  ! -name "secrets-tmp-sweep.sh" \
  ! -path "*/scripts/*" \
  ! -path "*/deploy/templates/*" \
  ! -path "/var/log/bubble-security/*" \
  ! -name "*.bak-*" \
  ! -path "*/.claude/channels/*/.env" \
  2>/dev/null || true)

if [[ -z "$HITS" ]]; then
  log "clean — no plaintext secret leftovers found"
  exit 0
fi

# Build report: perms + owner + name ONLY. Never cat contents.
REPORT=$(printf '%s\n' "$HITS" | while read -r f; do
  [[ -e "$f" ]] && stat -c '%a %U:%G %n' "$f" 2>/dev/null
done)

# Flag world-readable ones (last perm digit >= 4) as CRITICAL
CRIT=$(printf '%s\n' "$REPORT" | awk '$1 ~ /[4567]$/ {print}')

log "FOUND leftovers:"
log "$REPORT"

MSG="🔴 Bubble Security — plaintext secret leftover(s) in temp/home (${TS_HUMAN})"
MSG="${MSG}"$'\n\n'"$REPORT"
if [[ -n "$CRIT" ]]; then
  MSG="${MSG}"$'\n\n'"⚠️ WORLD-READABLE (rotate creds + shred now):"$'\n'"$CRIT"
fi
MSG="${MSG}"$'\n\n'"Shred with: sudo shred -u <file>"

TOKEN=$(awk -F= '/^TELEGRAM_BOT_TOKEN=/{print $2; exit}' "${ENV_FILE}" 2>/dev/null || true)
if [[ -n "$TOKEN" ]]; then
  curl -s --max-time 15 \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${JORIS_TG_USER_ID}" \
    --data-urlencode "text=${MSG}" >/dev/null 2>&1 \
    && log "Telegram alert sent" || log "WARN: telegram send failed"
else
  log "WARN: no TELEGRAM_BOT_TOKEN in ${ENV_FILE} — alert NOT sent"
fi

exit 0
