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
# Board #1515: `datetime.astimezone()` (no-arg) silently falls back to UTC
# when TZ is set to an unknown/invalid zone (e.g. TZ=Bogus/Nonexistent) —
# it does NOT raise. That is the same silent-wrong-hour class as #850 (a
# 2h/1h offset nobody notices until a live order fires at the wrong time),
# just triggered by a bad TZ value instead of a naive mapping. The script
# now explicitly validates TZ (when set and non-empty) resolves to a real
# IANA zone via zoneinfo, and exits 2 with a stderr message (no stdout) if
# it doesn't — never silently defaulting to UTC.
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
#   H. Board #1515: unknown/invalid host TZ fails loud, never silent-UTC.
#   I. Board #1529: the whole matrix above (A-H) is re-run with $SCRIPT
#      invoked EXPLICITLY through a real bash 3.x interpreter when one is
#      available on this host (macOS stock /bin/bash, never upgraded past
#      3.2.57 post-GPLv3) — never relying on the script's own
#      `#!/usr/bin/env bash` shebang, which on a dev box with Homebrew bash
#      installed resolves to bash 5 and silently hides the bug. This is the
#      regression guard for #1529: a heredoc nested inside a `$(...)`
#      command substitution parses fine under bash 5 and mis-parses under
#      bash 3.2 ("bad substitution" / "unexpected EOF"), so simulating the
#      bug via TZ tricks (as sections A-H do for DST) cannot catch it —
#      only an actual bash 3.x process can. Skips gracefully (exit 0 for
#      that section, not the whole suite) when no bash 3.x is available
#      (e.g. Linux CI).
#   J. Board #1529: a missing conversion helper (scripts/
#      arm_wake_cron_convert.py, e.g. a partial checkout/rsync) fails loud
#      — non-zero exit, empty stdout, a stderr message — instead of falling
#      through to some other python3 on PATH or silently producing nothing.
#
# Run:  bash tests/test_arm_wake_cron.sh
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
SCRIPT="$REPO_ROOT/scripts/arm-wake-cron.sh"
HELPER="$REPO_ROOT/scripts/arm_wake_cron_convert.py"

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

# ── Board #1529: locate a real bash 3.x interpreter, or fall back gracefully.
# macOS ships bash 3.2 at /bin/bash (never upgraded post-GPLv3); Linux CI has
# no bash 3.x at all. Invoking $SCRIPT through this interpreter EXPLICITLY
# (RUNNER, used everywhere below instead of a bare "$SCRIPT") is what
# actually exercises the #1529 heredoc-in-$() parser bug — the script's own
# `#!/usr/bin/env bash` shebang resolves to whatever bash is first on PATH,
# which on a dev Mac with Homebrew installed is bash 5 and would silently
# hide the regression.
BASH32=""
for cand in /bin/bash /usr/bin/bash /opt/local/bin/bash3 /usr/local/bin/bash3; do
  if [[ -x "$cand" ]]; then
    v="$("$cand" -c 'echo "${BASH_VERSINFO[0]}"' 2>/dev/null || true)"
    if [[ "$v" =~ ^[0-9]+$ ]] && [[ "$v" -lt 4 ]]; then BASH32="$cand"; break; fi
  fi
done

echo "I. board #1529 — locating a bash 3.x interpreter for explicit invocation"
if [[ -n "$BASH32" ]]; then
  echo "   found: $BASH32 ($("$BASH32" -c 'echo "$BASH_VERSION"')) — sections A-H below all invoke \$SCRIPT explicitly through it (board #1529 regression guard)"
  RUNNER=("$BASH32" "$SCRIPT")
else
  echo "   SKIP: no bash 3.x interpreter found on this host (board #1529's heredoc-in-\$() parser bug is bash-3.2-specific and cannot be exercised without it, e.g. Linux CI) — sections A-H below invoke \$SCRIPT via its own shebang instead."
  RUNNER=("$SCRIPT")
fi
echo ""

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
OUT="$(TZ=UTC "${RUNNER[@]}" 08:03 one-shot 2026-07-28)"
check_eq "08:03 Paris on 2026-07-28 (one-shot)" "03 06 28 7 *" "$OUT"

OUT="$(TZ=UTC "${RUNNER[@]}" 14:42 one-shot 2026-07-28)"
check_eq "14:42 Paris on 2026-07-28 (one-shot) — the mistimed GTT execution tick" "42 12 28 7 *" "$OUT"

