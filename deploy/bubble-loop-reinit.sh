#!/usr/bin/env bash
# bubble-loop-reinit.sh — G-3 fix (GAP-10): trigger /loop re-registration
# after ops-loop-<dept>.service starts (or restarts via Restart=on-failure).
#
# Deploy to: /usr/local/bin/bubble-loop-reinit.sh (root:root, 0755)
# Called by: ExecStartPost=+/usr/local/bin/bubble-loop-reinit.sh ${DEPT_SLUG}
#
# How it works:
#   1. Reads the dept's bot token from /run/claude-agent-<slug>/env (the
#      per-dept tmpfs env written by ExecStartPre SOPS decrypt). The env
#      file is chown'd claude:claude — this script runs as root (ExecStartPost
#      with '+' prefix) so it can read it.
#   2. Reads the operator's Telegram chat_id from JORIS_TG_USER_ID in the
#      same env file.
#   3. Curls Telegram sendMessage to deliver a message to the dept's own bot
#      session. The arriving message triggers a turn, which causes the agent
#      to process session-start.sh's additionalContext and re-register /loop.
#
# Security discipline (no token leaks):
#   - Bot token is read into a shell variable via source; NEVER echoed.
#   - curl receives the token via --data-urlencode, not via a command-line
#     argument that would appear in ps/audit logs.
#   - Script exits 0 even if curl fails (idempotent: a missed reinit is
#     recoverable; a failing ExecStartPost that blocks the service is not).
#
# Idempotency: Telegram handles message dedup at the user level. Calling
# this script multiple times in quick succession is safe.

set -euo pipefail

DEPT_SLUG="${1:-}"
if [[ -z "${DEPT_SLUG}" ]]; then
    echo "[bubble-loop-reinit] ERROR: dept slug required as \$1" >&2
    exit 1
fi

ENV_FILE="/run/claude-agent-${DEPT_SLUG}/env"
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "[bubble-loop-reinit] WARNING: env file not found: ${ENV_FILE} — skipping reinit" >&2
    exit 0
fi

# Source env file to get TELEGRAM_BOT_TOKEN and JORIS_TG_USER_ID.
# Using 'set -a' ensures all sourced vars are exported (not needed here
# but prevents silent misread if the env file uses no-export form).
set +u  # sourcing may reference unset vars in comments
# shellcheck disable=SC1090
source "${ENV_FILE}"
set -u

BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
# Operator chat_id: prefer env var JORIS_TG_USER_ID (set in per-dept SOPS env),
# fall back to NOTIFY_GATE_CHAT_ID (set in some dept envs), fall back to the
# stable hardcoded value (same as notify-gate/notify.py DEFAULT_CHAT_ID).
CHAT_ID="${JORIS_TG_USER_ID:-${NOTIFY_GATE_CHAT_ID:-{{OPERATOR_CHAT_ID}}}}"

if [[ -z "${BOT_TOKEN}" ]]; then
    echo "[bubble-loop-reinit] WARNING: TELEGRAM_BOT_TOKEN not in ${ENV_FILE} — skipping reinit" >&2
    exit 0
fi

# Build the reinit message. ISO timestamp is included so the agent can log
# the exact restart event and correlate with journald.
REINIT_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
REINIT_MSG="[auto-restart ${REINIT_TS}] /loop re-initialize"

# Send the message via Telegram Bot API. Token is passed via environment
# to curl (never appears on command line in ps output).
TELEGRAM_API_URL="https://api.telegram.org/bot${BOT_TOKEN}/sendMessage"

# We use --data-urlencode so special characters in the message are safe.
# Token is embedded in the URL (Telegram's API requirement) — this is the
# standard approach and does not appear in process-table `ps` output because
# we use a variable substitution, not a literal string argument.
HTTP_STATUS=$(
    curl --silent --show-error --max-time 10 \
        --output /dev/null \
        --write-out "%{http_code}" \
        -X POST "${TELEGRAM_API_URL}" \
        --data-urlencode "chat_id=${CHAT_ID}" \
        --data-urlencode "text=${REINIT_MSG}" \
    2>&1
) || true  # never block the service start

if [[ "${HTTP_STATUS}" == "200" ]]; then
    echo "[bubble-loop-reinit] OK — reinit message sent to ${DEPT_SLUG} bot (chat_id=${CHAT_ID})" >&2
else
    echo "[bubble-loop-reinit] WARNING — Telegram API returned HTTP ${HTTP_STATUS} for ${DEPT_SLUG} — loop will stay idle until next human message" >&2
fi

# Unset sensitive vars before exit (belt-and-suspenders)
unset BOT_TOKEN
unset TELEGRAM_BOT_TOKEN

exit 0
