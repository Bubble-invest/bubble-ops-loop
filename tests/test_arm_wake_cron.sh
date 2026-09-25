#!/usr/bin/env bash
# =============================================================================
# test_arm_wake_cron.sh — DST-safety + cross-platform portability regression
# test for scripts/arm-wake-cron.sh.
#
# Board #850: self-armed wakes fired 2h late because CronCreate reads
# box-local time (UTC on the VPS) while wake times are armed in Paris
# wall-clock.
# Board #1510: the GNU-`date -d`-based fix from #850 dies on macOS/BSD
# `date` ("date: illegal option -- d"). The script now converts via
# python3's stdlib `zoneinfo` instead, and always converts to whatever
# timezone the HOST it is running on actually has (UTC on the VPS,
# Europe/Paris on the Macs) — never a hardcoded "the host is UTC"
# assumption.
#
# This test runs unmodified on BOTH platforms (CI/ubuntu-latest and macOS):
# it never touches GNU-date-only flags itself, and it EXERCISES both host
# regimes on a single machine by forcing the `TZ` env var around each call —
# TZ=UTC simulates the VPS, TZ=Europe/Paris simulates a Mac. `TZ` overrides
# the process's notion of "the system's local timezone", which is exactly
# what `datetime.astimezone()` (no-arg) inside the script reads, so this
# faithfully exercises both host code paths regardless of which one the
# test happens to actually run on.
#
# Covers:
#   A. UTC-host (VPS) simulation, summer (CEST, UTC+2) known-answer pairs
#      reproducing the ACTUAL board #850 incident (2026-07-28) — one-shot.
#   B. UTC-host (VPS) simulation, winter (CET, UTC+1) known-answer pairs —
#      proves the DST flip is derived from the tz database, not hardcoded.
#   C. UTC-host (VPS) simulation, DST TRANSITION-DAY edge cases: the actual
#      2026 EU spring-forward (2026-03-29) and fall-back (2026-10-25) dates,
#      one day before and on the transition day itself, one-shot mode.
#   D. Europe/Paris-host (Mac) simulation: converting Paris -> Paris-local
#      must be a no-op (same wall-clock minute/hour), across both a summer
#      and a winter date, one-shot AND daily — this is the literal fix for
#      #1510 (a Mac dept's own host clock already IS Europe/Paris).
#   E. Daily mode ("today") sanity on a UTC-simulated host: matches
#      whatever the CURRENT live Paris-UTC offset actually is.
#   F. Input validation: bad HH:MM, bad mode, bad date, missing arg all fail
#      loudly (non-zero exit), never silently produce a wrong-but-plausible
#      cron expression.
#   G. Regression guard: the WRONG mapping from the original incident
#      ("08:03 Paris" -> "3 8 * * *", i.e. treating Paris wall-clock as if
#      it were box-UTC) must NOT be what the helper produces on a
#      UTC-simulated host in summer.
#
# Run:  bash tests/test_arm_wake_cron.sh
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
SCRIPT="$REPO_ROOT/scripts/arm-wake-cron.sh"

[[ -f "$SCRIPT" ]] || { echo "FATAL: helper not found: $SCRIPT"; exit 2; }
[[ -x "$SCRIPT" ]] || { echo "FATAL: helper not executable: $SCRIPT"; exit 2; }

# The helper is pure bash + python3 (stdlib zoneinfo) — no GNU-date-only
# flags, so unlike the pre-#1510 revision this test needs no coreutils/gdate
# fallback and runs identically on Linux and macOS. It does still need
# python3 on PATH, same as the helper itself.
if ! command -v python3 >/dev/null 2>&1; then
  echo "SKIP: no python3 on PATH (needed by both the helper and this test)."
  exit 0
fi

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

check_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    ok "$label -> '$actual'"
  else
    bad "$label: expected '$expected', got '$actual'"
  fi
}

echo "== test_arm_wake_cron.sh =="
echo "   script: $SCRIPT"
echo ""

# ── A. UTC-host (VPS) simulation: summer (CEST, UTC+2) known-answer pairs,
#      the ACTUAL board #850 incident (2026-07-28) ──────────────────────────
echo "A. TZ=UTC (simulated VPS host), summer (CEST, UTC+2) — the #850 incident"
OUT="$(TZ=UTC "$SCRIPT" 08:03 one-shot 2026-07-28)"
check_eq "08:03 Paris on 2026-07-28 (one-shot)" "03 06 28 7 *" "$OUT"

OUT="$(TZ=UTC "$SCRIPT" 14:42 one-shot 2026-07-28)"
check_eq "14:42 Paris on 2026-07-28 (one-shot) — the mistimed GTT execution tick" "42 12 28 7 *" "$OUT"

OUT="$(TZ=UTC "$SCRIPT" 21:07 one-shot 2026-07-28)"
check_eq "21:07 Paris on 2026-07-28 (one-shot) — the missed ACGL-prep wake" "07 19 28 7 *" "$OUT"