OUT="$(TZ=UTC "${RUNNER[@]}" 21:07 one-shot 2026-07-28)"
check_eq "21:07 Paris on 2026-07-28 (one-shot) — the missed ACGL-prep wake" "07 19 28 7 *" "$OUT"

# ── B. UTC-host (VPS) simulation: winter (CET, UTC+1) known-answer pairs ────
echo ""
echo "B. TZ=UTC (simulated VPS host), winter (CET, UTC+1) — proves DST is derived, not hardcoded"
OUT="$(TZ=UTC "${RUNNER[@]}" 08:03 one-shot 2026-01-15)"
check_eq "08:03 Paris on 2026-01-15 (one-shot, winter)" "03 07 15 1 *" "$OUT"

OUT="$(TZ=UTC "${RUNNER[@]}" 21:00 one-shot 2026-12-24)"
check_eq "21:00 Paris on 2026-12-24 (one-shot, winter)" "00 20 24 12 *" "$OUT"

# ── C. UTC-host (VPS) simulation: real 2026 EU DST transition-day edges ─────
echo ""
echo "C. TZ=UTC (simulated VPS host), DST transition-day edges (2026)"
OUT="$(TZ=UTC "${RUNNER[@]}" 08:03 one-shot 2026-03-28)"
check_eq "08:03 Paris on 2026-03-28 (day before spring-forward, still CET)" "03 07 28 3 *" "$OUT"

OUT="$(TZ=UTC "${RUNNER[@]}" 08:03 one-shot 2026-03-29)"
check_eq "08:03 Paris on 2026-03-29 (spring-forward day itself, now CEST)" "03 06 29 3 *" "$OUT"

OUT="$(TZ=UTC "${RUNNER[@]}" 08:03 one-shot 2026-10-24)"
check_eq "08:03 Paris on 2026-10-24 (day before fall-back, still CEST)" "03 06 24 10 *" "$OUT"

OUT="$(TZ=UTC "${RUNNER[@]}" 08:03 one-shot 2026-10-25)"
check_eq "08:03 Paris on 2026-10-25 (fall-back day itself, now CET)" "03 07 25 10 *" "$OUT"

# ── D. Europe/Paris-host (Mac) simulation: Paris -> Paris-local is a no-op ──
echo ""
echo "D. TZ=Europe/Paris (simulated Mac host) — Paris -> host-local is a no-op (#1510 fix)"
OUT="$(TZ=Europe/Paris "${RUNNER[@]}" 08:03 one-shot 2026-07-28)"
check_eq "08:03 Paris on 2026-07-28, host already Europe/Paris (one-shot, summer)" "03 08 28 7 *" "$OUT"

OUT="$(TZ=Europe/Paris "${RUNNER[@]}" 08:03 one-shot 2026-01-15)"
check_eq "08:03 Paris on 2026-01-15, host already Europe/Paris (one-shot, winter)" "03 08 15 1 *" "$OUT"

OUT="$(TZ=Europe/Paris "${RUNNER[@]}" 08:03 daily)"
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
OUT="$(TZ=UTC "${RUNNER[@]}" 08:03 daily)"
check_eq "08:03 Paris today (daily), live UTC offset is ${CUR_OFFSET_HOURS}h" "${EXPECT_MIN} ${EXPECT_HOUR_STR} * * *" "$OUT"

# ── F. input validation ──────────────────────────────────────────────────────
echo ""
echo "F. input validation fails loudly instead of guessing"
"${RUNNER[@]}" 25:99 daily >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "bad HH:MM rejected (non-zero exit)" || bad "bad HH:MM silently accepted"

"${RUNNER[@]}" 08:03 weekly >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "bad mode rejected (non-zero exit)" || bad "bad mode silently accepted"

"${RUNNER[@]}" 08:03 one-shot "not-a-date" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "bad explicit date rejected (non-zero exit)" || bad "bad explicit date silently accepted"

"${RUNNER[@]}" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "missing required arg rejected (non-zero exit)" || bad "missing arg silently accepted"

# ── G. regression guard: the ORIGINAL wrong mapping must not reappear ────────
echo ""
echo "G. regression guard — the incident's wrong Paris-as-box-local mapping is gone"
OUT="$(TZ=UTC "${RUNNER[@]}" 08:03 daily)"
if [[ "$OUT" == "3 8 * * *" || "$OUT" == "03 08 * * *" ]]; then
  bad "helper reproduced the WRONG naive mapping (08:03 Paris treated as 08:03 box-UTC): '$OUT'"
else
  ok "helper does NOT reproduce the wrong naive Paris-as-UTC mapping (got '$OUT', not '3 8 * * *')"
