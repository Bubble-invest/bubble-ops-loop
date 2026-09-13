#!/usr/bin/env bash
set -uo pipefail
RUNNER="${1:?runner required}"
ROOT="$(cd "$(dirname "$RUNNER")/../.." && pwd)"
WORK="$(mktemp -d "$HOME/.local-floor-test.XXXXXX")"
cleanup() { find "$WORK" -depth -delete; }
trap cleanup EXIT
PASS=0; FAIL=0
ok(){ echo "PASS: $1"; PASS=$((PASS+1)); }
bad(){ echo "FAIL: $1" >&2; FAIL=$((FAIL+1)); }
run(){ /bin/bash "$RUNNER" "$@"; }
NOW=2000000000
export LOCAL_LOOP_NOW_EPOCH="$NOW"
DEPT="$WORK/dept"; STATE="$WORK/state"; mkdir -p "$DEPT/outputs" "$STATE"; chmod 700 "$STATE"
TMUX="$WORK/tmux"; cat >"$TMUX" <<'TMUX'
#!/bin/sh
[ "$1" = has-session ] && exit "${TMUX_FAIL:-0}"
exit 1
TMUX
chmod 700 "$TMUX"
SELECTOR="$WORK/harness-fixture"
base=(--dept-dir "$DEPT" --slug fixture --telegram-state-dir "$STATE" --session-name ops-loop-fixture --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --stale-sec 5400 --cooldown-sec 900)

# Missing heartbeat is stale. Without explicit injection activation it is visible/non-green.
run "${base[@]}" >"$WORK/dry.log" 2>&1; rc=$?
[[ "$rc" -ne 0 ]] && grep -q 'DEFERRED' "$WORK/dry.log" && [[ ! -e "$STATE/inject" ]] && ok "stale inactive is deferred" || bad "stale inactive contract"

# Active stale run appends once, chmods private, and records cooldown.
run "${base[@]}" --activate-inject >"$WORK/first.log" 2>&1; rc=$?
lines=$(wc -l <"$STATE/inject" | tr -d ' ')
mode=$(stat -f '%Lp' "$STATE/inject")
[[ "$rc" -eq 0 && "$lines" -eq 1 && "$mode" = 600 ]] && grep -q 'WAKE_APPENDED_UNCONFIRMED' "$WORK/first.log" && ok "stale appends one private unconfirmed wake" || bad "first injection"
run "${base[@]}" --activate-inject >"$WORK/second.log" 2>&1; rc=$?
lines2=$(wc -l <"$STATE/inject" | tr -d ' ')
[[ "$rc" -eq 0 && "$lines2" -eq 1 ]] && grep -q 'COOLDOWN' "$WORK/second.log" && ok "cooldown suppresses duplicate" || bad "cooldown"
LOCAL_LOOP_NOW_EPOCH=$((NOW+901)) run "${base[@]}" --activate-inject >"$WORK/third.log" 2>&1; rc=$?
lines3=$(wc -l <"$STATE/inject" | tr -d ' ')
[[ "$rc" -eq 0 && "$lines3" -eq 2 ]] && ok "fake clock releases cooldown" || bad "fake clock cooldown"

