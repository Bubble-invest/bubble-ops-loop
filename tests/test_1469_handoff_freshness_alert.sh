#!/usr/bin/env bash
# test_1469_handoff_freshness_alert.sh — regression test for board #1469:
#
#   1. The HANDOFF.md freshness gate is now 12h by default (was ~20-24h),
#      still overridable via HANDOFF_MAX_AGE_H — a stale-day handoff must
#      SKIP, not rotate into a context-thin session.
#   2. Every SKIP (missing OR stale HANDOFF.md) now alerts via the fleet's
#      existing kanban emitter (tools/kanban/emit_kanban_item.sh) instead of
#      failing silently, with a per-slug-per-day dedup title.
#
# Covers both scripts/bubble-session-rotate.sh (VPS) and its Mac twin
# deploy/local/bubble-session-rotate-mac.sh.
#
# Hermetic:
#   - a stub emit_kanban_item.sh records its own invocation instead of
#     touching GitHub/Telegram/gh.
#   - systemctl/launchctl are never invoked: the "proceeds" case uses
#     --dry-run (built into both scripts), which returns before any restart.
#   - the Mac script is run directly (supports --workdir, runs natively on a
#     Darwin dev box). The VPS script hardcodes /srv/agents/<slug> +
#     /home/agent-<slug> (by design -- board #1195), which don't exist on a
#     Mac dev box and can't be created without root, so this test exercises
#     it via a byte-identical copy with only those two path prefixes
#     substituted (a test double, not a change to the shipped script) -- same
#     idea as stubbing a binary ahead of PATH, applied to a path constant
#     instead of a command.
#
# Run: bash tests/test_1469_handoff_freshness_alert.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VPS_SCRIPT="$REPO_ROOT/scripts/bubble-session-rotate.sh"
MAC_SCRIPT="$REPO_ROOT/deploy/local/bubble-session-rotate-mac.sh"

