#!/usr/bin/env bash
# =============================================================================
# test_revendor_all_depts.sh — TDD harness for scripts/revendor-all-depts.sh (#419)
#
# THE GAP this script fixes: vendor-dept-libs.sh self-heals a dept's vendored
# framework libs only at that dept's SERVICE START. A merged framework fix sits
# un-propagated on a running dept until its next restart. revendor-all-depts.sh
# is the proactive sweep: given a framework root + agents root, it discovers
# every host:vps dept dir and runs vendor-dept-libs.sh for each; host:local
# depts (VPS holds a read-only mirror only) are skipped.
#
# Runs entirely against THROWAWAY FIXTURES under a mktemp dir. NEVER touches
# real dept repos or /home/claude/. Every invocation passes explicit
# --framework / --agents-root pointing at the fixture tree.
#
# Assertions:
#   T1  sweep refreshes a stale vendored lib in BOTH fixture depts.
#   T2  --dry-run reports what WOULD be refreshed and changes NOTHING on disk.
#   T3  a missing/malformed dept dir (no scripts/lib) is skipped, not fatal —
#       the sweep still exits 0 and still processes the OTHER good dept.
#   T4  host:local dept is SKIPPED entirely (never re-vendored), host:vps
#       (and host-absent, defaulting to vps) dept in the same sweep IS.
#   T5  idempotency: a second real run makes no further changes (already in
#       sync — vendor-dept-libs.sh's own cmp -s skip kicks in).
#   T6  fixture safety: agents-root used here is a throwaway, not the live one.
# =============================================================================
set -uo pipefail

SCRIPT_UNDER_TEST="${1:?usage: test_revendor_all_depts.sh <path-to-revendor-all-depts.sh>}"
[[ -f "$SCRIPT_UNDER_TEST" ]] || { echo "FATAL: script not found: $SCRIPT_UNDER_TEST"; exit 2; }
VENDOR_SCRIPT="$(dirname "$SCRIPT_UNDER_TEST")/vendor-dept-libs.sh"
[[ -f "$VENDOR_SCRIPT" ]] || { echo "FATAL: sibling vendor-dept-libs.sh not found: $VENDOR_SCRIPT"; exit 2; }

