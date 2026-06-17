#!/usr/bin/env bash
# =============================================================================
# test_local_loop_staleness.sh — TDD harness for the Mac BACKUP FLOOR's core:
# the heartbeat-staleness check in deploy/local/lib/local_loop_lib.sh
# (is_heartbeat_stale) + the decision wiring in local-loop-backup-runner.sh.
#
# Context (MIRANDA-BUILD-SPEC B2): a host:local dept's /loop runs on a Mac via
# launchd. A Mac backup floor force-ticks it ONLY when the dept's heartbeat is
# stale. This is the testable core: given a heartbeat.log with a recent vs old
# last line, the check returns fresh vs stale; a MISSING log is fail-safe stale.
#
# Runs entirely against THROWAWAY fixtures under a mktemp dir. NEVER calls
# launchctl, NEVER launches `claude` (the runner is exercised WITHOUT
# --activate-tick, so it only decides + prints). Mirrors the style of
# test_sync_local_dept_clones.sh (bash harness + tmp fixture).
#
# Assertions:
#   T1  a RECENT heartbeat line → fresh (no backup tick).
#   T2  an OLD heartbeat line (> stale-sec) → stale (backup tick).
#   T3  MISSING heartbeat.log → stale (fail-safe: never skip when blind).
#   T4  the runner WITHOUT --activate-tick prints a DRY decision and NEVER
#       launches claude (exit 0, "would force-tick" only when stale).
#   T5  the runner on a fresh dept exits 0 and does NOT force-tick.
#   T6  microsecond/offset ISO form (datetime.isoformat()) is parsed correctly.
#   T8  the force-tick command is a HEADLESS `-p /loop` tick + --add-dir <ws>
#       (regression guard for the 2026-06-17 "bare claude, never ticked" bug).
#   T9  WITH --activate-tick + a stub claude, claude IS launched with -p /loop
#       and --add-dir (proves the floor actually ticks).
#   T10 --extra-path lets a bare `claude` resolve under a minimal launchd PATH.
#   T7  fixture safety — the dept-dir is a throwaway, not a live workspace.
# =============================================================================
set -uo pipefail

LIB="${1:?usage: test_local_loop_staleness.sh <lib/local_loop_lib.sh> [runner.sh]}"
RUNNER="${2:-$(cd "$(dirname "$LIB")/.." && pwd)/local-loop-backup-runner.sh}"
[[ -f "$LIB" ]]    || { echo "FATAL: lib not found: $LIB"; exit 2; }
[[ -f "$RUNNER" ]] || { echo "FATAL: runner not found: $RUNNER"; exit 2; }