FAILED=0
fail() { echo "FAIL: $*" >&2; FAILED=1; }
pass() { echo "PASS: $*"; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ── stub emit_kanban_item.sh: records its argv, never touches gh/curl ──────
mk_stub_emitter() {
  local dir="$1"
  mkdir -p "$dir"
  cat > "$dir/emit_kanban_item.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_EMIT_LOG:?STUB_EMIT_LOG not set}"
exit 0
EOF
  chmod +x "$dir/emit_kanban_item.sh"
}

# touch a file N hours in the past (GNU or BSD date, whichever this host has)
touch_hours_ago() {
  local hours="$1" path="$2"
  local ts
  ts="$(date -v-"${hours}"H +%Y%m%d%H%M.%S 2>/dev/null || date -d "-${hours} hours" +%Y%m%d%H%M.%S)"
  touch -t "$ts" "$path"
}

# =============================================================================
# Group 1: default freshness window is 12h (static assertion on both scripts)
# =============================================================================
if grep -q 'HANDOFF_MAX_AGE_H="\${HANDOFF_MAX_AGE_H:-12}"' "$VPS_SCRIPT"; then
  pass "VPS script: HANDOFF_MAX_AGE_H default is 12"
else
  fail "VPS script: HANDOFF_MAX_AGE_H default is not 12"
fi
if grep -q 'HANDOFF_MAX_AGE_H="\${HANDOFF_MAX_AGE_H:-12}"' "$MAC_SCRIPT"; then
  pass "Mac script: HANDOFF_MAX_AGE_H default is 12"
else
  fail "Mac script: HANDOFF_MAX_AGE_H default is not 12"
fi

# =============================================================================
# Group 2: Mac script (deploy/local/bubble-session-rotate-mac.sh) -- full
# dynamic coverage, run directly (supports --workdir).
# =============================================================================
M_WORK="$TMP/mac-dept"
mkdir -p "$M_WORK"
mk_stub_emitter "$M_WORK/tools/kanban"
# The script also requires a plist to exist (unrelated FATAL check) -- fake it.
mkdir -p "$TMP/mac-home/Library/LaunchAgents"

run_mac() {
  # $1 = handoff age in hours ("" = no file), remaining = extra script args
  local age="$1"; shift
  rm -f "$M_WORK/HANDOFF.md"
  if [ -n "$age" ]; then
    touch_hours_ago "$age" "$M_WORK/HANDOFF.md"
  fi
  STUB_EMIT_LOG="$TMP/mac_emit.log" \
  HOME="$TMP/mac-home" \
    bash "$MAC_SCRIPT" macslug --workdir "$M_WORK" "$@" 2>&1 || true
}

# We need a real plist at $HOME/Library/LaunchAgents/com.bubble.ops-loop-macslug.plist
touch "$TMP/mac-home/Library/LaunchAgents/com.bubble.ops-loop-macslug.plist"

# --- 2a. missing HANDOFF.md -> SKIP + alert ---------------------------------
rm -f "$TMP/mac_emit.log"
out=$(run_mac "" --dry-run)
if echo "$out" | grep -q "SKIP macslug: no HANDOFF.md"; then
  pass "Mac: missing HANDOFF.md -> SKIP"
else
  fail "Mac: missing HANDOFF.md did not SKIP. Output: $out"
fi
if [ -f "$TMP/mac_emit.log" ] && grep -q "task=session-rotate" "$TMP/mac_emit.log" \
   && grep -q "title=session-rotate SKIP: macslug" "$TMP/mac_emit.log"; then
  pass "Mac: missing-HANDOFF SKIP alerted via emit_kanban_item.sh"
else
  fail "Mac: missing-HANDOFF SKIP did not alert (log: $(cat "$TMP/mac_emit.log" 2>/dev/null || echo '<none>'))"
fi

# --- 2b. stale HANDOFF.md (13h old): within the OLD ~20-24h window but past
#         the NEW 12h default -> must now SKIP (the regression this card
#         fixes) + alert -------------------------------------------------
rm -f "$TMP/mac_emit.log"
out=$(run_mac 13 --dry-run)
if echo "$out" | grep -q "SKIP macslug: HANDOFF.md is 13h stale"; then
  pass "Mac: 13h-old HANDOFF.md SKIPs under the new 12h default (was allowed under the old ~20-24h window)"
else
  fail "Mac: 13h-old HANDOFF.md did not SKIP under the tightened default. Output: $out"
fi
if [ -f "$TMP/mac_emit.log" ] && grep -q "title=session-rotate SKIP: macslug" "$TMP/mac_emit.log"; then
  pass "Mac: stale-HANDOFF SKIP alerted via emit_kanban_item.sh"
else
  fail "Mac: stale-HANDOFF SKIP did not alert"
fi

# --- 2c. fresh HANDOFF.md (2h old) -> proceeds, NO alert --------------------
rm -f "$TMP/mac_emit.log"
out=$(run_mac 2 --dry-run)
if echo "$out" | grep -q "HANDOFF.md present + fresh" && echo "$out" | grep -q "DRY-RUN macslug"; then
  pass "Mac: fresh HANDOFF.md proceeds (dry-run reaches the rotate step)"
else
  fail "Mac: fresh HANDOFF.md did not proceed. Output: $out"
fi
if [ ! -s "$TMP/mac_emit.log" ]; then
  pass "Mac: fresh HANDOFF.md did NOT alert (no SKIP)"
else
  fail "Mac: fresh HANDOFF.md unexpectedly alerted: $(cat "$TMP/mac_emit.log")"
fi

# --- 2d. HANDOFF_MAX_AGE_H override still works (e.g. loosened to 24h) -----
rm -f "$TMP/mac_emit.log"
out=$(HANDOFF_MAX_AGE_H=24 STUB_EMIT_LOG="$TMP/mac_emit.log" HOME="$TMP/mac-home" \
      bash "$MAC_SCRIPT" macslug --workdir "$M_WORK" --dry-run 2>&1 || true)
touch_hours_ago 13 "$M_WORK/HANDOFF.md"
out=$(HANDOFF_MAX_AGE_H=24 STUB_EMIT_LOG="$TMP/mac_emit.log" HOME="$TMP/mac-home" \
      bash "$MAC_SCRIPT" macslug --workdir "$M_WORK" --dry-run 2>&1 || true)
if echo "$out" | grep -q "HANDOFF.md present + fresh"; then
  pass "Mac: HANDOFF_MAX_AGE_H=24 override widens the window back for a 13h-old handoff"
else
  fail "Mac: HANDOFF_MAX_AGE_H override did not take effect. Output: $out"
fi

# --- 2e. dedup: two SKIPs same slug same day -> emitter called twice, but
#         with the IDENTICAL title (dedup key) so emit_kanban_item.sh's own
#         open-issue check would collapse them to one card -----------------
rm -f "$TMP/mac_emit.log"
run_mac "" --dry-run >/dev/null
run_mac 13 --dry-run >/dev/null
titles=$(grep -o 'title=session-rotate SKIP: macslug ([0-9-]*)' "$TMP/mac_emit.log" | sort -u | wc -l | tr -d ' ')
if [ "$titles" = "1" ]; then
  pass "Mac: repeat SKIPs same day produce the SAME dedup title (task+title key)"
else
  fail "Mac: repeat SKIPs did not share a dedup title: $(cat "$TMP/mac_emit.log")"
fi

# =============================================================================
# Group 3: VPS script (scripts/bubble-session-rotate.sh) -- exercised via a
# test-only copy with the hardcoded /srv/agents + /home/agent-* prefixes
# substituted for tmpdir paths (identical logic otherwise; no Linux/root
# available on this dev box). See file header.
# =============================================================================
V_BASE="$TMP/vps"
V_WORK="$V_BASE/agents/vpsslug"
V_HOME="$V_BASE/home/agent-vpsslug"
mkdir -p "$V_WORK" "$V_HOME/.claude/projects"
mk_stub_emitter "$V_WORK/tools/kanban"

VPS_TEST_COPY="$TMP/bubble-session-rotate.under-test.sh"
sed -e "s#workdir=\"/srv/agents/\${slug}\"#workdir=\"${V_BASE}/agents/\${slug}\"#" \
    -e "s#home=\"/home/agent-\${slug}\"#home=\"${V_BASE}/home/agent-\${slug}\"#" \
    "$VPS_SCRIPT" > "$VPS_TEST_COPY"
chmod +x "$VPS_TEST_COPY"
# Sanity: the substitution actually matched (else this test is silently
# testing nothing and would give false confidence).
if diff -q "$VPS_TEST_COPY" "$VPS_SCRIPT" >/dev/null 2>&1; then
  fail "VPS test copy: path substitution did not change anything -- script structure may have shifted"
fi

# STUBBIN: a GNU-stat shim (`stat -c %Y FILE`) ahead of PATH, since this
# script targets Linux/systemd and a BSD/macOS dev box's stat has no -c flag.
STUBBIN="$TMP/stubbin"
mkdir -p "$STUBBIN"
cat > "$STUBBIN/stat" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-c" ] && [ "$2" = "%Y" ]; then
  exec /usr/bin/stat -f %m "$3"
fi
exec /usr/bin/stat "$@"
EOF
chmod +x "$STUBBIN/stat"

run_vps() {
  local age="$1"; shift
  rm -f "$V_WORK/HANDOFF.md"
  if [ -n "$age" ]; then
    touch_hours_ago "$age" "$V_WORK/HANDOFF.md"
  fi
  PATH="$STUBBIN:$PATH" STUB_EMIT_LOG="$TMP/vps_emit.log" \
    bash "$VPS_TEST_COPY" vpsslug "$@" 2>&1 || true
}

# --- 3a. missing HANDOFF.md -> SKIP + alert ---------------------------------
rm -f "$TMP/vps_emit.log"
out=$(run_vps "" --dry-run)
if echo "$out" | grep -q "SKIP vpsslug: no HANDOFF.md"; then
  pass "VPS: missing HANDOFF.md -> SKIP"
else
  fail "VPS: missing HANDOFF.md did not SKIP. Output: $out"
fi
if [ -f "$TMP/vps_emit.log" ] && grep -q "title=session-rotate SKIP: vpsslug" "$TMP/vps_emit.log"; then
  pass "VPS: missing-HANDOFF SKIP alerted via emit_kanban_item.sh"
else
  fail "VPS: missing-HANDOFF SKIP did not alert"
fi

# --- 3b. 13h-old HANDOFF.md -> SKIPs under the new 12h default -------------
rm -f "$TMP/vps_emit.log"
out=$(run_vps 13 --dry-run)
if echo "$out" | grep -q "SKIP vpsslug: HANDOFF.md is 13h stale"; then
  pass "VPS: 13h-old HANDOFF.md SKIPs under the new 12h default"
else
  fail "VPS: 13h-old HANDOFF.md did not SKIP under the tightened default. Output: $out"
fi
if [ -f "$TMP/vps_emit.log" ] && grep -q "title=session-rotate SKIP: vpsslug" "$TMP/vps_emit.log"; then
  pass "VPS: stale-HANDOFF SKIP alerted via emit_kanban_item.sh"
else
  fail "VPS: stale-HANDOFF SKIP did not alert"
fi

# --- 3c. fresh HANDOFF.md (2h old) -> proceeds, no alert --------------------
rm -f "$TMP/vps_emit.log"
out=$(run_vps 2 --dry-run)
if echo "$out" | grep -q "HANDOFF.md present + fresh" && echo "$out" | grep -q "DRY-RUN vpsslug"; then
  pass "VPS: fresh HANDOFF.md proceeds (dry-run reaches the rotate step)"
else
  fail "VPS: fresh HANDOFF.md did not proceed. Output: $out"
fi
if [ ! -s "$TMP/vps_emit.log" ]; then
  pass "VPS: fresh HANDOFF.md did NOT alert (no SKIP)"
else
  fail "VPS: fresh HANDOFF.md unexpectedly alerted"
fi

# --- 3d. --force bypasses the gate entirely (no SKIP, no alert) -- pre-
#         existing behavior, guarded against regression -----------------
rm -f "$TMP/vps_emit.log"
out=$(run_vps "" --force --dry-run)
if echo "$out" | grep -q "SKIP"; then
  fail "VPS: --force should bypass the HANDOFF gate entirely. Output: $out"
else
  pass "VPS: --force bypasses the HANDOFF gate (no SKIP)"
fi

if [ "$FAILED" -eq 0 ]; then
  echo "ALL PASS"
  exit 0
else
  echo "SOME TESTS FAILED"
  exit 1
fi
