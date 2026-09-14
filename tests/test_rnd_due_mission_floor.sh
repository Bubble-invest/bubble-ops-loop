#!/usr/bin/env bash
set -uo pipefail

RUNNER="${1:?runner required}"
ROOT="$(cd "$(dirname "$RUNNER")/../.." && pwd)"
WORK="$(mktemp -d "$HOME/.rnd-due-floor-test.XXXXXX")"
cleanup() { find "$WORK" -depth -delete; }
trap cleanup EXIT

PASS=0
FAIL=0
ok() { echo "PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "FAIL: $1" >&2; FAIL=$((FAIL + 1)); }
run() { /bin/bash "$RUNNER" "$@"; }

NOW=1789300800 # 2026-09-13T12:00:00Z
DEPT="$WORK/rnd"
STATE="$WORK/state"
mkdir -p "$DEPT/missions" "$DEPT/layers"/{1,2,3,4} "$DEPT/outputs" "$STATE"
chmod 700 "$STATE"
for layer in 1 2 3 4; do printf '# layer %s\n' "$layer" >"$DEPT/layers/$layer/PROMPT.md"; done
for mission in kanban-board daily-scan weekly-scan monthly-scan wiki-compile website-guardian; do
    printf '# %s\n' "$mission" >"$DEPT/missions/$mission.md"
done
cat >"$DEPT/dept.yaml" <<'YAML'
loop:
  due_dispatch:
    mission_ids: [kanban_board, daily_scan, weekly_scan, monthly_scan, wiki_compile, planned_scan]
    watermark: monitoring/due-mission-watermarks.json
    pending_lease_seconds: 21600
layers:
  subscribed: [1, 2, 3, 4]
recurring_missions:
- id: kanban_board
  layer: [1, 2, 3, 4]
  status: live
  cadence: continuous
  due: {policy: every_tick}
  mission_file: missions/kanban-board.md
- id: daily_scan
  layer: 1
  status: live
  cadence: daily
  due: {policy: calendar_period, timezone: Europe/Paris}
  mission_file: missions/daily-scan.md
- id: weekly_scan
  layer: 4
  status: live
  cadence: weekly
  due: {policy: calendar_period, timezone: Europe/Paris}
  mission_file: missions/weekly-scan.md
- id: monthly_scan
  layer: 2
  status: live
  cadence: monthly
  due: {policy: calendar_period, timezone: Europe/Paris}
  mission_file: missions/monthly-scan.md
- id: wiki_compile
  layer: 4
  status: live
  cadence: continuous
  due: {policy: every_tick}
  mission_file: missions/wiki-compile.md
# planned_scan is IN mission_ids scope but status: planned — the #1317 status
# filter must skip it (never dispatched, never leased) even though it is due by
# cadence. Its mission file deliberately does NOT exist, proving a not-yet-built
# planned mission cannot crash the floor or starve the live missions.
- id: planned_scan
  layer: 1
  status: planned
  cadence: weekly
  due: {policy: calendar_period, timezone: Europe/Paris}
  mission_file: missions/planned-scan.md
# website_guardian is intentionally NOT status: live AND out of mission_ids
# scope — it must never be scheduled (double-checks the scope + status gates).
- id: website_guardian
  layer: 1
  status: planned
  cadence: weekly
  mission_file: missions/website-guardian.md
YAML

TMUX="$WORK/tmux"
cat >"$TMUX" <<'TMUX'
#!/bin/sh
[ "$1" = has-session ] && exit 0
exit 1
TMUX
chmod 700 "$TMUX"
SELECTOR="$WORK/harness-rnd"
printf 'claude\n' >"$SELECTOR"
chmod 600 "$SELECTOR"
base=(--dept-dir "$DEPT" --slug rnd --telegram-state-dir "$STATE" --session-name ops-loop-rnd --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --stale-sec 5400 --cooldown-sec 900 --activate-inject)

LOCAL_LOOP_NOW_EPOCH="$NOW" run "${base[@]}" >"$WORK/first.log" 2>&1
rc=$?
prompt="$(tail -n 1 "$STATE/inject" 2>/dev/null)"
if [[ "$rc" -eq 0 ]] \
    && [[ "$prompt" == *'DUE_MISSIONS=[kanban_board{'* ]] \
    && [[ "$prompt" == *'daily_scan{cadence=daily,period=2026-09-13'* ]] \
    && [[ "$prompt" == *'weekly_scan{cadence=weekly,period=2026-W37'* ]] \
    && [[ "$prompt" == *'monthly_scan{cadence=monthly,period=2026-09'* ]] \
    && [[ "$prompt" == *'wiki_compile{cadence=continuous,period=continuous'* ]]; then
    ok "stale floor tick injects the exact continuous/daily/weekly/monthly due missions"
else
    bad "first due-mission prompt"
fi
[[ "$prompt" != *website_guardian* ]] && ok "mission outside explicit M1-M8-style scope is not scheduled" || bad "unscoped mission leaked into plan"
# #1317: an IN-scope status:planned mission is skipped by the status filter and
# never leased, even though it is due by cadence and its file does not exist.
[[ "$prompt" != *planned_scan* ]] && ok "in-scope status:planned mission is skipped by the #1317 status filter" || bad "planned mission leaked into plan"
if [[ ! -f "$DEPT/monitoring/due-mission-watermarks.json" ]] || ! grep -q planned_scan "$DEPT/monitoring/due-mission-watermarks.json"; then
    ok "status:planned mission is never leased in the watermark"
else
    bad "planned mission got a pending lease"
fi
# The skip is not hidden: the one-line stderr notice naming the skipped mission
# must survive the wrapper's stdout-only $(...) capture and reach the log.
grep -q 'due-mission notice.*planned_scan' "$WORK/first.log" \
    && ok "skip notice for the planned mission reaches the log via stderr" \
    || bad "skip notice did not reach the log"
[[ "$prompt" == *'COMPLETE weekly_scan => '* ]] \
    && [[ "$prompt" != *'=> python3 '* ]] \
    && [[ "$prompt" == *'due_missions.py complete'* ]] \
    && [[ "$prompt" == *'only after that mission actually succeeds'* ]] \
    && [[ "$prompt" == *'inbox-accepted work'* ]] \
    && ok "prompt carries per-mission success-only completion protocol, pinned to an absolute interpreter (#1330 — not bare 'python3')" \
    || bad "completion protocol missing"

python3 "$ROOT/scripts/due_missions.py" complete --dept-dir "$DEPT" \
    --mission weekly_scan --period 2026-W37 --now-epoch "$NOW" >"$WORK/complete.log" 2>&1
rc=$?
[[ "$rc" -eq 0 ]] && grep -q 'completed weekly_scan for 2026-W37' "$WORK/complete.log" \
    && ok "explicit successful completion advances the weekly watermark" \
    || bad "completion command"

LOCAL_LOOP_NOW_EPOCH=$((NOW + 901)) run "${base[@]}" >"$WORK/second.log" 2>&1
rc=$?
second="$(tail -n 1 "$STATE/inject" 2>/dev/null)"
if [[ "$rc" -eq 0 ]] \
    && [[ "$second" == *'kanban_board{cadence=continuous'* ]] \
    && [[ "$second" == *'wiki_compile{cadence=continuous'* ]] \
    && [[ "$second" != *weekly_scan* ]]; then
    ok "same-period weekly success is suppressed while continuous missions remain"
else
    bad "same-period idempotence"
fi

# A healthy M1 heartbeat must not starve slower work. A due weekly mission
# overrides freshness once; after explicit completion, continuous-only work
# falls back under the normal stale gate and the 5m wake-catch stays quiet.
FRESH_DUE="$WORK/fresh-due"
FRESH_STATE="$WORK/fresh-state"
mkdir -p "$FRESH_DUE/missions" "$FRESH_DUE/layers"/{1,2,3,4} \
    "$FRESH_DUE/outputs/2026-09-13" "$FRESH_STATE"
chmod 700 "$FRESH_STATE"
for layer in 1 2 3 4; do printf '# layer\n' >"$FRESH_DUE/layers/$layer/PROMPT.md"; done
printf '# board\n' >"$FRESH_DUE/missions/board.md"
printf '# weekly\n' >"$FRESH_DUE/missions/weekly.md"
cat >"$FRESH_DUE/dept.yaml" <<'YAML'
loop:
  due_dispatch:
    mission_ids: [board, weekly]
    watermark: monitoring/due.json
    pending_lease_seconds: 21600
layers:
  subscribed: [1, 2, 3, 4]
recurring_missions:
- id: board
  layer: [1, 2, 3, 4]
  status: live
  cadence: continuous
  due: {policy: every_tick}
  mission_file: missions/board.md
- id: weekly
  layer: 4
  status: live
  cadence: weekly
  due: {policy: calendar_period, timezone: Europe/Paris}
  mission_file: missions/weekly.md
YAML
python3 - "$NOW" "$FRESH_DUE/outputs/2026-09-13/heartbeat.log" <<'PY'
import datetime, sys
stamp = datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc)
open(sys.argv[2], "w").write(stamp.strftime("%Y-%m-%dT%H:%M:%SZ") + " tick healthy\n")
PY
fresh_args=(--dept-dir "$FRESH_DUE" --slug rnd --telegram-state-dir "$FRESH_STATE" --session-name ops-loop-rnd --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --stale-sec 5400 --cooldown-sec 900 --activate-inject)
LOCAL_LOOP_NOW_EPOCH="$NOW" run "${fresh_args[@]}" >"$WORK/fresh-due.log" 2>&1
rc=$?
[[ "$rc" -eq 0 && -s "$FRESH_STATE/inject" ]] \
    && grep -q 'heartbeat FRESH but periodic mission due' "$WORK/fresh-due.log" \
    && grep -q 'weekly{cadence=weekly,period=2026-W37' "$FRESH_STATE/inject" \
    && ok "fresh heartbeat cannot starve a due weekly mission" \
    || bad "fresh heartbeat periodic override"

