#!/usr/bin/env bash
# =============================================================================
# test_arm_wake_cron.sh — DST-safety regression test for
# scripts/arm-wake-cron.sh (board #850: self-armed wakes fired 2h late
# because CronCreate reads box-local (UTC) while wake times are armed in
# Paris wall-clock).
#
# Covers:
#   A. Known-answer pairs reproducing the ACTUAL incident on the card —
#      2026-07-28 (CEST, summer, UTC+2): the three armed/fired pairs Ben
#      reported must derive correctly via the helper.
#   B. A winter (CET, UTC+1) known-answer pair, proving the DST flip is
#      handled via the tz database, not a hardcoded offset.
#   C. Daily mode ("today") produces the right minute/hour for the
#      currently-running season (sanity: matches whatever offset `date`
#      itself reports right now).
#   D. Input validation: bad HH:MM, bad mode, bad date all fail loudly
#      (non-zero exit), never silently produce a wrong-but-plausible cron.
#   E. Regression guard: the WRONG mapping from the original incident/boot
#      guidance ("08:03 Paris" -> "3 8 * * *", i.e. treating Paris wall-clock
#      as if it were box-UTC) must NOT be what the helper produces for a
#      summer date.
#
# Run:  bash tests/test_arm_wake_cron.sh
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
SCRIPT="$REPO_ROOT/scripts/arm-wake-cron.sh"

[[ -f "$SCRIPT" ]] || { echo "FATAL: helper not found: $SCRIPT"; exit 2; }
[[ -x "$SCRIPT" ]] || { echo "FATAL: helper not executable: $SCRIPT"; exit 2; }

# This test needs GNU date (the box the helper actually runs on is Linux).
# On macOS, `date` is BSD date and lacks -d; fall back to a Homebrew
# coreutils gnubin PATH prefix if present, else skip with a clear message
# rather than failing on the wrong tool.
if ! date -d 'today' +'%H' >/dev/null 2>&1; then
  GNUBIN="/opt/homebrew/opt/coreutils/libexec/gnubin"
  if [[ -x "$GNUBIN/date" ]]; then
    export PATH="$GNUBIN:$PATH"
  fi
fi
if ! date -d 'today' +'%H' >/dev/null 2>&1; then
  echo "SKIP: no GNU date on PATH (needed by both the helper and this test)."
  echo "      On macOS: brew install coreutils, or run this on the VPS/CI (ubuntu-latest)."
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

# ── A. known-answer pairs from the ACTUAL board #850 incident (2026-07-28, CEST) ──
echo "A. summer (CEST, UTC+2) known-answer pairs from the #850 incident"
OUT="$("$SCRIPT" 08:03 one-shot 2026-07-28)"
check_eq "08:03 Paris on 2026-07-28 (one-shot)" "03 06 28 7 *" "$OUT"

OUT="$("$SCRIPT" 14:42 one-shot 2026-07-28)"
check_eq "14:42 Paris on 2026-07-28 (one-shot) — the mistimed GTT execution tick" "42 12 28 7 *" "$OUT"

OUT="$("$SCRIPT" 21:07 one-shot 2026-07-28)"
check_eq "21:07 Paris on 2026-07-28 (one-shot) — the missed ACGL-prep wake" "07 19 28 7 *" "$OUT"

# ── B. winter (CET, UTC+1) known-answer pair ─────────────────────────────────
echo ""
echo "B. winter (CET, UTC+1) known-answer pair — proves DST is derived, not hardcoded"
OUT="$("$SCRIPT" 08:03 one-shot 2026-01-15)"
check_eq "08:03 Paris on 2026-01-15 (one-shot, winter)" "03 07 15 1 *" "$OUT"

OUT="$("$SCRIPT" 21:00 one-shot 2026-12-24)"
check_eq "21:00 Paris on 2026-12-24 (one-shot, winter)" "00 20 24 12 *" "$OUT"

# ── C. daily mode sanity — matches whatever the CURRENT offset actually is ──
echo ""
echo "C. daily mode ('today') is internally consistent with the live offset"
# %z is e.g. +0200 (CEST) / +0100 (CET); take the sign+hour digits.
CUR_OFFSET_HOURS="$(TZ=Europe/Paris date +%z | cut -c1-3)"
CUR_OFFSET_HOURS="${CUR_OFFSET_HOURS#+}"
CUR_OFFSET_HOURS=$((10#$CUR_OFFSET_HOURS))
EXPECT_MIN=03
EXPECT_HOUR=$(( (8 - CUR_OFFSET_HOURS + 24) % 24 ))
EXPECT_HOUR_STR="$(printf '%02d' "$EXPECT_HOUR")"
OUT="$("$SCRIPT" 08:03 daily)"
check_eq "08:03 Paris today (daily), live UTC offset is ${CUR_OFFSET_HOURS}h" "${EXPECT_MIN} ${EXPECT_HOUR_STR} * * *" "$OUT"

# ── D. input validation ──────────────────────────────────────────────────────
echo ""
echo "D. input validation fails loudly instead of guessing"
"$SCRIPT" 25:99 daily >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "bad HH:MM rejected (non-zero exit)" || bad "bad HH:MM silently accepted"

"$SCRIPT" 08:03 weekly >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "bad mode rejected (non-zero exit)" || bad "bad mode silently accepted"

"$SCRIPT" 08:03 one-shot "not-a-date" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "bad explicit date rejected (non-zero exit)" || bad "bad explicit date silently accepted"

"$SCRIPT" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "missing required arg rejected (non-zero exit)" || bad "missing arg silently accepted"

# ── E. regression guard: the ORIGINAL wrong mapping must not reappear ────────
echo ""
echo "E. regression guard — the incident's wrong Paris-as-box-local mapping is gone"
OUT="$("$SCRIPT" 08:03 daily)"
if [[ "$OUT" == "3 8 * * *" || "$OUT" == "03 08 * * *" ]]; then
  bad "helper reproduced the WRONG naive mapping (08:03 Paris treated as 08:03 box-UTC): '$OUT'"
else
  ok "helper does NOT reproduce the wrong naive Paris-as-UTC mapping (got '$OUT', not '3 8 * * *')"
fi

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