fi

# ── H. Fix #1515: unknown/invalid host TZ fails loud, never silent-UTC ──────
echo ""
echo "H. TZ validation (#1515) — unknown TZ fails loud instead of silently defaulting to UTC"

OUT="$(TZ=Bogus/Nonexistent "${RUNNER[@]}" 08:03 one-shot 2>/dev/null)"
RC=$?
if [[ $RC -ne 0 && -z "$OUT" ]]; then
  ok "bogus TZ (one-shot) rejected: non-zero exit ($RC), empty stdout"
else
  bad "bogus TZ (one-shot): expected non-zero exit + empty stdout, got exit=$RC stdout='$OUT'"
fi

ERR="$(TZ=Bogus/Nonexistent "${RUNNER[@]}" 08:03 one-shot 2>&1 >/dev/null)"
[[ -n "$ERR" ]] && ok "bogus TZ (one-shot) prints a stderr message ('$ERR')" || bad "bogus TZ (one-shot) printed no stderr message"

OUT="$(TZ=Bogus/Nonexistent "${RUNNER[@]}" 08:03 daily 2>/dev/null)"
RC=$?
if [[ $RC -ne 0 && -z "$OUT" ]]; then
  ok "bogus TZ (daily) rejected: non-zero exit ($RC), empty stdout"
else
  bad "bogus TZ (daily): expected non-zero exit + empty stdout, got exit=$RC stdout='$OUT'"
fi

# UTC and Europe/Paris must behave EXACTLY as before this fix (regression
# guard for the fix itself — a validator that's too strict is its own bug).
OUT="$(TZ=UTC "${RUNNER[@]}" 08:03 one-shot 2026-07-28)"
check_eq "TZ=UTC still works unchanged (one-shot)" "03 06 28 7 *" "$OUT"

OUT="$(TZ=UTC "${RUNNER[@]}" 08:03 daily)"
check_eq "TZ=UTC still works unchanged (daily)" "${EXPECT_MIN} ${EXPECT_HOUR_STR} * * *" "$OUT"

OUT="$(TZ=Europe/Paris "${RUNNER[@]}" 08:03 one-shot 2026-07-28)"
check_eq "TZ=Europe/Paris still works unchanged (one-shot)" "03 08 28 7 *" "$OUT"

OUT="$(TZ=Europe/Paris "${RUNNER[@]}" 08:03 daily)"
check_eq "TZ=Europe/Paris still works unchanged (daily)" "03 08 * * *" "$OUT"

# Empty/unset TZ must still fall through to the system zone and work.
OUT="$(env -u TZ "${RUNNER[@]}" 08:03 daily 2>/dev/null)"
RC=$?
[[ $RC -eq 0 && -n "$OUT" ]] && ok "unset TZ still works (daily) -> '$OUT'" || bad "unset TZ failed: exit=$RC stdout='$OUT'"

OUT="$(TZ= "${RUNNER[@]}" 08:03 daily 2>/dev/null)"
RC=$?
[[ $RC -eq 0 && -n "$OUT" ]] && ok "empty TZ still works (daily) -> '$OUT'" || bad "empty TZ failed: exit=$RC stdout='$OUT'"

# ── J. Board #1529: a missing conversion helper fails loud ──────────────────
echo ""
echo "J. missing conversion helper (#1529) fails loud — non-zero exit, empty stdout"

if [[ -f "$HELPER" ]]; then
  HELPER_BAK="$(mktemp)"
  mv "$HELPER" "$HELPER_BAK"
  restore_helper() { [[ -f "$HELPER_BAK" ]] && mv "$HELPER_BAK" "$HELPER"; }
  trap restore_helper EXIT

  ERRFILE="$(mktemp)"
  OUT="$("${RUNNER[@]}" 08:03 daily 2>"$ERRFILE")"
  RC=$?
  ERR="$(cat "$ERRFILE" 2>/dev/null)"
  rm -f "$ERRFILE"

  if [[ $RC -ne 0 && -z "$OUT" ]]; then
    ok "missing helper rejected: non-zero exit ($RC), empty stdout"
  else
    bad "missing helper: expected non-zero exit + empty stdout, got exit=$RC stdout='$OUT'"
  fi
  [[ -n "$ERR" ]] && ok "missing helper prints a stderr message ('$ERR')" || bad "missing helper printed no stderr message"

  restore_helper
  trap - EXIT
else
  bad "expected conversion helper not found at $HELPER — cannot exercise the missing-helper case"
fi

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