before_lines="$(wc -l <"$FRESH_STATE/inject" | tr -d ' ')"
LOCAL_LOOP_NOW_EPOCH=$((NOW + 901)) run "${fresh_args[@]}" >"$WORK/fresh-pending.log" 2>&1
rc=$?
after_lines="$(wc -l <"$FRESH_STATE/inject" | tr -d ' ')"
[[ "$rc" -eq 0 && "$before_lines" = "$after_lines" ]] \
    && grep -q 'heartbeat FRESH.*no periodic mission due' "$WORK/fresh-pending.log" \
    && ok "uncompleted weekly lease suppresses duplicate after inbox cooldown" \
    || bad "pending lease duplicate suppression"

# Keep the heartbeat fresh at lease expiry: the expired weekly claim must
# become due and override freshness again.
RETRY_NOW=$((NOW + 21601))
python3 - "$RETRY_NOW" "$FRESH_DUE/outputs/2026-09-13/heartbeat.log" <<'PY'
import datetime, sys
stamp = datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc)
open(sys.argv[2], "w").write(stamp.strftime("%Y-%m-%dT%H:%M:%SZ") + " tick healthy\n")
PY
before_lines="$(wc -l <"$FRESH_STATE/inject" | tr -d ' ')"
LOCAL_LOOP_NOW_EPOCH="$RETRY_NOW" run "${fresh_args[@]}" >"$WORK/fresh-expired.log" 2>&1
rc=$?
after_lines="$(wc -l <"$FRESH_STATE/inject" | tr -d ' ')"
[[ "$rc" -eq 0 && "$after_lines" -eq $((before_lines + 1)) ]] \
    && grep -q 'weekly{cadence=weekly,period=2026-W37' "$FRESH_STATE/inject" \
    && ok "expired uncompleted weekly lease retries on the next floor invocation" \
    || bad "pending lease expiry retry"

