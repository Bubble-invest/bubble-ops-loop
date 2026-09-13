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
#   T11 (board #956) the wrapper self-heals the telegram plugin's boot_rearm +
#       bubble-inject patches before every launch: --channel-patches-script
#       is honored, and OMITTING it (or passing "") disables the hook cleanly
#       (backward-compatible no-op) rather than erroring.
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
nexists() { if [[ ! -f "$2" ]]; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (file still present $2)"; FAIL=$((FAIL+1)); fi; }
absent() { if [[ -f "$2" ]]; then echo "  FAIL: $1 (file still present $2)"; FAIL=$((FAIL+1)); else echo "  PASS: $1"; PASS=$((PASS+1)); fi; }

# valid_plist <file>: the rendered plist must be well-formed XML AND a valid plist.
# Two independent checks, because they catch DIFFERENT failures (board #1101):
#   - plistlib.load() (Python stdlib) is a strict XML parser. It is FATAL on a
#     plist that embeds "--" inside an XML comment (e.g. a "--channels" flag in a
#     doc comment) — exactly the class of bug #1101 fixed. This is the assertion
#     the card asks for; it is the one that actually trips on the malformed plist.
#   - plutil -lint (if present) is TOLERANT of that same "--" and passes anyway,
#     so it is kept only as a supplementary structural check, never the gate.
valid_plist() {
    local f="$1"
    # Strict XML/plist parse via Python stdlib — the #1101 gate.
    python3 - "$f" <<'PY' || return 1
import plistlib, sys
with open(sys.argv[1], "rb") as fh:
    plistlib.load(fh)
PY
    # Supplementary structural lint (tolerant of the #1101 bug — informational).
    if command -v plutil >/dev/null 2>&1; then
        plutil -lint "$f" >/dev/null 2>&1 || return 1
    fi
    return 0
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
# Backup floor render (NO --activate)
# -----------------------------------------------------------------------------
out="$WORK/backup.log"
TG_BACKUP="$WORK/tg-backup"; mkdir -p "$TG_BACKUP"; chmod 700 "$TG_BACKUP"
TMUX_BACKUP="$WORK/tmux"; printf '#!/bin/sh\nexit 0\n' >"$TMUX_BACKUP"; chmod 700 "$TMUX_BACKUP"
"$INSTALL_BACKUP" --dept-dir "$DEPT" --slug "$SLUG" --interval 10800 \
    --telegram-state-dir "$TG_BACKUP" --session-name "ops-loop-$SLUG" \
    --harness-selector "$WORK/harness-$SLUG" \
    --tmux-bin "$TMUX_BACKUP" --stale-sec 5400 --cooldown-sec 900 \
    --launch-agents-dir "$LA" --log-dir "$LOGS" \
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
want   "T6i backup plist activates existing-session injection" "activate-inject" "$BPLIST"
want   "T6j backup plist carries exact channel state" "$TG_BACKUP" "$BPLIST"
want   "T6k backup plist carries exact session" "ops-loop-$SLUG" "$BPLIST"
want   "T6k2 backup plist carries harness selector" "$WORK/harness-$SLUG" "$BPLIST"
nowant "T6l backup plist has no model flag" "claude-bin" "$BPLIST"
nowant "T6m backup plist avoids shell -c" "<string>-c</string>" "$BPLIST"

# -----------------------------------------------------------------------------
# Wake-catch render (--wake-catch, NO --activate): a distinct, short-interval
# agent that reuses the SAME runner so it shares the staleness guard + cooldown
# (never double-ticks). Catches a stale loop promptly after the Mac wakes.
# -----------------------------------------------------------------------------
out="$WORK/wake.log"
"$INSTALL_BACKUP" --dept-dir "$DEPT" --slug "$SLUG" --wake-catch \
    --telegram-state-dir "$TG_BACKUP" --session-name "ops-loop-$SLUG" \
    --harness-selector "$WORK/harness-$SLUG" \
    --tmux-bin "$TMUX_BACKUP" --stale-sec 5400 --cooldown-sec 900 \
    --launch-agents-dir "$LA" --log-dir "$LOGS" \
    >"$out" 2>&1
rc=$?
WPLIST="$LA/com.bubble.ops-loop-wake-${SLUG}.plist"
chk    "T6w wake-catch install (no --activate) exits 0" 0 "$rc"
exists "T6w-b wake-catch plist file written" "$WPLIST"
if valid_plist "$WPLIST"; then echo "  PASS: T6w-c wake-catch plist is valid"; PASS=$((PASS+1)); else echo "  FAIL: T6w-c wake-catch plist INVALID"; FAIL=$((FAIL+1)); fi
want   "T6w-d wake-catch plist Label"        "com.bubble.ops-loop-wake-${SLUG}" "$WPLIST"
nowant "T6w-e wake-catch is NOT the backup floor label" "com.bubble.ops-loop-backup-${SLUG}" "$WPLIST"
want   "T6w-f wake-catch uses StartInterval"  "<key>StartInterval</key>" "$WPLIST"
nowant "T6w-g wake-catch no StartCalendarInterval" "<key>StartCalendarInterval</key>" "$WPLIST"
want   "T6w-h wake-catch default interval is 5m (prompt after wake)" "<integer>300</integer>" "$WPLIST"
want   "T6w-i wake-catch reuses the runner"   "local-loop-backup-runner.sh" "$WPLIST"
want   "T6w-j wake-catch activates injection" "activate-inject" "$WPLIST"
want   "T6w-k wake-catch carries exact session" "ops-loop-$SLUG" "$WPLIST"
# --interval must still override the wake-catch default.
"$INSTALL_BACKUP" --dept-dir "$DEPT" --slug "$SLUG" --wake-catch --interval 120 \
    --telegram-state-dir "$TG_BACKUP" --session-name "ops-loop-$SLUG" \
    --harness-selector "$WORK/harness-$SLUG" --tmux-bin "$TMUX_BACKUP" \
    --launch-agents-dir "$LA" --log-dir "$LOGS" >"$WORK/wake2.log" 2>&1
want   "T6w-l wake-catch honours explicit --interval override" "<integer>120</integer>" "$WPLIST"
# ...and the reverse flag order must behave identically (INTERVAL_SET is set in
# the same parse loop regardless of order).
"$INSTALL_BACKUP" --dept-dir "$DEPT" --slug "$SLUG" --interval 90 --wake-catch \
    --telegram-state-dir "$TG_BACKUP" --session-name "ops-loop-$SLUG" \
    --harness-selector "$WORK/harness-$SLUG" --tmux-bin "$TMUX_BACKUP" \
    --launch-agents-dir "$LA" --log-dir "$LOGS" >"$WORK/wake3.log" 2>&1
want   "T6w-m --interval before --wake-catch also overrides" "<integer>90</integer>" "$WPLIST"
# Uninstall must target ONLY the wake label, leaving the backup floor plist intact.
"$INSTALL_BACKUP" --slug "$SLUG" --wake-catch --uninstall \
    --launch-agents-dir "$LA" >"$WORK/wake-uninstall.log" 2>&1
rc=$?
chk    "T6w-n wake-catch --uninstall exits 0" 0 "$rc"
nexists "T6w-o wake-catch plist removed by --uninstall" "$WPLIST"
exists  "T6w-p backup floor plist untouched by wake --uninstall" "$BPLIST"

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
# T11 (board #956): channel-patches self-heal hook
# -----------------------------------------------------------------------------
echo ""
echo "T11: telegram-plugin patch self-heal hook (board #956)"
FAKE_PATCHER="$WORK/fake-install-channel-patches.sh"
cat > "$FAKE_PATCHER" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$FAKE_PATCHER"

WRAP3="$WORK/wrappers-patches"
"$INSTALL_LOOP" --dept-dir "$DEPT" --slug "patched" \
    --launch-agents-dir "$LA" --log-dir "$LOGS" --wrapper-dir "$WRAP3" \
    --claude-bin /usr/bin/claude --tmux-bin /opt/homebrew/bin/tmux \
    --telegram-state-dir "$WORK/tg" --extra-path "/opt/homebrew/bin" \
    --channel-patches-script "$FAKE_PATCHER" \
    >"$WORK/main-patches.log" 2>&1
rc=$?
chk "T11a install with --channel-patches-script exits 0" 0 "$rc"
want "T11b wrapper invokes the channel-patches script" "$FAKE_PATCHER" "$WRAP3/ops-loop-patched-wrapper.sh"
want "T11c invocation is fail-open (|| true)" "|| true" "$WRAP3/ops-loop-patched-wrapper.sh"

# T11d: explicitly opting OUT (empty string) disables the hook cleanly.
WRAP4="$WORK/wrappers-nopatches"
"$INSTALL_LOOP" --dept-dir "$DEPT" --slug "nopatch" \
    --launch-agents-dir "$LA" --log-dir "$LOGS" --wrapper-dir "$WRAP4" \
    --claude-bin /usr/bin/claude --tmux-bin /opt/homebrew/bin/tmux \
    --telegram-state-dir "$WORK/tg" --extra-path "/opt/homebrew/bin" \
    --channel-patches-script "" \
    >"$WORK/main-nopatches.log" 2>&1
rc=$?
chk "T11d install with --channel-patches-script '' exits 0" 0 "$rc"
nowant "T11e opted-out wrapper mentions no channel-patches script" "install-channel-patches" "$WRAP4/ops-loop-nopatch-wrapper.sh"
nowant "T11f opted-out wrapper has no self-heal comment" "Self-heal" "$WRAP4/ops-loop-nopatch-wrapper.sh"

# -----------------------------------------------------------------------------
# T8: --uninstall (no --activate) removes the plist + wrapper, no launchctl
# -----------------------------------------------------------------------------
"$INSTALL_LOOP" --uninstall --slug "$SLUG" --launch-agents-dir "$LA" --wrapper-dir "$WRAP" >"$WORK/uninstall.log" 2>&1
rc=$?
chk    "T8 --uninstall exits 0" 0 "$rc"
absent "T8b plist removed by --uninstall" "$PLIST"
absent "T8c wrapper removed by --uninstall" "$WRAPPER"

# -----------------------------------------------------------------------------
# T9: only read-only launchctl print is allowed without --activate.
# -----------------------------------------------------------------------------
if grep -v "TRIPWIRE: launchctl print " "$LC_TRIP" 2>/dev/null | grep -q .; then
    echo "  FAIL: T9 non-read-only launchctl invoked without --activate"; FAIL=$((FAIL+1))
else
    echo "  PASS: T9 no registration-changing launchctl without --activate"; PASS=$((PASS+1))
fi

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