PASS=0; FAIL=0
chk()    { if [[ "$2" == "$3" ]]; then echo "  PASS: $1 (rc=$3)"; PASS=$((PASS+1)); else echo "  FAIL: $1 (expected rc=$2, got rc=$3)"; FAIL=$((FAIL+1)); fi; }
chk_eq() { if [[ "$2" == "$3" ]]; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (expected '$2', got '$3')"; FAIL=$((FAIL+1)); fi; }
want()   { if grep -q "$2" "$3" 2>/dev/null; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (no match '$2' in $3)"; FAIL=$((FAIL+1)); fi; }
nowant() { if grep -q "$2" "$3" 2>/dev/null; then echo "  FAIL: $1 (unexpected '$2' in $3)"; FAIL=$((FAIL+1)); else echo "  PASS: $1"; PASS=$((PASS+1)); fi; }

# shellcheck source=/dev/null
. "$LIB"

WORK="$(mktemp -d /tmp/local-loop-stale.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

STALE_SEC=5400   # 90 min, the default

# make_dept <slug> writes a throwaway dept dir, echoes its path. The caller then
# drops a heartbeat (or not) under outputs/<today>/heartbeat.log.
make_dept() {
    local slug="$1"
    local dir="$WORK/agents/bubble-ops-$slug"
    mkdir -p "$dir/outputs"
    echo "$dir"
}
# write_hb <dept-dir> <iso-ts> : write one heartbeat line under today's dir.
write_hb() {
    local dir="$1" ts="$2"
    local today; today="$(date -u +%Y-%m-%d)"
    mkdir -p "$dir/outputs/$today"
    printf '%s tick idle queues: none\n' "$ts" > "$dir/outputs/$today/heartbeat.log"
}
iso_now()      { date -u +%Y-%m-%dT%H:%M:%SZ; }
# iso_ago <sec> : an ISO-8601 Z timestamp <sec> seconds in the past (portable
# across BSD/GNU date).
iso_ago() {
    local sec="$1"
    python3 - "$sec" <<'PY'
import sys, datetime
ago = int(sys.argv[1])
t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=ago)
print(t.strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
}

echo "== local-loop staleness tests =="

# -----------------------------------------------------------------------------
# T1: recent heartbeat → fresh
# -----------------------------------------------------------------------------
d="$(make_dept fresh1)"
write_hb "$d" "$(iso_now)"
res="$(is_heartbeat_stale "$d" "$STALE_SEC")"
chk_eq "T1 recent heartbeat → fresh" "fresh" "$res"

# -----------------------------------------------------------------------------
# T2: old heartbeat (older than stale-sec) → stale
# -----------------------------------------------------------------------------
d="$(make_dept stale1)"
write_hb "$d" "$(iso_ago $((STALE_SEC + 600)))"   # 100 min old > 90 min
res="$(is_heartbeat_stale "$d" "$STALE_SEC")"
chk_eq "T2 old heartbeat (>stale) → stale" "stale" "$res"

# -----------------------------------------------------------------------------
# T3: missing heartbeat.log → stale (fail-safe)
# -----------------------------------------------------------------------------
d="$(make_dept missing1)"   # no heartbeat written at all
res="$(is_heartbeat_stale "$d" "$STALE_SEC")"
chk_eq "T3 missing heartbeat → stale (fail-safe)" "stale" "$res"

# -----------------------------------------------------------------------------
# T4: runner WITHOUT --activate-tick on a STALE dept → dry decision, no claude
# -----------------------------------------------------------------------------
d="$(make_dept stale2)"
write_hb "$d" "$(iso_ago $((STALE_SEC + 600)))"
# Shim a `claude` that, if ever called, leaves a tripwire file. The dry run must
# NEVER call it.
SHIM="$WORK/shim"; mkdir -p "$SHIM"
TRIP="$WORK/claude-was-called"
cat > "$SHIM/claude" <<EOF
#!/usr/bin/env bash
echo "TRIPWIRE: claude launched" > "$TRIP"
EOF
chmod +x "$SHIM/claude"
out="$WORK/run-stale.log"
PATH="$SHIM:$PATH" "$RUNNER" --dept-dir "$d" --slug stale2 --stale-sec "$STALE_SEC" >"$out" 2>&1
rc=$?
chk "T4 runner (dry) on stale dept exits 0" 0 "$rc"
want   "T4b stale dept logged as STALE" "STALE" "$out"
want   "T4c stale dept prints a dry 'would force-tick'" "would force-tick" "$out"
nowant "T4d dry run NEVER launched claude" "." "$TRIP"

# -----------------------------------------------------------------------------
# T5: runner on a FRESH dept → exits 0, no tick, no claude
# -----------------------------------------------------------------------------
d="$(make_dept fresh2)"
write_hb "$d" "$(iso_now)"
TRIP2="$WORK/claude-was-called-2"
cat > "$SHIM/claude" <<EOF
#!/usr/bin/env bash
echo "TRIPWIRE" > "$TRIP2"
EOF
chmod +x "$SHIM/claude"
out="$WORK/run-fresh.log"
PATH="$SHIM:$PATH" "$RUNNER" --dept-dir "$d" --slug fresh2 --stale-sec "$STALE_SEC" >"$out" 2>&1
rc=$?
chk "T5 runner on fresh dept exits 0" 0 "$rc"
want   "T5b fresh dept logged as FRESH" "FRESH" "$out"
nowant "T5c fresh dept never force-ticks" "would force-tick" "$out"
nowant "T5d fresh dept never launched claude" "." "$TRIP2"

# -----------------------------------------------------------------------------
# T6: microsecond/offset ISO form (datetime.isoformat()) is parsed → fresh when
#     recent (regression guard mirroring loop_backup.py's _ISO_RE fix).
# -----------------------------------------------------------------------------
d="$(make_dept iso1)"
today="$(date -u +%Y-%m-%d)"; mkdir -p "$d/outputs/$today"
micro="$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())')"
printf '%s tick idle\n' "$micro" > "$d/outputs/$today/heartbeat.log"
res="$(is_heartbeat_stale "$d" "$STALE_SEC")"
chk_eq "T6 microsecond/offset ISO form parsed → fresh" "fresh" "$res"

# -----------------------------------------------------------------------------
# T8: the force-tick command is a HEADLESS /loop tick (not a bare interactive
#     claude). Regression guard for the 2026-06-17 bug where the runner launched
#     `claude --dangerously-skip-permissions` with NO prompt (never ticked) and
#     was skill-blind. The dry output must show the real command shape.
# -----------------------------------------------------------------------------
d="$(make_dept stale3)"
write_hb "$d" "$(iso_ago $((STALE_SEC + 600)))"
WS="$WORK/ws-skills"; mkdir -p "$WS/.claude/skills"
out="$WORK/run-cmd.log"
"$RUNNER" --dept-dir "$d" --slug stale3 --stale-sec "$STALE_SEC" \
    --workspace-dir "$WS" >"$out" 2>&1
want "T8 dry force-tick invokes /loop headless"      '\-p /loop' "$out"
want "T8b dry force-tick grants workspace --add-dir" "add-dir $WS" "$out"

# -----------------------------------------------------------------------------
# T9: WITH --activate-tick + a stub claude, the runner ACTUALLY launches claude,
#     passing `-p /loop` and `--add-dir <ws>`. Proves the floor really ticks
#     (the missing --activate-tick + bad command were why it never did).
# -----------------------------------------------------------------------------
d="$(make_dept stale4)"
write_hb "$d" "$(iso_ago $((STALE_SEC + 600)))"
STUB="$WORK/stub"; mkdir -p "$STUB"
ARGS_LOG="$WORK/claude-args.log"
cat > "$STUB/claude" <<EOF
#!/usr/bin/env bash
echo "\$@" > "$ARGS_LOG"
exit 0
EOF
chmod +x "$STUB/claude"
out="$WORK/run-activate.log"
"$RUNNER" --dept-dir "$d" --slug stale4 --stale-sec "$STALE_SEC" \
    --claude-bin "$STUB/claude" --workspace-dir "$WS" --activate-tick >"$out" 2>&1
rc=$?
chk    "T9 runner --activate-tick on stale dept exits 0" 0 "$rc"
want   "T9b claude WAS launched (args captured)" "." "$ARGS_LOG"
want   "T9c claude got -p /loop"     '\-p /loop' "$ARGS_LOG"
want   "T9d claude got --add-dir ws" "add-dir $WS" "$ARGS_LOG"

# -----------------------------------------------------------------------------
# T10: --extra-path makes a bare `claude` on a NON-PATH dir resolve (the launchd
#      "command not found" bug). Stub lives in a dir NOT on PATH; pass it via
#      --extra-path + bare --claude-bin claude; the tick must still find it.
# -----------------------------------------------------------------------------
d="$(make_dept stale5)"
write_hb "$d" "$(iso_ago $((STALE_SEC + 600)))"
HIDDEN="$WORK/hidden-bin"; mkdir -p "$HIDDEN"
HIT="$WORK/extra-path-hit.log"
cat > "$HIDDEN/claude" <<EOF
#!/usr/bin/env bash
echo "found via extra-path" > "$HIT"
exit 0
EOF
chmod +x "$HIDDEN/claude"
out="$WORK/run-extrapath.log"
env -i HOME="$HOME" PATH="/usr/bin:/bin" bash "$RUNNER" --dept-dir "$d" --slug stale5 \
    --stale-sec "$STALE_SEC" --claude-bin claude --extra-path "$HIDDEN" --activate-tick >"$out" 2>&1 || true
want "T10 --extra-path lets a bare claude resolve" "." "$HIT"

# -----------------------------------------------------------------------------
# T7: fixture safety — dept-dir is a throwaway, not a live workspace
# -----------------------------------------------------------------------------
case "$WORK" in
  /Users/*/claude-workspaces/*|/home/claude/agents/*)
      echo "  FAIL: T7 fixture pointed at a LIVE workspace! ($WORK)"; FAIL=$((FAIL+1));;
  *)  echo "  PASS: T7 dept fixtures are throwaway ($WORK)"; PASS=$((PASS+1));;
esac

echo
echo "RESULTS: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
