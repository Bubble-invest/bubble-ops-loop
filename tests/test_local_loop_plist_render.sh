#!/usr/bin/env bash
# =============================================================================
# test_local_loop_plist_render.sh — TDD harness for the Mac launchd plist
# RENDER of both installers (deploy/local/install-local-loop.sh and
# install-local-loop-backup.sh).
#
# Context (MIRANDA-BUILD-SPEC B2): the installers render a launchd plist for a
# host:local dept's main /loop runner and its backup floor. This harness proves
# the rendered plist is VALID and carries the right Label / StartInterval /
# dept-dir command — rendered WITHOUT --activate, so launchctl is NEVER called.
#
# Runs against a THROWAWAY --launch-agents-dir under a mktemp dir. NEVER touches
# ~/Library/LaunchAgents and NEVER calls launchctl (no --activate). Mirrors the
# style of test_sync_local_dept_clones.sh.
#
# Assertions:
#   T1  install-local-loop.sh (no --activate) writes a plist + exits 0.
#   T2  the plist parses (plutil -lint if present, else a key/XML sanity check).
#   T3  it carries the right Label (com.bubble.ops-loop-<slug>).
#   T4  the MAIN runner uses KeepAlive (persistent session), NOT StartInterval /
#       StartCalendarInterval — the Mac twin of the VPS interactive --channels unit.
#   T5  it installs + invokes a wrapper that runs `claude --channels telegram`
#       inside tmux (the dept's Telegram channel), cd'ing into the dept-dir.
#   T6  install-local-loop-backup.sh renders a valid backup plist with the
#       backup Label + the runner invocation + StartInterval.
#   T7  re-running the installer is idempotent (overwrites, still valid, exit 0).
#   T8  --uninstall (no --activate) removes the plist without launchctl.
#   T9  NO launchctl was ever invoked (a PATH shim tripwire stays untouched).
#   T10 fixture safety — the LaunchAgents dir is a throwaway, not the real one.
# =============================================================================
set -uo pipefail

INSTALL_LOOP="${1:?usage: test_local_loop_plist_render.sh <install-local-loop.sh> <install-local-loop-backup.sh>}"
INSTALL_BACKUP="${2:?usage: ... <install-local-loop-backup.sh>}"
[[ -f "$INSTALL_LOOP" ]]   || { echo "FATAL: not found: $INSTALL_LOOP"; exit 2; }
[[ -f "$INSTALL_BACKUP" ]] || { echo "FATAL: not found: $INSTALL_BACKUP"; exit 2; }