# Fresh heartbeat never injects even with activation.
FRESH="$WORK/fresh"; mkdir -p "$FRESH/outputs/day"
python3 - "$NOW" "$FRESH/outputs/day/heartbeat.log" <<'PY'
import datetime,sys
n=int(sys.argv[1]); t=datetime.datetime.fromtimestamp(n,datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
open(sys.argv[2],'w').write(t+' tick idle\n')
PY
before=$(wc -l <"$STATE/inject")
LOCAL_LOOP_NOW_EPOCH="$NOW" run --dept-dir "$FRESH" --slug fixture --telegram-state-dir "$STATE" --session-name ops-loop-fixture --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --stale-sec 5400 --cooldown-sec 900 --activate-inject >"$WORK/fresh.log" 2>&1; rc=$?
after=$(wc -l <"$STATE/inject")
[[ "$rc" -eq 0 && "$before" = "$after" ]] && grep -q 'FRESH' "$WORK/fresh.log" && ok "fresh never injects" || bad "fresh contract"

# Harness switch is resolved every run. Hermes uses its live gateway helper
# and must never receive a stale Claude-channel wake.
HERMES_PY="$WORK/hermes-python"
HERMES_HELPER="$WORK/hermes-helper.py"
HERMES_ROOT="$WORK/hermes-root"
HERMES_PROFILE="$WORK/hermes-profile"
mkdir -p "$HERMES_ROOT" "$HERMES_PROFILE"
: >"$HERMES_HELPER"
cat >"$HERMES_PY" <<EOF_HERMES
#!/bin/sh
printf '%s\n' "\$@" >"$WORK/hermes-args"
cat >"$WORK/hermes-prompt"
: >"$WORK/hermes-called"
exit "\${HERMES_FAIL:-0}"
EOF_HERMES
chmod 700 "$HERMES_PY"
printf 'hermes' >"$SELECTOR"
before=$(wc -l <"$STATE/inject")
LOCAL_LOOP_HERMES_PY="$HERMES_PY" LOCAL_LOOP_HERMES_WAKE_HELPER="$HERMES_HELPER" LOCAL_LOOP_HERMES_ROOT="$HERMES_ROOT" LOCAL_LOOP_HERMES_PROFILE_HOME="$HERMES_PROFILE" LOCAL_LOOP_NOW_EPOCH=$((NOW+2000)) run "${base[@]}" --activate-inject >"$WORK/hermes.log" 2>&1; rc=$?
[[ "$rc" -eq 0 && "$(wc -l <"$STATE/inject")" = "$before" && -e "$WORK/hermes-called" ]] \
  && grep -q 'HERMES_WAKE_QUEUED_UNCONFIRMED' "$WORK/hermes.log" \
  && grep -q '^Resume your OODA loop' "$WORK/hermes-prompt" \
  && grep -Fxq -- '--profile-home' "$WORK/hermes-args" \
  && grep -Fxq -- "$HERMES_PROFILE" "$WORK/hermes-args" \
  && ok "Hermes selector calls live helper without writing Claude inbox" || bad "Hermes routing"
printf ' \n hermes\t ' >"$SELECTOR"
rm -f "$WORK/hermes-called"
LOCAL_LOOP_HERMES_PY="$HERMES_PY" LOCAL_LOOP_HERMES_WAKE_HELPER="$HERMES_HELPER" LOCAL_LOOP_HERMES_ROOT="$HERMES_ROOT" LOCAL_LOOP_HERMES_PROFILE_HOME="$HERMES_PROFILE" LOCAL_LOOP_NOW_EPOCH=$((NOW+3000)) run "${base[@]}" --activate-inject >"$WORK/hermes-space.log" 2>&1; rc=$?
[[ "$rc" -eq 0 && "$(wc -l <"$STATE/inject")" = "$before" && -e "$WORK/hermes-called" ]] && grep -q 'HERMES_WAKE_QUEUED_UNCONFIRMED' "$WORK/hermes-space.log" && ok "whitespace Hermes selector calls helper" || bad "whitespace Hermes routing"

# Helper failure is visible/nonzero and still cannot touch Claude's inbox.
rm -f "$WORK/hermes-called"
HERMES_FAIL=7 LOCAL_LOOP_HERMES_PY="$HERMES_PY" LOCAL_LOOP_HERMES_WAKE_HELPER="$HERMES_HELPER" LOCAL_LOOP_HERMES_ROOT="$HERMES_ROOT" LOCAL_LOOP_HERMES_PROFILE_HOME="$HERMES_PROFILE" LOCAL_LOOP_NOW_EPOCH=$((NOW+4000)) run "${base[@]}" --activate-inject >"$WORK/hermes-fail.log" 2>&1; rc=$?
[[ "$rc" -eq 7 && "$(wc -l <"$STATE/inject")" = "$before" ]] && grep -q 'Hermes wake helper failed (exit=7)' "$WORK/hermes-fail.log" && ok "Hermes helper failure is visible and nonzero" || bad "Hermes helper failure"

# A fresh heartbeat exits before either harness path (no helper and no inject).
rm -f "$WORK/hermes-called"
before=$(wc -l <"$STATE/inject")
LOCAL_LOOP_HERMES_PY="$HERMES_PY" LOCAL_LOOP_HERMES_WAKE_HELPER="$HERMES_HELPER" LOCAL_LOOP_HERMES_ROOT="$HERMES_ROOT" LOCAL_LOOP_HERMES_PROFILE_HOME="$HERMES_PROFILE" LOCAL_LOOP_NOW_EPOCH="$NOW" run --dept-dir "$FRESH" --slug fixture --telegram-state-dir "$STATE" --session-name ops-loop-fixture --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --stale-sec 5400 --cooldown-sec 900 --activate-inject >"$WORK/fresh-hermes.log" 2>&1; rc=$?
[[ "$rc" -eq 0 && ! -e "$WORK/hermes-called" && "$(wc -l <"$STATE/inject")" = "$before" ]] && grep -q 'FRESH' "$WORK/fresh-hermes.log" && ok "fresh heartbeat calls neither harness wake" || bad "fresh harness gate"
printf 'claude\n' >"$SELECTOR"

# Missing session and unsafe state/target shapes fail without appending.
before=$(wc -l <"$STATE/inject")
TMUX_FAIL=1 run "${base[@]}" --activate-inject >"$WORK/no-session.log" 2>&1; rc=$?
[[ "$rc" -ne 0 && "$(wc -l <"$STATE/inject")" = "$before" ]] && ok "missing session fails closed" || bad "session failure"
UNSAFE="$WORK/unsafe"; mkdir "$UNSAFE"; chmod 777 "$UNSAFE"
run --dept-dir "$DEPT" --slug fixture --telegram-state-dir "$UNSAFE" --session-name x --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --activate-inject >"$WORK/unsafe.log" 2>&1; rc=$?
[[ "$rc" -ne 0 && ! -e "$UNSAFE/inject" ]] && ok "writable-peer state rejected" || bad "unsafe state"
REAL="$WORK/real"; mkdir "$REAL"; chmod 700 "$REAL"; LINK="$WORK/state-link"; ln -s "$REAL" "$LINK"
run --dept-dir "$DEPT" --slug fixture --telegram-state-dir "$LINK" --session-name x --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --activate-inject >"$WORK/link.log" 2>&1; rc=$?
[[ "$rc" -ne 0 && ! -e "$REAL/inject" ]] && ok "symlink state rejected" || bad "symlink state"
FIFO="$WORK/fifo"; mkdir "$FIFO"; chmod 700 "$FIFO"; mkfifo "$FIFO/inject"
run --dept-dir "$DEPT" --slug fixture --telegram-state-dir "$FIFO" --session-name x --harness-selector "$SELECTOR" --tmux-bin "$TMUX" --activate-inject >"$WORK/fifo.log" 2>&1; rc=$?
[[ "$rc" -ne 0 ]] && ok "nonregular inject rejected" || bad "fifo inject"

# Legacy activation can never call a model binary.
TRIP="$WORK/claude"; cat >"$TRIP" <<EOF2
#!/bin/sh
touch "$WORK/MODEL_CALLED"
EOF2
chmod 700 "$TRIP"
PATH="$WORK:$PATH" run "${base[@]}" --claude-bin "$TRIP" --activate-tick >"$WORK/legacy.log" 2>&1; rc=$?
[[ "$rc" -eq 64 && ! -e "$WORK/MODEL_CALLED" ]] && ok "legacy model activation disabled" || bad "legacy activation"
! grep -q '\$CLAUDE_BIN.*dangerously\|"\$CLAUDE_BIN"' "$RUNNER" && ok "runner has no model execution" || bad "model execution remains"

echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
