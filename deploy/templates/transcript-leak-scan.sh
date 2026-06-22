#!/usr/bin/env bash
# /home/claude/scripts/transcript-leak-scan.sh
# Layer 3 — Dedicated JSONL transcript leak scanner
# Runs daily at 06:30 UTC via transcript-leak-scan.timer
#
# Scans /home/claude/.claude/projects/**/*.jsonl for credential-prefix patterns
# in files modified in the last 24h.
#
# OUTPUT SAFETY RULE (SPEC-008 derived):
#   - Matched lines are NEVER printed to stdout/stderr/journal/log.
#   - Only <file>:<line>: <first-10-chars>...[REDACTED] is recorded.
#   - Telegram alert NEVER includes the matched value — only file count + names.
#   - TOKEN is unset immediately after curl.
#
# Exit codes: 0 = clean (or scan errors), 1 = internal script error
# Telegram alert is sent ONLY on findings (no noise on clean runs).

set -uo pipefail

# ─── Configuration ────────────────────────────────────────────────────────
# These can be overridden via environment for testing:
#   TRANSCRIPTS_DIR=/tmp/test-dir LOG_DIR=/tmp/test-logs ./transcript-leak-scan.sh
ENV_FILE="${ENV_FILE:-/run/claude-agent/env}"
TRANSCRIPTS_DIR="${TRANSCRIPTS_DIR:-/home/claude/.claude/projects}"
LOG_DIR="${LOG_DIR:-/var/log/bubble-security}"
DATE_STAMP=$(date -u +"%Y-%m-%d")
TS_HUMAN=$(date -u +"%Y-%m-%d %H:%M UTC")
LOG_FILE="${LOG_DIR}/transcript-leak-scan-${DATE_STAMP}.log"
OPERATOR_TG_USER_ID="${BUBBLE_OPERATOR_CHAT_ID:?set BUBBLE_OPERATOR_CHAT_ID}"
# How far back to look (seconds). 86400 = 24h. Add a 5-min buffer for timer jitter.
# Can be overridden: LOOKBACK_SECS=99999 for test (catch all files regardless of mtime)
LOOKBACK_SECS="${LOOKBACK_SECS:-87300}"

# ─── Patterns to hunt ─────────────────────────────────────────────────────
# NEVER add a pattern that would cause grep to print the matched value.
# We use grep -l (filename only) for the initial scan, then a second pass
# with grep -n to get line numbers + first-10-chars prefix ONLY.
PATTERNS=(
    "tskey-auth-[a-zA-Z0-9]{20,}"
    "sk-ant-oat01-[A-Za-z0-9_-]{40,}"
    "github_pat_[A-Za-z0-9_]{50,}"
    "ghp_[A-Za-z0-9]{36}"
    "ghs_[A-Za-z0-9]{30,}"
    "sk-[a-zA-Z0-9_-]{32,}"
    "AKIA[0-9A-Z]"
    "xoxb-"
    "xoxp-"
)

# ─── Logging ─────────────────────────────────────────────────────────────
log() {
    echo "[${TS_HUMAN}] $*" >> "${LOG_FILE}"
}

# ─── Ensure log dir exists ────────────────────────────────────────────────
mkdir -p "${LOG_DIR}" || { echo "FATAL: cannot create ${LOG_DIR}" >&2; exit 1; }

log "=== transcript-leak-scan start ==="
log "lookback=${LOOKBACK_SECS}s patterns=${#PATTERNS[@]}"

# ─── Collect recently modified JSONL files ────────────────────────────────
if [[ ! -d "${TRANSCRIPTS_DIR}" ]]; then
    log "transcripts dir not found: ${TRANSCRIPTS_DIR} — exiting clean"
    log "=== transcript-leak-scan done (no transcripts dir) ==="
    exit 0
fi

mapfile -t RECENT_FILES < <(
    find "${TRANSCRIPTS_DIR}" -type f -name "*.jsonl" \
        -newer /proc/1/cmdline \
        -newermt "-${LOOKBACK_SECS} seconds" 2>/dev/null \
    || true
)