PASS=0; FAIL=0
chk()    { if [[ "$2" == "$3" ]]; then echo "  PASS: $1 (rc=$3)"; PASS=$((PASS+1)); else echo "  FAIL: $1 (expected rc=$2, got rc=$3)"; FAIL=$((FAIL+1)); fi; }
want()   { if grep -q "$2" "$3" 2>/dev/null; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (no match '$2' in $3)"; FAIL=$((FAIL+1)); fi; }
nowant() { if grep -q "$2" "$3" 2>/dev/null; then echo "  FAIL: $1 (unexpected '$2' in $3)"; FAIL=$((FAIL+1)); else echo "  PASS: $1"; PASS=$((PASS+1)); fi; }
exists() { if [[ -f "$2" ]]; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (no file $2)"; FAIL=$((FAIL+1)); fi; }
absent() { if [[ -f "$2" ]]; then echo "  FAIL: $1 (file still present $2)"; FAIL=$((FAIL+1)); else echo "  PASS: $1"; PASS=$((PASS+1)); fi; }

# valid_plist <file>: plutil -lint if available, else a basic XML/key sanity.
valid_plist() {
    local f="$1"
    if command -v plutil >/dev/null 2>&1; then
        plutil -lint "$f" >/dev/null 2>&1
    else
        grep -q "<plist" "$f" && grep -q "</plist>" "$f" && grep -q "<key>Label</key>" "$f"
    fi
}

WORK="$(mktemp -d /tmp/local-loop-plist.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

LA="$WORK/LaunchAgents"          # throwaway "LaunchAgents" dir
LOGS="$WORK/logs"
WRAP="$WORK/wrappers"            # throwaway wrapper dir
DEPT="$WORK/agents/bubble-ops-content"; mkdir -p "$DEPT/outputs"
SLUG="content"

# launchctl tripwire shim — if any installer calls launchctl WITHOUT --activate,
# this records it and the test fails T9.
SHIM="$WORK/shim"; mkdir -p "$SHIM"
LC_TRIP="$WORK/launchctl-was-called"
cat > "$SHIM/launchctl" <<EOF
#!/usr/bin/env bash
echo "TRIPWIRE: launchctl \$*" >> "$LC_TRIP"
exit 0
EOF
chmod +x "$SHIM/launchctl"
export PATH="$SHIM:$PATH"

echo "== local-loop plist render tests =="

# -----------------------------------------------------------------------------
# Main loop runner render (NO --activate)
# -----------------------------------------------------------------------------
WSDIR="$WORK/workspace"; mkdir -p "$WSDIR/.claude/skills"   # the body whose skills the dept reuses
out="$WORK/main.log"
"$INSTALL_LOOP" --dept-dir "$DEPT" --slug "$SLUG" \
    --launch-agents-dir "$LA" --log-dir "$LOGS" --wrapper-dir "$WRAP" \
    --claude-bin /usr/bin/claude --tmux-bin /opt/homebrew/bin/tmux \
    --telegram-state-dir "$WORK/tg" --extra-path "/opt/homebrew/bin" \
    --workspace-dir "$WSDIR" \
    >"$out" 2>&1
rc=$?
PLIST="$LA/com.bubble.ops-loop-${SLUG}.plist"
WRAPPER="$WRAP/ops-loop-${SLUG}-wrapper.sh"
chk    "T1 install-local-loop (no --activate) exits 0" 0 "$rc"
exists "T1b plist file written" "$PLIST"
exists "T1c wrapper file written" "$WRAPPER"
if valid_plist "$PLIST"; then echo "  PASS: T2 rendered plist is valid"; PASS=$((PASS+1)); else echo "  FAIL: T2 rendered plist INVALID"; FAIL=$((FAIL+1)); fi
want   "T3 plist carries the right Label" "com.bubble.ops-loop-${SLUG}" "$PLIST"
want   "T4 MAIN runner uses KeepAlive"    "<key>KeepAlive</key>"        "$PLIST"
nowant "T4b MAIN runner does NOT use StartInterval"          "<key>StartInterval</key>"         "$PLIST"
nowant "T4c MAIN runner does NOT use StartCalendarInterval"  "<key>StartCalendarInterval</key>" "$PLIST"
want   "T4d plist ProgramArguments points at the wrapper"    "ops-loop-${SLUG}-wrapper.sh" "$PLIST"
want   "T5 wrapper cd's into the dept-dir"        "cd \"${DEPT}\"" "$WRAPPER"
want   "T5b wrapper runs claude with --channels telegram" "channels plugin:telegram@claude-plugins-official" "$WRAPPER"
want   "T5c wrapper runs claude inside tmux"      "new-session" "$WRAPPER"
want   "T5d wrapper sources the telegram env"     "${WORK}/tg/.env" "$WRAPPER"
want   "T5e wrapper grants the workspace via --add-dir (brain↔body)" "add-dir '${WSDIR}'" "$WRAPPER"

# -----------------------------------------------------------------------------
# Backup floor render (NO --activate) — now WITH claude-bin + workspace + extra-path
# -----------------------------------------------------------------------------
out="$WORK/backup.log"
"$INSTALL_BACKUP" --dept-dir "$DEPT" --slug "$SLUG" --interval 10800 \
    --launch-agents-dir "$LA" --log-dir "$LOGS" \
    --claude-bin /usr/bin/claude --workspace-dir "$WSDIR" --extra-path "/opt/homebrew/bin" \
    >"$out" 2>&1
rc=$?
BPLIST="$LA/com.bubble.ops-loop-backup-${SLUG}.plist"
chk    "T6 install-local-loop-backup (no --activate) exits 0" 0 "$rc"
exists "T6b backup plist file written" "$BPLIST"
if valid_plist "$BPLIST"; then echo "  PASS: T6c backup plist is valid"; PASS=$((PASS+1)); else echo "  FAIL: T6c backup plist INVALID"; FAIL=$((FAIL+1)); fi
want   "T6d backup plist Label"            "com.bubble.ops-loop-backup-${SLUG}" "$BPLIST"
want   "T6e backup plist StartInterval"    "<key>StartInterval</key>" "$BPLIST"
nowant "T6f backup plist no StartCalendarInterval key" "<key>StartCalendarInterval</key>" "$BPLIST"
want   "T6g backup plist invokes the runner" "local-loop-backup-runner.sh" "$BPLIST"
want   "T6h backup plist passes --dept-dir"  "dept-dir" "$BPLIST"
# Regression guard (2026-06-17): a real install bakes --activate-tick + the
# claude-bin + the workspace, so the floor actually ticks + is skill-aware.
want   "T6i backup plist bakes --activate-tick (floor really ticks)" "activate-tick" "$BPLIST"
want   "T6j backup plist bakes --claude-bin"    "claude-bin" "$BPLIST"
want   "T6k backup plist bakes --workspace-dir" "workspace-dir" "$BPLIST"
# And --dry-tick keeps the old detect-only behaviour (no --activate-tick).
"$INSTALL_BACKUP" --dept-dir "$DEPT" --slug "${SLUG}-dry" --interval 10800 \
    --launch-agents-dir "$LA" --log-dir "$LOGS" --dry-tick >"$WORK/backup-dry.log" 2>&1
BPLIST_DRY="$LA/com.bubble.ops-loop-backup-${SLUG}-dry.plist"
nowant "T6l --dry-tick omits --activate-tick (detect-only)" "activate-tick" "$BPLIST_DRY"

# -----------------------------------------------------------------------------
# T7: idempotent re-run
# -----------------------------------------------------------------------------
"$INSTALL_LOOP" --dept-dir "$DEPT" --slug "$SLUG" \
    --launch-agents-dir "$LA" --log-dir "$LOGS" --wrapper-dir "$WRAP" \
    --claude-bin /usr/bin/claude --tmux-bin /opt/homebrew/bin/tmux \
    --telegram-state-dir "$WORK/tg" --extra-path "/opt/homebrew/bin" \
    --workspace-dir "$WSDIR" \
    >"$WORK/main2.log" 2>&1
rc=$?
chk "T7 re-run install is idempotent (exit 0)" 0 "$rc"
if valid_plist "$PLIST"; then echo "  PASS: T7b plist still valid after re-run"; PASS=$((PASS+1)); else echo "  FAIL: T7b plist invalid after re-run"; FAIL=$((FAIL+1)); fi

# T7c: WITHOUT --workspace-dir, the wrapper carries NO --add-dir (self-contained dept).
WRAP2="$WORK/wrappers-nows"
"$INSTALL_LOOP" --dept-dir "$DEPT" --slug "selfcontained" \
    --launch-agents-dir "$LA" --log-dir "$LOGS" --wrapper-dir "$WRAP2" \
    --claude-bin /usr/bin/claude --tmux-bin /opt/homebrew/bin/tmux \
    --telegram-state-dir "$WORK/tg" --extra-path "/opt/homebrew/bin" \
    >"$WORK/main3.log" 2>&1
nowant "T7c no --workspace-dir => wrapper has NO --add-dir" "add-dir" "$WRAP2/ops-loop-selfcontained-wrapper.sh"

# -----------------------------------------------------------------------------
# T8: --uninstall (no --activate) removes the plist + wrapper, no launchctl
# -----------------------------------------------------------------------------
"$INSTALL_LOOP" --uninstall --slug "$SLUG" --launch-agents-dir "$LA" --wrapper-dir "$WRAP" >"$WORK/uninstall.log" 2>&1
rc=$?
chk    "T8 --uninstall exits 0" 0 "$rc"
absent "T8b plist removed by --uninstall" "$PLIST"
absent "T8c wrapper removed by --uninstall" "$WRAPPER"

# -----------------------------------------------------------------------------
# T9: launchctl was NEVER called (no --activate anywhere above)
# -----------------------------------------------------------------------------
nowant "T9 launchctl was NEVER invoked (no --activate)" "." "$LC_TRIP"

# -----------------------------------------------------------------------------
# T10: fixture safety — LaunchAgents dir is a throwaway, not the real one
# -----------------------------------------------------------------------------
case "$LA" in
  "$HOME/Library/LaunchAgents") echo "  FAIL: T10 pointed at the REAL LaunchAgents!"; FAIL=$((FAIL+1));;
  *) echo "  PASS: T10 LaunchAgents dir is a throwaway ($LA)"; PASS=$((PASS+1));;
esac

echo
echo "RESULTS: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
