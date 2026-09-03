#!/usr/bin/env bash
# =============================================================================
# test_loop_tick_watchdog.sh — end-to-end harness for scripts/loop-tick-watchdog.py
# (board #724). The pure decision is covered by
# scripts/lib/tests/test_loop_tick_watchdog.py; THIS harness proves the RUNNER's
# side effects + discovery, hermetically:
#
#   E1  STALLED dept  → exactly ONE line appended to its inject file, a
#       kick-inject event in the state log, ONE notify — and NO restart.
#   E2  HEALTHY-IDLE dept (last turn ended normally, 8h ago) → inject file
#       untouched, no event, no notify, no restart.
#   E3  CRASH-LOOP: 3 kicks already in the window → the 4th pass does NOT
#       inject; it escalates (hold-guardrail) ONCE and stays quiet after.
#   E4  DRY_RUN on a stalled dept → decides + logs, touches nothing.
#   E5  Stalled but session NOT alive → no inject (nothing to inject into),
#       alert instead (the supervisor owns process death; never double-launch).
#   E6  Inject proved deaf (same error line after the cooldown) → level-2
#       RESTART hook fires exactly once; forbidden when the runtime lacks
#       --continue (alert instead).
#   E7  Mac discovery: a fake ~/Library/LaunchAgents with
#       com.bubble.ops-loop-<slug>.plist (+ -backup- to be skipped) + wrapper
#       → correct slug / dept_dir / session_dir / inject / --continue.
#   E8  The inject payload is ONE line and does not start with "/".
#
# `claude` is never launched; restart/notify/alive are stub commands that
# leave tripwire files. Runs on macOS + Linux (python3 stdlib only).
#
# Run:  bash tests/test_loop_tick_watchdog.sh [-v]
# =============================================================================
set -uo pipefail
VERBOSE=0; [[ "${1:-}" == "-v" ]] && VERBOSE=1

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
RUNNER="$REPO_ROOT/scripts/loop-tick-watchdog.py"
[[ -f "$RUNNER" ]] || { echo "FATAL: runner not found: $RUNNER"; exit 2; }
PY="$(command -v python3)"

PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }
chk_eq() { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi; }
want()   { if grep -q -- "$2" "$3" 2>/dev/null; then ok "$1"; else bad "$1 (no '$2' in $3)"; fi; }
nowant() { if grep -q -- "$2" "$3" 2>/dev/null; then bad "$1 (unexpected '$2' in $3)"; else ok "$1"; fi; }
count_lines() { [[ -f "$1" ]] && wc -l < "$1" | tr -d ' ' || echo 0; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/tickwd.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# ── fixture helpers (real transcript shapes; see the pytest module) ──────────
NOW="$($PY -c 'import time;print(int(time.time()))')"
iso() { $PY -c 'import sys,datetime;print(datetime.datetime.fromtimestamp(int(sys.argv[1]),datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))' "$1"; }
# make_dept <slug> <kind: stalled|healthy> <age_sec> → writes dept dir, session dir (with transcript), inject file; prints JSON spec
make_dept() {
    local slug="$1" kind="$2" age="$3" resumes="${4:-true}"
    local dept="$WORK/agents/bubble-ops-$slug" sess="$WORK/projects/-agents-bubble-ops-$slug" state="$WORK/channels/telegram-$slug"
    mkdir -p "$dept" "$sess" "$state"; : > "$state/inject"
    local t0=$((NOW - age)) tx="$sess/s1.jsonl"
    {
        printf '{"type":"user","timestamp":"%s","uuid":"u1","message":{"role":"user","content":"Run my full /loop tick now (STEP A-F per CLAUDE.md)."}}\n' "$(iso $t0)"
        printf '{"type":"assistant","timestamp":"%s","uuid":"a1","message":{"role":"assistant","model":"claude-opus-4-8","content":[{"type":"tool_use","id":"b1","name":"Bash","input":{}}]}}\n' "$(iso $((t0+5)))"
        printf '{"type":"user","timestamp":"%s","uuid":"r1","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"b1","content":"ok"}]}}\n' "$(iso $((t0+6)))"
        if [[ "$kind" == "stalled" ]]; then
            printf '{"type":"assistant","timestamp":"%s","uuid":"e1","isApiErrorMessage":true,"message":{"role":"assistant","model":"<synthetic>","content":[{"type":"text","text":"API Error: 529 Overloaded. This is a server-side issue, usually temporary."}]}}\n' "$(iso $((t0+20)))"
        else
            printf '{"type":"assistant","timestamp":"%s","uuid":"c1","message":{"role":"assistant","model":"claude-opus-4-8","content":[{"type":"tool_use","id":"c1","name":"CronCreate","input":{"cron":"3 6 4 9 *"}}]}}\n' "$(iso $((t0+10)))"
            printf '{"type":"user","timestamp":"%s","uuid":"r2","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"c1","content":"ok"}]}}\n' "$(iso $((t0+11)))"
            printf '{"type":"assistant","timestamp":"%s","uuid":"a2","message":{"role":"assistant","model":"claude-opus-4-8","content":[{"type":"text","text":"Tick done, next wake armed."}]}}\n' "$(iso $((t0+20)))"
        fi
        printf '{"type":"system","subtype":"turn_duration","durationMs":205,"isMeta":false,"timestamp":"%s"}\n' "$(iso $((t0+20)))"
    } > "$tx"
    touch -t "$($PY -c 'import sys,datetime;print(datetime.datetime.fromtimestamp(int(sys.argv[1])).strftime("%Y%m%d%H%M.%S"))' $((t0+20)))" "$tx"
    printf '{"slug":"%s","dept_dir":"%s","session_dir":"%s","inject_file":"%s","host":"vps","resumes_context":%s,"env_file":"","unit":"ops-loop-%s.service","tmux_bin":"","tmux_session":"","bot_pid_file":"%s"}\n' \
        "$slug" "$dept" "$sess" "$state/inject" "$resumes" "$slug" "$state/bot.pid"
}

# ── stub hooks (tripwires) ───────────────────────────────────────────────────
STUB="$WORK/stub"; mkdir -p "$STUB"
cat > "$STUB/discover" <<EOF
#!/usr/bin/env bash
cat "$WORK/specs.jsonl"
EOF
cat > "$STUB/alive" <<EOF
#!/usr/bin/env bash
# alive unless a "dead-<slug>" marker exists
[[ -f "$WORK/dead-\$1" ]] && exit 1 || exit 0
EOF
cat > "$STUB/restart" <<EOF
#!/usr/bin/env bash
echo "\$1" >> "$WORK/restarts.log"
EOF
cat > "$STUB/notify" <<EOF
#!/usr/bin/env bash
printf '%s|%s|%s\n' "\$1" "\$2" "\$3" >> "$WORK/notify.log"
EOF
chmod +x "$STUB"/*
STATE="$WORK/state/wd.jsonl"

run_wd() {  # [extra env assignments...] — runs one pass, log → $OUT
    OUT="$WORK/run-$RANDOM.log"
    env BUBBLE_TICKWD_DISCOVER_CMD="$STUB/discover" BUBBLE_TICKWD_ALIVE_CMD="$STUB/alive" \
        BUBBLE_TICKWD_RESTART_CMD="$STUB/restart" BUBBLE_TICKWD_NOTIFY_CMD="$STUB/notify" \
        BUBBLE_TICKWD_STATE="$STATE" "$@" "$PY" "$RUNNER" --host vps >"$OUT" 2>&1
    RC=$?
    [[ "$VERBOSE" == "1" ]] && cat "$OUT"
    return 0
}
reset_state() { rm -f "$STATE" "$WORK/restarts.log" "$WORK/notify.log" "$WORK"/dead-*; : > "$WORK/specs.jsonl"; }

echo "== loop-tick-watchdog e2e =="

# ── E1: stalled → ONE inject, event, notify, no restart ─────────────────────
reset_state
make_dept stalled1 stalled 1200 > "$WORK/specs.jsonl"     # error 20 min ago
INJ="$WORK/channels/telegram-stalled1/inject"
run_wd
chk_eq "E1 exit 0" "0" "$RC"
want   "E1 decided kick-inject" "stalled1: kick-inject" "$OUT"
chk_eq "E1 exactly one inject line" "1" "$(count_lines "$INJ")"
want   "E1 inject line is the re-arm turn" "tick-watchdog" "$INJ"
chk_eq "E1 one kick-inject event recorded" "1" "$(grep -c '"action": "kick-inject"' "$STATE" 2>/dev/null || grep -c '"action":"kick-inject"' "$STATE")"
chk_eq "E1 one notify" "1" "$(count_lines "$WORK/notify.log")"
chk_eq "E1 no restart" "0" "$(count_lines "$WORK/restarts.log")"
# second pass right away: cooldown → nothing more
run_wd
want   "E1b second pass holds (cooldown)" "hold-cooldown" "$OUT"
chk_eq "E1b still exactly one inject line" "1" "$(count_lines "$INJ")"

# ── E2: healthy idle 8h → untouched ─────────────────────────────────────────
reset_state
make_dept healthy1 healthy 28800 > "$WORK/specs.jsonl"
INJ2="$WORK/channels/telegram-healthy1/inject"
run_wd
want   "E2 decided ok-idle" "healthy1: ok-idle" "$OUT"
chk_eq "E2 inject untouched" "0" "$(count_lines "$INJ2")"
[[ -f "$STATE" ]] && bad "E2 no event expected" || ok "E2 no event"
chk_eq "E2 no notify" "0" "$(count_lines "$WORK/notify.log")"
chk_eq "E2 no restart" "0" "$(count_lines "$WORK/restarts.log")"

# ── E3: crash-loop guardrail ────────────────────────────────────────────────
reset_state
make_dept loopy stalled 2400 > "$WORK/specs.jsonl"
INJ3="$WORK/channels/telegram-loopy/inject"
mkdir -p "$(dirname "$STATE")"
for h in 5 3 1; do   # three prior kicks in the window, all for OLDER errors
    $PY - "$STATE" "$NOW" "$h" <<'PY'
import sys, json, datetime
p, now, h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
iso = lambda e: datetime.datetime.fromtimestamp(e, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
ev = {"ts": iso(now - h*3600), "slug": "loopy", "action": "kick-inject", "reason": "t", "level": 1, "err_ts": iso(now - h*3600 - 1800)}
open(p, "a").write(json.dumps(ev) + "\n")
PY
done
run_wd
want   "E3 4th pass escalates (hold-guardrail)" "loopy: hold-guardrail" "$OUT"
chk_eq "E3 NO inject on the 4th" "0" "$(count_lines "$INJ3")"
chk_eq "E3 escalation notified once" "1" "$(count_lines "$WORK/notify.log")"
run_wd
chk_eq "E3b repeat pass stays quiet (dedupe)" "1" "$(count_lines "$WORK/notify.log")"
chk_eq "E3b still no inject" "0" "$(count_lines "$INJ3")"

# ── E4: DRY_RUN touches nothing ─────────────────────────────────────────────
reset_state
make_dept dry1 stalled 1200 > "$WORK/specs.jsonl"
INJ4="$WORK/channels/telegram-dry1/inject"
run_wd BUBBLE_TICKWD_DRY_RUN=1
want   "E4 dry-run decided kick-inject" "dry1: kick-inject" "$OUT"
want   "E4 dry-run says would" "DRY_RUN — would kick-inject" "$OUT"
chk_eq "E4 inject untouched" "0" "$(count_lines "$INJ4")"
[[ -f "$STATE" ]] && bad "E4 no event in dry-run" || ok "E4 no event in dry-run"
chk_eq "E4 no notify" "0" "$(count_lines "$WORK/notify.log")"

# ── E5: stalled but session dead → no inject, alert ─────────────────────────
reset_state
make_dept dead1 stalled 1200 > "$WORK/specs.jsonl"
touch "$WORK/dead-dead1"
INJ5="$WORK/channels/telegram-dead1/inject"
run_wd
want   "E5 recognises dead session" "inject impossible" "$OUT"
chk_eq "E5 no inject line" "0" "$(count_lines "$INJ5")"
chk_eq "E5 no restart (supervisor owns process death)" "0" "$(count_lines "$WORK/restarts.log")"
chk_eq "E5 one alert" "1" "$(count_lines "$WORK/notify.log")"

# ── E6: inject proved deaf → restart once; forbidden without --continue ─────
reset_state
make_dept deaf1 stalled 7200 > "$WORK/specs.jsonl"     # error 2h ago
run_wd                                                  # pass 1: inject
want   "E6 pass1 injects" "deaf1: kick-inject" "$OUT"
# Rewind the recorded kick 40 min into the past so the cooldown has elapsed,
# while the transcript (same error line) stayed untouched → deaf.
$PY - "$STATE" <<'PY'
import sys, json, datetime
p = sys.argv[1]
evs = [json.loads(l) for l in open(p) if l.strip()]
for ev in evs:
    t = datetime.datetime.strptime(ev["ts"], "%Y-%m-%dT%H:%M:%SZ") - datetime.timedelta(minutes=40)
    ev["ts"] = t.strftime("%Y-%m-%dT%H:%M:%SZ")
open(p, "w").write("".join(json.dumps(e) + "\n" for e in evs))
PY
run_wd                                                  # pass 2: restart
want   "E6 pass2 escalates to restart" "deaf1: kick-restart" "$OUT"
chk_eq "E6 restart hook fired once" "1" "$(count_lines "$WORK/restarts.log")"
chk_eq "E6 inject not re-appended" "1" "$(count_lines "$WORK/channels/telegram-deaf1/inject")"
# same scenario, runtime without --continue → alert, never restart
reset_state
make_dept deaf2 stalled 7200 false > "$WORK/specs.jsonl"
run_wd
$PY - "$STATE" <<'PY'
import sys, json, datetime
p = sys.argv[1]
evs = [json.loads(l) for l in open(p) if l.strip()]
for ev in evs:
    t = datetime.datetime.strptime(ev["ts"], "%Y-%m-%dT%H:%M:%SZ") - datetime.timedelta(minutes=40)
    ev["ts"] = t.strftime("%Y-%m-%dT%H:%M:%SZ")
open(p, "w").write("".join(json.dumps(e) + "\n" for e in evs))
PY
run_wd
want   "E6b no --continue → alert-inject-failed" "deaf2: alert-inject-failed" "$OUT"
chk_eq "E6b restart hook NOT fired" "0" "$(count_lines "$WORK/restarts.log")"

# ── E7: Mac discovery from a fake LaunchAgents dir ──────────────────────────
LA="$WORK/LaunchAgents"; mkdir -p "$LA" "$WORK/AppSupport"
WRAP="$WORK/AppSupport/ops-loop-rnd-wrapper.sh"
cat > "$WRAP" <<'EOF'
#!/bin/bash
export TELEGRAM_STATE_DIR="/Users/joris/.claude/channels/telegram-rnd"
cd "/Users/joris/claude-workspaces/Rick_RnD"
TMUX_BIN="/Users/joris/.local/bin/tmux"
SESSION="ops-loop-rnd"
start_claude() { "$TMUX_BIN" new-session -d -s "$SESSION" "exec claude $1 --model 'claude-opus-4-8[1m]' --channels plugin:telegram@claude-plugins-official"; }
start_claude "--continue"
EOF
cat > "$LA/com.bubble.ops-loop-rnd.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.bubble.ops-loop-rnd</string>
<!-- The wrapper launches a PERSISTENT interactive \`claude --channels\`
     session inside tmux — this REAL fleet comment contains "--", which is
     illegal inside an XML comment: plutil tolerates it, plistlib does not. -->
<key>ProgramArguments</key><array><string>$WRAP</string></array>
<key>WorkingDirectory</key><string>/Users/joris/claude-workspaces/Rick_RnD</string>
<key>KeepAlive</key><true/>
</dict></plist>
EOF
cp "$LA/com.bubble.ops-loop-rnd.plist" "$LA/com.bubble.ops-loop-backup-rnd.plist"   # must be skipped
DISC="$($PY - "$REPO_ROOT" "$LA" <<'PY'
import sys, os, json, importlib.util
repo, la = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("runner", os.path.join(repo, "scripts", "loop-tick-watchdog.py"))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m   # py3.9 dataclasses resolve string annotations via sys.modules
spec.loader.exec_module(m)
specs = m.discover_local(la, "/Users/joris/.claude/projects", "/Users/joris/.claude/channels")
print(json.dumps([s.__dict__ for s in specs]))
PY
)"
chk_eq "E7 exactly one dept discovered (backup plist skipped)" "1" "$($PY -c 'import sys,json;print(len(json.loads(sys.argv[1])))' "$DISC")"
chk_eq "E7 slug" "rnd" "$($PY -c 'import sys,json;print(json.loads(sys.argv[1])[0]["slug"])' "$DISC")"
chk_eq "E7 session_dir encodes cwd like Claude Code" "/Users/joris/.claude/projects/-Users-joris-claude-workspaces-Rick-RnD" "$($PY -c 'import sys,json;print(json.loads(sys.argv[1])[0]["session_dir"])' "$DISC")"
chk_eq "E7 inject file from wrapper TELEGRAM_STATE_DIR" "/Users/joris/.claude/channels/telegram-rnd/inject" "$($PY -c 'import sys,json;print(json.loads(sys.argv[1])[0]["inject_file"])' "$DISC")"
chk_eq "E7 --continue detected" "True" "$($PY -c 'import sys,json;print(json.loads(sys.argv[1])[0]["resumes_context"])' "$DISC")"
chk_eq "E7 tmux bin + session from wrapper" "/Users/joris/.local/bin/tmux ops-loop-rnd" "$($PY -c 'import sys,json;s=json.loads(sys.argv[1])[0];print(s["tmux_bin"],s["tmux_session"])' "$DISC")"

# ── E8: inject payload shape ────────────────────────────────────────────────
reset_state
make_dept shape1 stalled 1200 > "$WORK/specs.jsonl"
run_wd
INJ8="$WORK/channels/telegram-shape1/inject"
chk_eq "E8 single line" "1" "$(count_lines "$INJ8")"
case "$(head -c1 "$INJ8")" in /) bad "E8 must not start with /" ;; *) ok "E8 does not start with /" ;; esac

echo
echo "RESULTS: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
