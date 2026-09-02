#!/usr/bin/env bash
# arm-wake-cron.sh — print the box-UTC cron expression for a Paris wall-clock
# wake time, DST-safe. Fixes board #850: the VPS box's local clock is UTC,
# but every dept self-arms its next /loop wake in Paris wall-clock via
# CronCreate (layer windows L1 07:30 / L3 15:30 / L2 18:00 / L4 21:00-22:30
# are all Paris-local). CronCreate interprets its cron expression in the
# box's LOCAL time, so a naive "08:03 Paris" -> "3 8 * * *" mapping fires
# 2h late every day in summer (CEST, UTC+2) and 1h late in winter (CET,
# UTC+1) — this is exactly the defect that mistimed a live Euronext market
# order 48 minutes before close instead of mid-session (board #850).
#
# Usage:
#   scripts/arm-wake-cron.sh <Paris-HH:MM> [daily|one-shot] [YYYY-MM-DD]
#
#   <Paris-HH:MM>   Target wall-clock time in Europe/Paris, e.g. 08:03.
#   daily           (default) recurring daily cron: "M H * * *".
#   one-shot        fires once, pinned to a date: "M H D Mo *".
#                   Default target date is TOMORROW's Paris-local calendar
#                   day; pass YYYY-MM-DD to pin an explicit date instead
#                   (e.g. arming a specific market-close reminder).
#
# Output: a single line, the 5-field cron expression to hand to CronCreate.
# This script assumes the HOST clock is UTC (verify with `date` — if the
# box's local TZ is ever changed to Europe/Paris, this script must NOT be
# used as-is). It never hardcodes the CEST/CET offset — every conversion is
# derived from the system tz database at call time, so the Oct/Mar DST flip
# is handled automatically.
#
# Correctness note (board #850 comment thread, Ben 2026-07-29):
#   The intuitive-looking form
#     TZ=Europe/Paris date -d '22:35 today' -u +'%M %H'
#   is SILENTLY BROKEN: the TZ=<value> ENVIRONMENT-VARIABLE prefix does not
#   affect how `-d` parses a bare "HH:MM today" string once `-u` is ALSO
#   given for output — it echoes the input back unchanged (verified: prints
#   "35 22" for "22:35", i.e. no conversion at all) and looks plausible
#   enough to pass a casual glance. That is how the original two-hour bug
#   would have survived even a "fix."
#
#   The form that actually converts embeds the zone INSIDE the date string
#   via GNU date's `TZ="value"` prefix syntax (verified against known-answer
#   pairs spanning both DST regimes — see tests/test_arm_wake_cron.sh):
#     date -u -d 'TZ="Europe/Paris" <date> <time>' +'FORMAT'
#   This script uses ONLY that verified embedded form. Never "simplify" it
#   back to the TZ=-env-var-prefix form without re-checking against a
#   known-answer pair first — a timezone helper that returns its input
#   unchanged is indistinguishable, at a glance, from one that works.
set -euo pipefail

usage() {
  echo "Usage: $0 <Paris-HH:MM> [daily|one-shot] [YYYY-MM-DD]" >&2
  echo "  e.g.: $0 08:03 daily" >&2
  echo "        $0 14:42 one-shot           # tomorrow, Paris-local" >&2
  echo "        $0 21:00 one-shot 2026-10-28  # explicit date" >&2
  exit 2
}

PARIS_TIME="${1:-}"
MODE="${2:-daily}"
EXPLICIT_DATE="${3:-}"

[[ -n "$PARIS_TIME" ]] || usage

if ! [[ "$PARIS_TIME" =~ ^([01][0-9]|2[0-3]):([0-5][0-9])$ ]]; then
  echo "ERROR: <Paris-HH:MM> must be 24h HH:MM, got: $PARIS_TIME" >&2
  exit 2
fi

case "$MODE" in
  daily|one-shot) ;;
  *)
    echo "ERROR: mode must be 'daily' or 'one-shot', got: $MODE" >&2
    exit 2
    ;;
esac

if [[ -n "$EXPLICIT_DATE" ]] && ! [[ "$EXPLICIT_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "ERROR: date must be YYYY-MM-DD, got: $EXPLICIT_DATE" >&2
  exit 2
fi

if [[ "$MODE" == "daily" ]]; then
  # No day/month pinning needed -> safe to use the "today" keyword directly
  # inside the embedded-TZ form (verified: this combination, unlike the
  # TZ-env-prefix form above, converts correctly).
  read -r MIN HOUR <<<"$(date -u -d "TZ=\"Europe/Paris\" today ${PARIS_TIME}" +'%M %H')"
  echo "${MIN} ${HOUR} * * *"
  exit 0
fi

# one-shot: pin day + month too. Resolve the target date EXPLICITLY (never
# lean on a bare relative "tomorrow" inside the embedded-TZ string) so the
# calendar-day arithmetic is auditable and independent of exactly how GNU
# date resolves relative keywords across a timezone boundary:
#   1. Get TODAY's calendar date AS OBSERVED IN Paris — this is the most
#      basic, universally-correct use of the TZ env var (no -d, no -u; no
#      relation to the broken form above).
#   2. If no explicit date was given, add exactly one calendar day to that
#      anchor via plain date arithmetic (zone-independent once you already
#      have a concrete YYYY-MM-DD).
#   3. Feed that concrete anchor + the target time through the verified
#      embedded-TZ conversion to get the box-UTC minute/hour/day/month.
if [[ -n "$EXPLICIT_DATE" ]]; then
  TARGET_DATE="$EXPLICIT_DATE"
else
  TODAY_PARIS="$(TZ=Europe/Paris date +'%Y-%m-%d')"
  TARGET_DATE="$(date -d "${TODAY_PARIS} +1 day" +'%Y-%m-%d')"
fi

read -r MIN HOUR DAY MONTH <<<"$(date -u -d "TZ=\"Europe/Paris\" ${TARGET_DATE} ${PARIS_TIME}" +'%M %H %d %m')"
# Strip any leading zero so cron sees plain decimal fields, not
# octal-looking ones (bash `$((10#$x))` forces base-10 interpretation).
DAY=$((10#$DAY))
MONTH=$((10#$MONTH))
echo "${MIN} ${HOUR} ${DAY} ${MONTH} *"