python3 "$ROOT/scripts/due_missions.py" complete --dept-dir "$FRESH_DUE" \
    --mission weekly --period 2026-W37 --now-epoch "$RETRY_NOW" >/dev/null
before_lines="$(wc -l <"$FRESH_STATE/inject" | tr -d ' ')"
LOCAL_LOOP_NOW_EPOCH=$((RETRY_NOW + 60)) run "${fresh_args[@]}" >"$WORK/fresh-done.log" 2>&1
rc=$?
after_lines="$(wc -l <"$FRESH_STATE/inject" | tr -d ' ')"
[[ "$rc" -eq 0 && "$before_lines" = "$after_lines" ]] \
    && grep -q 'heartbeat FRESH.*no periodic mission due' "$WORK/fresh-done.log" \
    && ok "completed weekly plus fresh heartbeat skips continuous-only wake" \
    || bad "fresh continuous-only stale gate"

# A wake failure must release only the claims it just made, so the very next
# invocation can retry instead of waiting six hours.
FAIL_DEPT="$WORK/fail-release"
FAIL_STATE="$WORK/fail-state"
mkdir -p "$FAIL_DEPT/missions" "$FAIL_DEPT/layers"/{1,2,3,4} "$FAIL_DEPT/outputs" "$FAIL_STATE"
chmod 700 "$FAIL_STATE"
for layer in 1 2 3 4; do printf '# layer\n' >"$FAIL_DEPT/layers/$layer/PROMPT.md"; done
printf '# weekly\n' >"$FAIL_DEPT/missions/weekly.md"
cat >"$FAIL_DEPT/dept.yaml" <<'YAML'
loop:
  due_dispatch:
    mission_ids: [weekly]
    watermark: monitoring/due.json
    pending_lease_seconds: 21600