# ── B. UTC-host (VPS) simulation: winter (CET, UTC+1) known-answer pairs ────
echo ""
echo "B. TZ=UTC (simulated VPS host), winter (CET, UTC+1) — proves DST is derived, not hardcoded"
OUT="$(TZ=UTC "$SCRIPT" 08:03 one-shot 2026-01-15)"
check_eq "08:03 Paris on 2026-01-15 (one-shot, winter)" "03 07 15 1 *" "$OUT"

OUT="$(TZ=UTC "$SCRIPT" 21:00 one-shot 2026-12-24)"
check_eq "21:00 Paris on 2026-12-24 (one-shot, winter)" "00 20 24 12 *" "$OUT"

# ── C. UTC-host (VPS) simulation: real 2026 EU DST transition-day edges ─────
echo ""
echo "C. TZ=UTC (simulated VPS host), DST transition-day edges (2026)"
OUT="$(TZ=UTC "$SCRIPT" 08:03 one-shot 2026-03-28)"
check_eq "08:03 Paris on 2026-03-28 (day before spring-forward, still CET)" "03 07 28 3 *" "$OUT"

OUT="$(TZ=UTC "$SCRIPT" 08:03 one-shot 2026-03-29)"
check_eq "08:03 Paris on 2026-03-29 (spring-forward day itself, now CEST)" "03 06 29 3 *" "$OUT"

OUT="$(TZ=UTC "$SCRIPT" 08:03 one-shot 2026-10-24)"
check_eq "08:03 Paris on 2026-10-24 (day before fall-back, still CEST)" "03 06 24 10 *" "$OUT"

OUT="$(TZ=UTC "$SCRIPT" 08:03 one-shot 2026-10-25)"
check_eq "08:03 Paris on 2026-10-25 (fall-back day itself, now CET)" "03 07 25 10 *" "$OUT"

# ── D. Europe/Paris-host (Mac) simulation: Paris -> Paris-local is a no-op ──
echo ""
echo "D. TZ=Europe/Paris (simulated Mac host) — Paris -> host-local is a no-op (#1510 fix)"
OUT="$(TZ=Europe/Paris "$SCRIPT" 08:03 one-shot 2026-07-28)"
check_eq "08:03 Paris on 2026-07-28, host already Europe/Paris (one-shot, summer)" "03 08 28 7 *" "$OUT"

OUT="$(TZ=Europe/Paris "$SCRIPT" 08:03 one-shot 2026-01-15)"
check_eq "08:03 Paris on 2026-01-15, host already Europe/Paris (one-shot, winter)" "03 08 15 1 *" "$OUT"

OUT="$(TZ=Europe/Paris "$SCRIPT" 08:03 daily)"
check_eq "08:03 Paris today, host already Europe/Paris (daily)" "03 08 * * *" "$OUT"

# ── E. UTC-host (VPS) simulation: daily mode ('today') sanity, live offset ──
echo ""
echo "E. TZ=UTC (simulated VPS host), daily mode ('today') is internally consistent with the live offset"
# %z is e.g. +0200 (CEST) / +0100 (CET); take the sign+hour digits. This
# only needs GNU-or-BSD-agnostic `date +%z`, which both platforms support
# (unlike `-d`), to independently derive the expected offset for TODAY.
CUR_OFFSET_HOURS="$(TZ=Europe/Paris date +%z | cut -c1-3)"
CUR_OFFSET_HOURS="${CUR_OFFSET_HOURS#+}"
CUR_OFFSET_HOURS=$((10#$CUR_OFFSET_HOURS))
EXPECT_MIN=03
EXPECT_HOUR=$(( (8 - CUR_OFFSET_HOURS + 24) % 24 ))
EXPECT_HOUR_STR="$(printf '%02d' "$EXPECT_HOUR")"
OUT="$(TZ=UTC "$SCRIPT" 08:03 daily)"
check_eq "08:03 Paris today (daily), live UTC offset is ${CUR_OFFSET_HOURS}h" "${EXPECT_MIN} ${EXPECT_HOUR_STR} * * *" "$OUT"

# ── F. input validation ──────────────────────────────────────────────────────
echo ""
echo "F. input validation fails loudly instead of guessing"
"$SCRIPT" 25:99 daily >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "bad HH:MM rejected (non-zero exit)" || bad "bad HH:MM silently accepted"

"$SCRIPT" 08:03 weekly >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "bad mode rejected (non-zero exit)" || bad "bad mode silently accepted"

"$SCRIPT" 08:03 one-shot "not-a-date" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "bad explicit date rejected (non-zero exit)" || bad "bad explicit date silently accepted"

"$SCRIPT" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "missing required arg rejected (non-zero exit)" || bad "missing arg silently accepted"

# ── G. regression guard: the ORIGINAL wrong mapping must not reappear ────────
echo ""
echo "G. regression guard — the incident's wrong Paris-as-box-local mapping is gone"
OUT="$(TZ=UTC "$SCRIPT" 08:03 daily)"
if [[ "$OUT" == "3 8 * * *" || "$OUT" == "03 08 * * *" ]]; then
  bad "helper reproduced the WRONG naive mapping (08:03 Paris treated as 08:03 box-UTC): '$OUT'"
else
  ok "helper does NOT reproduce the wrong naive Paris-as-UTC mapping (got '$OUT', not '3 8 * * *')"
fi

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