PASS=0; FAIL=0
chk()        { if [[ "$2" == "$3" ]]; then echo "  PASS: $1 (rc=$3)"; PASS=$((PASS+1)); else echo "  FAIL: $1 (expected rc=$2, got rc=$3)"; FAIL=$((FAIL+1)); fi; }
chk_eq()     { if [[ "$2" == "$3" ]]; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (expected '$2', got '$3')"; FAIL=$((FAIL+1)); fi; }
chk_contains(){ if echo "$3" | grep -q "$2"; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (pattern '$2' not found)"; FAIL=$((FAIL+1)); fi; }

FIX="$(mktemp -d /tmp/revendor-all-fix.XXXXXX)"
trap 'rm -rf "$FIX"' EXIT

# ── helper: build a minimal fake framework ────────────────────────────────────
make_framework() {
  local dir="$1"
  mkdir -p "$dir/scripts/lib" "$dir/tools" \
           "$dir/skills/emit-kanban-task/scripts" \
           "$dir/tools/kanban"
  echo "# canonical dispatch_helpers" > "$dir/scripts/lib/dispatch_helpers.py"
  echo "# canonical notify"           > "$dir/scripts/lib/notify.py"
  echo "# canonical loop_notify"      > "$dir/scripts/lib/loop_notify.py"
  echo "# canonical notion_logbook"   > "$dir/scripts/lib/notion_logbook.py"
  echo "# canonical budget"           > "$dir/scripts/lib/budget.py"
  echo "# canonical notify_layer"     > "$dir/tools/notify_layer.py"
  echo "# SKILL.md"                   > "$dir/skills/emit-kanban-task/SKILL.md"
  printf '#!/bin/sh\necho emit\n'     > "$dir/skills/emit-kanban-task/scripts/emit.sh"
  chmod +x "$dir/skills/emit-kanban-task/scripts/emit.sh"
  printf '#!/bin/sh\necho kanban\n'   > "$dir/tools/kanban/emit_kanban_item.sh"
  chmod +x "$dir/tools/kanban/emit_kanban_item.sh"
}

# ── helper: build a minimal fake dept tree with a STALE lib (git init so
#    vendor-dept-libs.sh's skip-worktree step has a repo to act on) ───────────
make_dept() {  # make_dept <dir> [host]
  local dir="$1" host="${2:-}"
  mkdir -p "$dir/scripts/lib" "$dir/tools"
  git -C "$dir" init -q 2>/dev/null
  git -C "$dir" config user.email "fixture@test"
  git -C "$dir" config user.name "fixture"
  echo "# stale dispatch_helpers" > "$dir/scripts/lib/dispatch_helpers.py"
  git -C "$dir" add -A
  git -C "$dir" commit -q -m "init dept"
  if [[ -n "$host" ]]; then
    mkdir -p "$dir/onboarding"
    printf 'slug: %s\nstatus: Live\nhost: %s\n' "$(basename "$dir")" "$host" > "$dir/onboarding/STATE.yaml"
  fi
}

# =============================================================================
# T1: sweep refreshes stale lib in BOTH fixture depts
# =============================================================================
echo "== T1: sweep refreshes stale vendored files in both fixture depts =="
FW1="$FIX/t1/framework"; make_framework "$FW1"
AGENTS1="$FIX/t1/agents"
make_dept "$AGENTS1/bubble-ops-alpha"
make_dept "$AGENTS1/bubble-ops-beta"

out1="$("$SCRIPT_UNDER_TEST" --framework "$FW1" --agents-root "$AGENTS1" 2>&1)"
rc1=$?
chk "T1 exits 0" 0 "$rc1"
GOT_A="$(cat "$AGENTS1/bubble-ops-alpha/scripts/lib/dispatch_helpers.py")"
GOT_B="$(cat "$AGENTS1/bubble-ops-beta/scripts/lib/dispatch_helpers.py")"
chk_eq "T1 alpha re-vendored" "# canonical dispatch_helpers" "$GOT_A"
chk_eq "T1 beta re-vendored"  "# canonical dispatch_helpers" "$GOT_B"
chk_contains "T1 summary line reports both depts" "depts_total=2" "$out1"

# =============================================================================
# T2: --dry-run reports what would change, changes NOTHING on disk
# =============================================================================
echo "== T2: --dry-run is read-only =="
FW2="$FIX/t2/framework"; make_framework "$FW2"
AGENTS2="$FIX/t2/agents"
make_dept "$AGENTS2/bubble-ops-gamma"
PRE2="$(cat "$AGENTS2/bubble-ops-gamma/scripts/lib/dispatch_helpers.py")"

out2="$("$SCRIPT_UNDER_TEST" --framework "$FW2" --agents-root "$AGENTS2" --dry-run 2>&1)"
rc2=$?
chk "T2 exits 0" 0 "$rc2"
POST2="$(cat "$AGENTS2/bubble-ops-gamma/scripts/lib/dispatch_helpers.py")"
chk_eq "T2 dept file UNCHANGED after dry-run" "$PRE2" "$POST2"
chk_contains "T2 reports what would be refreshed" "would re-vendor" "$out2"
chk_contains "T2 names the stale file" "dispatch_helpers.py" "$out2"
chk_contains "T2 log line marked dry-run" "dry-run" "$out2"

# =============================================================================
# T3: a dept dir with no scripts/lib (missing dest dir) is a fail-open no-op,
#     not fatal — sweep still exits 0 and still processes the OTHER good dept.
# =============================================================================
echo "== T3: missing dest dir (no scripts/lib) is a fail-open no-op =="
FW3="$FIX/t3/framework"; make_framework "$FW3"
AGENTS3="$FIX/t3/agents"
make_dept "$AGENTS3/bubble-ops-good"
# "bad" dept: dir exists but has no scripts/lib (nothing to vendor into) — this
# must not abort the sweep or crash on the good dept.
mkdir -p "$AGENTS3/bubble-ops-bad-empty"

"$SCRIPT_UNDER_TEST" --framework "$FW3" --agents-root "$AGENTS3" >/dev/null 2>&1
rc3=$?
chk "T3 exits 0 despite a broken dept dir" 0 "$rc3"
GOT_GOOD="$(cat "$AGENTS3/bubble-ops-good/scripts/lib/dispatch_helpers.py")"
chk_eq "T3 good dept still re-vendored" "# canonical dispatch_helpers" "$GOT_GOOD"

# =============================================================================
# T4: host:local dept is SKIPPED; host:vps / host-absent dept IS re-vendored.
# =============================================================================
echo "== T4: host:local skipped, host:vps (and host-absent) re-vendored =="
FW4="$FIX/t4/framework"; make_framework "$FW4"
AGENTS4="$FIX/t4/agents"
make_dept "$AGENTS4/bubble-ops-remotemac" local
make_dept "$AGENTS4/bubble-ops-onvps"     vps
make_dept "$AGENTS4/bubble-ops-noflag"          # no host field -> defaults vps

out4="$("$SCRIPT_UNDER_TEST" --framework "$FW4" --agents-root "$AGENTS4" 2>&1)"
rc4=$?
chk "T4 exits 0" 0 "$rc4"
LOCAL_AFTER="$(cat "$AGENTS4/bubble-ops-remotemac/scripts/lib/dispatch_helpers.py")"
VPS_AFTER="$(cat "$AGENTS4/bubble-ops-onvps/scripts/lib/dispatch_helpers.py")"
NOFLAG_AFTER="$(cat "$AGENTS4/bubble-ops-noflag/scripts/lib/dispatch_helpers.py")"
chk_eq "T4 host:local dept NOT re-vendored (still stale)" "# stale dispatch_helpers" "$LOCAL_AFTER"
chk_eq "T4 host:vps dept re-vendored" "# canonical dispatch_helpers" "$VPS_AFTER"
chk_eq "T4 host-absent dept defaults to vps and IS re-vendored" "# canonical dispatch_helpers" "$NOFLAG_AFTER"
chk_contains "T4 log names the skipped local dept" "skip remotemac" "$out4"

# =============================================================================
# T5: idempotency — a second real run makes no further changes
# =============================================================================
echo "== T5: second run is idempotent (already in sync) =="
FW5="$FIX/t5/framework"; make_framework "$FW5"
AGENTS5="$FIX/t5/agents"
make_dept "$AGENTS5/bubble-ops-delta"
"$SCRIPT_UNDER_TEST" --framework "$FW5" --agents-root "$AGENTS5" >/dev/null 2>&1   # first run
out5="$("$SCRIPT_UNDER_TEST" --framework "$FW5" --agents-root "$AGENTS5" 2>&1)"     # second run
rc5=$?
chk "T5 exits 0" 0 "$rc5"
chk_contains "T5 second run reports 0 file(s) refreshed for the dept" "0 file(s) refreshed" "$out5"

# =============================================================================
# T6: fixture safety — the agents-root used throughout is a throwaway.
# =============================================================================
echo "== T6: fixture agents-root is a throwaway, not the live one =="
case "$AGENTS1" in
  /home/claude/agents) echo "  FAIL: fixture pointed at LIVE agents root!"; FAIL=$((FAIL+1));;
  *) echo "  PASS: T6 agents-root is a throwaway fixture ($AGENTS1)"; PASS=$((PASS+1));;
esac

echo
echo "RESULTS: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