layers:
  subscribed: [1, 2, 3, 4]
recurring_missions:
- id: weekly
  layer: 4
  status: live
  cadence: weekly
  due: {policy: calendar_period, timezone: Europe/Paris}
  mission_file: missions/weekly.md
YAML
TMUX_BAD="$WORK/tmux-bad"
printf '#!/bin/sh\nexit 1\n' >"$TMUX_BAD"
chmod 700 "$TMUX_BAD"
fail_args=(--dept-dir "$FAIL_DEPT" --slug rnd --telegram-state-dir "$FAIL_STATE" --session-name ops-loop-rnd --harness-selector "$SELECTOR" --tmux-bin "$TMUX_BAD" --activate-inject)
LOCAL_LOOP_NOW_EPOCH="$NOW" run "${fail_args[@]}" >"$WORK/inject-fail.log" 2>&1
rc=$?
[[ "$rc" -ne 0 && ! -e "$FAIL_STATE/inject" ]] || bad "fixture wake failure did not fail"
fail_args=(--dept-dir "$FAIL_DEPT" --slug rnd --telegram-state-dir "$FAIL_STATE" --session-name ops-loop-rnd --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --activate-inject)
LOCAL_LOOP_NOW_EPOCH=$((NOW + 1)) run "${fail_args[@]}" >"$WORK/inject-retry.log" 2>&1
rc=$?
[[ "$rc" -eq 0 && -s "$FAIL_STATE/inject" ]] \
    && grep -q 'weekly{cadence=weekly' "$FAIL_STATE/inject" \
    && ok "failed wake releases its claim so the next invocation retries" \
    || bad "failed wake claim release"

# A configured scope with a missing due rule must fail before appending a wake.
BAD="$WORK/bad"
BAD_STATE="$WORK/bad-state"
mkdir -p "$BAD/missions" "$BAD/layers"/{1,2,3,4} "$BAD/outputs" "$BAD_STATE"
chmod 700 "$BAD_STATE"
for layer in 1 2 3 4; do printf '# layer\n' >"$BAD/layers/$layer/PROMPT.md"; done
printf '# mission\n' >"$BAD/missions/broken.md"
cat >"$BAD/dept.yaml" <<'YAML'
loop:
  due_dispatch:
    mission_ids: [broken]
    watermark: monitoring/due.json
    pending_lease_seconds: 21600
layers:
  subscribed: [1, 2, 3, 4]
recurring_missions:
# status: live so the missing-due-rule still fails closed and raises — the
# #1317 status filter must NOT mask a broken LIVE mission (a planned one WOULD
# be silently skipped; that split is covered by the unit tests).
- id: broken
  layer: 1
  status: live
  cadence: weekly
  mission_file: missions/broken.md
YAML
bad_args=(--dept-dir "$BAD" --slug rnd --telegram-state-dir "$BAD_STATE" --session-name ops-loop-rnd --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --activate-inject)
LOCAL_LOOP_NOW_EPOCH="$NOW" run "${bad_args[@]}" >"$WORK/bad.log" 2>&1
rc=$?
[[ "$rc" -ne 0 && ! -e "$BAD_STATE/inject" ]] \
    && grep -q 'missing due rule' "$WORK/bad.log" \
    && ok "invalid configured due rule fails closed without an injection" \
    || bad "invalid rule fail-closed contract"

echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
