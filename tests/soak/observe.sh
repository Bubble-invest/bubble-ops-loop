#!/usr/bin/env bash
# Step-11 soak observation — passive metric polling every 5 min.
#
# Polls Morty's bubble-token-broker + bubble-git-guard + bubble-ops-fixture
# state and logs to observe.csv. Designed to run for 30 min in the agent
# session; for a true 24h soak, schedule via cron or systemd timer.
#
# Columns: ts, ticks_total, broker_audit_lines, guard_audit_lines,
#          journald_broker_entries, ops_loop_pid, ops_loop_active,
#          remote_commit_count, errors_in_journal
#
# Usage:
#   bash tests/soak/observe.sh [DURATION_MIN] [INTERVAL_SEC]
#
# Defaults: DURATION_MIN=30, INTERVAL_SEC=300 (5 min).

set -euo pipefail

DURATION_MIN="${1:-30}"
INTERVAL_SEC="${2:-300}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CSV="$SCRIPT_DIR/observe.csv"

# CSV header (only write if file is new / empty)
if [ ! -s "$CSV" ]; then
  echo "ts,ticks_total,broker_audit_lines,guard_audit_lines,journald_broker_entries,ops_loop_pid,ops_loop_active,remote_commit_count,errors_in_journal" > "$CSV"
fi

# Helper: probe Morty and return one CSV row (no newline)
probe() {
  local ts ticks broker_lines guard_lines journald_entries ops_loop_pid \
        ops_loop_active remote_commit_count errors

  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  # Single ssh call returns a |-delimited line that we parse locally.
  # All sub-counts are 0 by default if the corresponding file/log is missing
  # (graceful degradation; never crash the soak).
  local payload
  payload="$(ssh -o BatchMode=yes -o ConnectTimeout=10 hetzner '
    ticks="0"
    if [ -r /home/claude/agents/fixture/.claude/scheduled_tasks.lock ] || true; then
      ticks=$(sudo find /home/claude/agents/fixture/.claude/ -name "heartbeat*" -type f 2>/dev/null | head -1 | xargs -r sudo wc -l 2>/dev/null | awk "{print \$1}")
      [ -z "$ticks" ] && ticks="0"
    fi
    broker_lines=$(sudo wc -l < /var/log/bubble-token-broker/audit.jsonl 2>/dev/null || echo 0)
    guard_lines=$(sudo wc -l < /var/log/bubble-git-guard/audit.jsonl 2>/dev/null || echo 0)
    journald_entries=$(sudo journalctl SYSLOG_IDENTIFIER=bubble-token-broker --no-pager 2>/dev/null | wc -l)
    pid=$(systemctl show -p MainPID --value ops-loop-fixture.service 2>/dev/null || echo 0)
    active=$(systemctl is-active ops-loop-fixture.service 2>/dev/null || echo unknown)
    errors=$(sudo journalctl -u ops-loop-fixture.service --since "-5min" --no-pager 2>/dev/null | grep -iE "error|fatal|traceback" | wc -l)
    printf "%s|%s|%s|%s|%s|%s|%s" "$ticks" "$broker_lines" "$guard_lines" "$journald_entries" "$pid" "$active" "$errors"
  ' 2>/dev/null || echo '0|0|0|0|0|unknown|0')"

  IFS='|' read -r ticks broker_lines guard_lines journald_entries ops_loop_pid ops_loop_active errors <<< "$payload"

  # Remote commit count via gh (cached when possible by gh's own cache)
  remote_commit_count="$(gh api 'repos/vdk888/bubble-ops-fixture/commits?per_page=100' --jq 'length' 2>/dev/null || echo 0)"

  printf "%s,%s,%s,%s,%s,%s,%s,%s,%s\n" \
    "$ts" "$ticks" "$broker_lines" "$guard_lines" "$journald_entries" \
    "$ops_loop_pid" "$ops_loop_active" "$remote_commit_count" "$errors"
}

DURATION_SEC=$(( DURATION_MIN * 60 ))
START_SEC=$(date +%s)
END_SEC=$(( START_SEC + DURATION_SEC ))

echo "[soak] start ts=$(date -u +%Y-%m-%dT%H:%M:%SZ) duration=${DURATION_MIN}min interval=${INTERVAL_SEC}s csv=$CSV" >&2

while [ "$(date +%s)" -lt "$END_SEC" ]; do
  row="$(probe)"
  echo "$row" >> "$CSV"
  echo "[soak] $row" >&2
  # Sleep only if we have time left
  REMAIN=$(( END_SEC - $(date +%s) ))
  if [ "$REMAIN" -gt 0 ] && [ "$REMAIN" -ge "$INTERVAL_SEC" ]; then
    sleep "$INTERVAL_SEC"
  elif [ "$REMAIN" -gt 0 ]; then
    sleep "$REMAIN"
  fi
done

echo "[soak] complete ts=$(date -u +%Y-%m-%dT%H:%M:%SZ) rows=$(wc -l < "$CSV")" >&2
