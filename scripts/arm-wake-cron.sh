#!/usr/bin/env bash
# arm-wake-cron.sh — print the HOST-local cron expression for a Paris wall-clock
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
#
# Fix #1510: this now converts to whatever timezone the HOST's system clock
# is actually set to (verify with `date`) — UTC on the VPS, Europe/Paris on
# the Macs — instead of hardcoding "the host is UTC". CronCreate always
# interprets the cron expression in the box's own local time, so converting
# to the box's own local time is the one mapping that's correct on every
# host. On a host whose local zone already IS Europe/Paris, this is
# (correctly) a no-op modulo any DST-boundary edge case.
#
# Portability #1510: GNU `date -d` (used below in earlier revisions) does
# not exist on BSD/macOS `date` ("date: illegal option -- d"). Rather than
# forking two different date(1) code paths (GNU vs BSD -j -f), the
# Paris -> host-local conversion is done with Python's stdlib `zoneinfo`
# (tz-database backed, DST-safe, no extra dependency, ships with python3 on
# both the VPS and every dept Mac) — see the embedded helper below. Only
# `python3` is required; it is not GNU/BSD `date`-specific.
#
# Correctness note (board #850 comment thread, Ben 2026-07-29 — kept for
# history; the code below no longer uses GNU date, but the lesson still
# applies to any future rewrite):
#   The intuitive-looking form
#     TZ=Europe/Paris date -d '22:35 today' -u +'%M %H'
#   is SILENTLY BROKEN: the TZ=<value> ENVIRONMENT-VARIABLE prefix does not
#   affect how `-d` parses a bare "HH:MM today" string once `-u` is ALSO
#   given for output — it echoes the input back unchanged (verified: prints
#   "35 22" for "22:35", i.e. no conversion at all) and looks plausible
#   enough to pass a casual glance. That is how the original two-hour bug
#   would have survived even a "fix." A timezone helper that returns its
#   input unchanged is indistinguishable, at a glance, from one that works
#   — always check any new implementation against known-answer pairs (see
#   tests/test_arm_wake_cron.sh) spanning both DST regimes AND both a
#   UTC-host and a Europe/Paris-host simulation before trusting it.
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

command -v python3 >/dev/null 2>&1 || {
  echo "ERROR: python3 is required (for tz-database-backed Paris -> host-local conversion) but not found on PATH" >&2
  exit 2
}

# Resolve the target calendar date EXPLICITLY (never lean on a bare relative
# "tomorrow", and never resolve it inside the conversion step) so the
# calendar-day arithmetic is auditable:
#   1. daily         -> today, AS OBSERVED IN Paris.
#   2. one-shot + explicit date -> that date, taken as-is.
#   3. one-shot, no explicit date -> tomorrow, AS OBSERVED IN Paris (today
#      in Paris + 1 calendar day).
# Both "today in Paris" and the actual Paris -> host-local conversion are
# done by the same embedded python3 helper so there is exactly one place
# that touches the tz database.
CONVERTED="$(python3 - "$PARIS_TIME" "$MODE" "$EXPLICIT_DATE" <<'PY'
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

paris_time, mode, explicit_date = sys.argv[1], sys.argv[2], sys.argv[3]
hh, mm = (int(x) for x in paris_time.split(":"))
paris = ZoneInfo("Europe/Paris")

today_paris = datetime.now(paris).date()

if mode == "daily":
    target_date = today_paris
elif explicit_date:
    target_date = date.fromisoformat(explicit_date)
else:
    target_date = today_paris + timedelta(days=1)

# Localize the target wall-clock time as Europe/Paris, then convert to
# whatever timezone THIS HOST's system clock is actually set to (the VPS'
# is UTC, the Macs' is Europe/Paris) — tz-database backed, so the Mar/Oct
# DST flip on either end is handled automatically, never a hardcoded offset.
dt_paris = datetime(target_date.year, target_date.month, target_date.day, hh, mm, tzinfo=paris)
dt_local = dt_paris.astimezone()

# MIN/HOUR are always printed zero-padded to 2 digits (matches the previous
# GNU `date +'%M %H'` output format exactly, byte for byte). DAY/MONTH are
# printed as plain decimal WITHOUT zero-padding (cron doesn't need it, and a
# leading zero on a cron field risks being misread as octal by naive
# parsers) — same convention the previous revision enforced explicitly.
if mode == "daily":
    print(f"{dt_local.minute:02d} {dt_local.hour:02d}")
else:
    print(f"{dt_local.minute:02d} {dt_local.hour:02d} {dt_local.day} {dt_local.month}")
PY
)"

if [[ "$MODE" == "daily" ]]; then
  read -r MIN HOUR <<<"$CONVERTED"
  echo "${MIN} ${HOUR} * * *"
else
  read -r MIN HOUR DAY MONTH <<<"$CONVERTED"
  echo "${MIN} ${HOUR} ${DAY} ${MONTH} *"
fi
