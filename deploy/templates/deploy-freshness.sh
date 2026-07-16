#!/usr/bin/env bash
# =============================================================================
# deploy-freshness.sh — silent-miss watchdog for bubble-deploy-full.timer.
# Deploy: /home/claude/scripts/deploy-freshness.sh (claude:claude 0755), via
# install-deploy-freshness.sh. Run via deploy-freshness.timer (daily, well
# after the 2h deploy cadence).
#
# OnFailure= (see onfailure-dropins/) catches a deploy that RUNS and exits
# non-zero. It CANNOT catch a deploy that never fired at all (timer disabled,
# box down at every scheduled tick, unit masked). This watchdog closes that
# gap: it checks that a SUCCESSFUL deploy happened within the last
# MAX_AGE_SEC. If not, it pings the operator once. Idempotent-quiet: silent
# when healthy. Mirrors wiki-compile-freshness.sh's pattern.
#
# Success signal: `journalctl -u bubble-deploy-full.service` records
# "=== bubble-deploy DONE ===" on the LAST run of the unit (bubble-deploy.sh
# logs this unconditionally at the end of its run — see scripts/bubble-deploy.sh).
# We check the most recent invocation's exit status AND that DONE marker via
# `systemctl show`, which is more reliable than grepping log text for success.
#
# Config (env, no hardcoded company facts — doctrine: agent-native-infra):
#   BUBBLE_OPERATOR_CHAT_ID   Telegram chat_id to alert (default: empty -> skip TG)
#   DEPLOY_FRESHNESS_UNIT     unit to watch (default: bubble-deploy-full.service)
#   DEPLOY_FRESHNESS_MAX_AGE_SEC  staleness threshold in seconds (default: below)
# =============================================================================
set -uo pipefail

ENV_FILE=/run/claude-agent/env
UNIT="${DEPLOY_FRESHNESS_UNIT:-bubble-deploy-full.service}"
# Timer cadence is every 2h (see deploy/templates/bubble-deploy-full.timer).
# Threshold = one missed run + slack, mirroring wiki-compile-freshness.sh's
# "24h cadence -> 26h threshold" ratio. Marked clearly so it's easy to retune.
MAX_AGE_SEC="${DEPLOY_FRESHNESS_MAX_AGE_SEC:-$(( 5 * 3600 ))}"   # 5h (2h cadence + slack)
LOG_TAG="deploy-freshness"

log() { logger -t "$LOG_TAG" "$*" 2>/dev/null; echo "[$LOG_TAG] $*" >&2; }

now=$(date -u +%s)

# Last time the unit STARTED (systemd tracks this regardless of outcome) and
# whether that last run exited cleanly.
last_start_ts="$(systemctl show "$UNIT" -p ExecMainStartTimestamp --value 2>/dev/null || echo '')"
last_exit_code="$(systemctl show "$UNIT" -p ExecMainStatus --value 2>/dev/null || echo '')"

if [[ -z "$last_start_ts" || "$last_start_ts" == "n/a" ]]; then
    log "STALE — $UNIT has never run (no ExecMainStartTimestamp)"
    detail="$UNIT has never run since last boot/reload — no record of a successful deploy"
    stale=1
else
    last_start_epoch=$(date -u -d "$last_start_ts" +%s 2>/dev/null || echo 0)
    age=$(( now - last_start_epoch ))
    if [[ "$last_exit_code" != "0" ]]; then
        log "STALE — last run of $UNIT exited $last_exit_code (${age}s ago)"
        detail="last run of ${UNIT} exited ${last_exit_code} (not 0), ${age}s ago"
        stale=1
    elif (( age > MAX_AGE_SEC )); then
        hrs=$(( age / 3600 ))
        log "STALE — last successful run of $UNIT was ${hrs}h ago (threshold $(( MAX_AGE_SEC / 3600 ))h)"
        detail="last successful run of ${UNIT} was ${hrs}h ago (threshold $(( MAX_AGE_SEC / 3600 ))h)"
        stale=1
    else
        log "OK — $UNIT last succeeded ${age}s ago (<= ${MAX_AGE_SEC}s); quiet."
        stale=0
    fi
fi

if [[ "$stale" != "1" ]]; then
    exit 0
fi

BOT_TOKEN=""
[[ -r "$ENV_FILE" ]] && BOT_TOKEN="$(awk -F= '/^TELEGRAM_BOT_TOKEN=/{print $2; exit}' "$ENV_FILE" 2>/dev/null)"
OPERATOR_CHAT_ID="${BUBBLE_OPERATOR_CHAT_ID:-}"

if [[ -z "$BOT_TOKEN" || -z "$OPERATOR_CHAT_ID" ]]; then
    log "no TELEGRAM_BOT_TOKEN or BUBBLE_OPERATOR_CHAT_ID configured — stale alert NOT sent"
    exit 0
fi

MSG="🟠 ${UNIT} may be STALE
${detail}
(fleet deploy sync — merged PRs are not reaching the box). Check ${UNIT%.service}.timer and the last journal: journalctl -u ${UNIT} -n 30."
curl -s --max-time 10 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
     -d chat_id="$OPERATOR_CHAT_ID" --data-urlencode text="$MSG" >/dev/null 2>&1 \
    && log "stale alert sent to chat_id=$OPERATOR_CHAT_ID" \
    || log "warn — telegram alert curl failed (non-fatal)"

exit 0