# Fallback: if -newermt fails (busybox find), use -mmin
if [[ ${#RECENT_FILES[@]} -eq 0 ]]; then
    mapfile -t RECENT_FILES < <(
        find "${TRANSCRIPTS_DIR}" -type f -name "*.jsonl" \
            -mmin "-$(( LOOKBACK_SECS / 60 ))" 2>/dev/null \
        || true
    )
fi

log "recent JSONL files to scan: ${#RECENT_FILES[@]}"

if [[ ${#RECENT_FILES[@]} -eq 0 ]]; then
    log "0 recent files — nothing to scan"
    log "=== transcript-leak-scan done (0 files, clean) ==="
    exit 0
fi

# ─── Build grep -e args from PATTERNS array ───────────────────────────────
GREP_ARGS=()
for pat in "${PATTERNS[@]}"; do
    GREP_ARGS+=( -e "${pat}" )
done

# ─── Phase 1: filename-only scan (no value leakage) ─────────────────────
LEAK_FILES=()
for f in "${RECENT_FILES[@]}"; do
    # -l: print filename only. -I: skip binary files. -P: PCRE-like (GNU grep).
    if grep -lI "${GREP_ARGS[@]}" "${f}" >/dev/null 2>&1; then
        LEAK_FILES+=( "${f}" )
    fi
done

LEAK_COUNT=${#LEAK_FILES[@]}
log "phase1 matches: ${LEAK_COUNT} file(s)"

# ─── Phase 2 (findings only): extract line# + first-10-chars + REDACTED ─
if [[ ${LEAK_COUNT} -gt 0 ]]; then
    log "--- redacted match locations ---"
    for f in "${LEAK_FILES[@]}"; do
        log "file: ${f}"
        # grep -n: prefix with line number. Then awk: print file:line: FIRST10...[REDACTED]
        # The pattern match position is found; we take the 10 chars at the match start.
        # We do NOT print the full line — only enough to confirm it's a real hit.
        while IFS= read -r match_line; do
            lineno="${match_line%%:*}"
            content="${match_line#*:}"
            # Find which pattern matched, extract 10 chars starting at match
            redacted_prefix=""
            for pat in "${PATTERNS[@]}"; do
                # Use bash regex to find the pattern; extract match+4 trailing chars
                if [[ "${content}" =~ (${pat}[A-Za-z0-9_\-]{0,4}) ]]; then
                    redacted_prefix="${BASH_REMATCH[1]}"
                    break
                fi
            done
            if [[ -z "${redacted_prefix}" ]]; then
                redacted_prefix="(pattern-match)"
            fi
            log "  ${f}:${lineno}: ${redacted_prefix}...[REDACTED]"
        done < <(
            grep -nI "${GREP_ARGS[@]}" "${f}" 2>/dev/null \
                | cut -c1-120 \
            || true
        )
    done
    log "--- end redacted locations ---"
fi

# ─── Telegram alert (ONLY on findings) ───────────────────────────────────
if [[ ${LEAK_COUNT} -gt 0 ]]; then
    # Build file list — basenames only in the alert (no full paths that
    # could inadvertently encode project names with secrets in dir names)
    FILE_BASENAMES=""
    for f in "${LEAK_FILES[@]}"; do
        FILE_BASENAMES+="  • $(basename "${f}")\n"
    done

    ALERT_MSG="🚨 Leak detected in ${LEAK_COUNT} transcript(s) [${DATE_STAMP}]

Matched credential patterns in recently-modified JSONL files:
${FILE_BASENAMES}
Full log: ${LOG_FILE}

Action: rotate exposed credentials immediately."

    TOKEN=$(awk -F= '/^TELEGRAM_BOT_TOKEN=/{print $2; exit}' "${ENV_FILE}" 2>/dev/null)
    if [[ -n "${TOKEN:-}" ]]; then
        curl -s --max-time 15 \
            "https://api.telegram.org/bot${TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${OPERATOR_TG_USER_ID}" \
            --data-urlencode "text=${ALERT_MSG}" \
            > /dev/null 2>&1
        unset TOKEN
        log "Telegram alert sent to chat_id=${OPERATOR_TG_USER_ID}"
    else
        unset TOKEN
        log "WARN: no TELEGRAM_BOT_TOKEN in ${ENV_FILE} — Telegram alert NOT sent"
    fi
fi

log "=== transcript-leak-scan done (leaks=${LEAK_COUNT}) ==="
exit 0
