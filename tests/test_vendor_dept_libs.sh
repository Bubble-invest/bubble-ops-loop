#!/usr/bin/env bash
# =============================================================================
# test_vendor_dept_libs.sh — TDD harness for scripts/vendor-dept-libs.sh
#
# Runs entirely against THROWAWAY FIXTURES under a mktemp dir. NEVER touches
# real dept repos or /home/claude/. Every invocation builds a fake framework
# and a fake dept tree, then exercises the script's FRAMEWORK resolution logic
# and its copy/diff behaviour.
#
# Assertions:
#   T1  env override ($BUBBLE_FRAMEWORK_ROOT) wins when set.
#   T2  sibling layout (Mac host:local): framework resolved from dirname(dept).
#   T3  VPS fallback: verified by source inspection (grep for the hardcoded path
#       "/home/claude/bubble-ops-loop"); the live execution sub-test fires only on
#       a host where /home/claude/bubble-ops-loop exists (i.e. the VPS). We can't
#       write /home/claude on a Mac, so there is no fixture simulation here.
#   T4  fail-open: no framework anywhere → exits 0, logs WARN, nothing copied.
#   T5  missing dept dir → exits 0 (fail-open), nothing copied.
#   T6  end-to-end copy: stale lib in dept is refreshed from framework.
#   T7  idempotency: second run makes no new copies (cmp -s matches → skip).
# =============================================================================
set -uo pipefail

SCRIPT_UNDER_TEST="${1:?usage: test_vendor_dept_libs.sh <path-to-vendor-dept-libs.sh>}"
PASS=0; FAIL=0

chk() {   # chk <desc> <expected_rc> <actual_rc>
  if [[ "$2" == "$3" ]]; then
    echo "  PASS: $1 (rc=$3)"; PASS=$((PASS+1))
  else
    echo "  FAIL: $1 (expected rc=$2, got rc=$3)"; FAIL=$((FAIL+1))
  fi
}
chk_eq() { # chk_eq <desc> <expected> <actual>
  if [[ "$2" == "$3" ]]; then
    echo "  PASS: $1"; PASS=$((PASS+1))
  else
    echo "  FAIL: $1 (expected '$2', got '$3')"; FAIL=$((FAIL+1))
  fi
}
chk_contains() { # chk_contains <desc> <needle> <haystack>
  if echo "$3" | grep -q "$2"; then
    echo "  PASS: $1"; PASS=$((PASS+1))
  else
    echo "  FAIL: $1 (pattern '$2' not found in output)"; FAIL=$((FAIL+1))
  fi
}

FIX="$(mktemp -d /tmp/vendor-libs-fix.XXXXXX)"
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
  echo "# canonical notify_layer"     > "$dir/tools/notify_layer.py"
  echo "# SKILL.md"                   > "$dir/skills/emit-kanban-task/SKILL.md"
  printf '#!/bin/sh\necho emit\n'     > "$dir/skills/emit-kanban-task/scripts/emit.sh"
  chmod +x "$dir/skills/emit-kanban-task/scripts/emit.sh"
  printf '#!/bin/sh\necho kanban\n'   > "$dir/tools/kanban/emit_kanban_item.sh"
  chmod +x "$dir/tools/kanban/emit_kanban_item.sh"
}

# ── helper: build a minimal fake dept tree (git init so skip-worktree works) ──
make_dept() {
  local dir="$1"
  mkdir -p "$dir/scripts/lib" "$dir/tools"
  git -C "$dir" init -q 2>/dev/null
  git -C "$dir" config user.email "fixture@test"
  git -C "$dir" config user.name "fixture"
  # stale copy of dispatch_helpers (differs from canonical):
  echo "# stale dispatch_helpers" > "$dir/scripts/lib/dispatch_helpers.py"
  git -C "$dir" add -A
  git -C "$dir" commit -q -m "init dept"
}

# =============================================================================
# T1: $BUBBLE_FRAMEWORK_ROOT override wins even when a sibling exists
# =============================================================================
echo "== T1: env override wins =="
FW_OVERRIDE="$FIX/fw-override"; make_framework "$FW_OVERRIDE"
FW_SIBLING="$FIX/parent1/bubble-ops-loop"; make_framework "$FW_SIBLING"
DEPT1="$FIX/parent1/bubble-ops-miranda"; make_dept "$DEPT1"
out1="$(BUBBLE_FRAMEWORK_ROOT="$FW_OVERRIDE" \
         "$SCRIPT_UNDER_TEST" "$DEPT1" 2>&1)"
rc=$?; chk "T1 exits 0" 0 "$rc"
# The OVERRIDE framework's dispatch_helpers.py content is "# canonical…"
# Dept started with "# stale…" → if override was used it becomes "# canonical…"
GOT1="$(cat "$DEPT1/scripts/lib/dispatch_helpers.py")"
chk_eq "T1 re-vendored from override framework" "# canonical dispatch_helpers" "$GOT1"

# =============================================================================
# T2: sibling layout (Mac host:local) — no env var, sibling exists
# =============================================================================
echo "== T2: sibling (Mac host:local) resolution =="
PARENT2="$FIX/parent2"
FW_SIB2="$PARENT2/bubble-ops-loop"; make_framework "$FW_SIB2"
DEPT2="$PARENT2/bubble-ops-miranda";  make_dept "$DEPT2"
out2="$(unset BUBBLE_FRAMEWORK_ROOT; \
         "$SCRIPT_UNDER_TEST" "$DEPT2" 2>&1)"
rc=$?; chk "T2 exits 0" 0 "$rc"
GOT2="$(cat "$DEPT2/scripts/lib/dispatch_helpers.py")"
chk_eq "T2 re-vendored from sibling framework" "# canonical dispatch_helpers" "$GOT2"
chk_contains "T2 log mentions re-vendored" "re-vendored" "$out2"

# =============================================================================
# T3: VPS fallback — no env var, no sibling, /home/claude/bubble-ops-loop exists
#     We can't write /home/claude on a Mac, so we can't create a fixture for
#     this path. Instead: (a) we verify the fallback is coded in the script via
#     grep (source inspection), and (b) if /home/claude/bubble-ops-loop actually
#     exists on this host (VPS only), we run a live resolution sub-test.
# =============================================================================
echo "== T3: VPS fallback path included in candidate list =="
# We inspect the script source: confirm it references /home/claude/bubble-ops-loop
if grep -q "/home/claude/bubble-ops-loop" "$SCRIPT_UNDER_TEST"; then
  echo "  PASS: T3 VPS fallback path present in script"; PASS=$((PASS+1))
else
  echo "  FAIL: T3 /home/claude/bubble-ops-loop not found in script"; FAIL=$((FAIL+1))
fi
# Additionally: if /home/claude/bubble-ops-loop actually exists on this machine,
# run a real fallback test. Otherwise, confirm fail-open handles both missing candidates.
if [[ -d "/home/claude/bubble-ops-loop" ]]; then
  DEPT3="$FIX/no-parent-dept"; mkdir -p "$DEPT3/scripts/lib" "$DEPT3/tools"
  git -C "$DEPT3" init -q 2>/dev/null
  git -C "$DEPT3" config user.email "fixture@test"
  git -C "$DEPT3" config user.name "fixture"
  echo "# stale" > "$DEPT3/scripts/lib/dispatch_helpers.py"
  git -C "$DEPT3" add -A && git -C "$DEPT3" commit -q -m "init"
  out3="$(unset BUBBLE_FRAMEWORK_ROOT; "$SCRIPT_UNDER_TEST" "$DEPT3" 2>&1)"
  rc3=$?; chk "T3 VPS path resolves (live machine)" 0 "$rc3"
else
  echo "  SKIP T3 live VPS path check (/home/claude/bubble-ops-loop not present — not a VPS)"
fi

# =============================================================================
# T4: fail-open — no framework anywhere, exits 0
# =============================================================================
echo "== T4: fail-open when no framework resolves =="
# Use a dept in a parent dir where no bubble-ops-loop sibling exists, and
# /home/claude/bubble-ops-loop is absent on this machine.
DEPT4="$FIX/isolated/bubble-ops-testdept"; mkdir -p "$DEPT4/scripts/lib"
out4="$(unset BUBBLE_FRAMEWORK_ROOT; \
         "$SCRIPT_UNDER_TEST" "$DEPT4" 2>&1)"
rc=$?; chk "T4 exits 0 (fail-open)" 0 "$rc"
chk_contains "T4 logs WARN about missing framework" "WARN" "$out4"

# =============================================================================
# T5: missing dept arg — exits 0, fail-open
# =============================================================================
echo "== T5: missing dept arg exits 0 =="
out5="$(unset BUBBLE_FRAMEWORK_ROOT; \
         "$SCRIPT_UNDER_TEST" "" 2>&1)"
rc=$?; chk "T5 exits 0" 0 "$rc"
chk_contains "T5 logs WARN about missing dept" "WARN" "$out5"

# =============================================================================
# T6: end-to-end copy — stale lib gets refreshed
# =============================================================================
echo "== T6: stale lib is refreshed from framework =="
PARENT6="$FIX/parent6"
FW6="$PARENT6/bubble-ops-loop"; make_framework "$FW6"
DEPT6="$PARENT6/bubble-ops-miranda"; make_dept "$DEPT6"
# Confirm stale before:
PRE6="$(cat "$DEPT6/scripts/lib/dispatch_helpers.py")"
chk_eq "T6 pre-run: dept has stale copy" "# stale dispatch_helpers" "$PRE6"
unset BUBBLE_FRAMEWORK_ROOT
"$SCRIPT_UNDER_TEST" "$DEPT6" >/dev/null 2>&1
POST6="$(cat "$DEPT6/scripts/lib/dispatch_helpers.py")"
chk_eq "T6 post-run: dept has canonical copy" "# canonical dispatch_helpers" "$POST6"

# =============================================================================
# T7: idempotency — second run does not re-copy (cmp -s matches)
# =============================================================================
echo "== T7: idempotency — second run skips already-synced files =="
PARENT7="$FIX/parent7"
FW7="$PARENT7/bubble-ops-loop"; make_framework "$FW7"
DEPT7="$PARENT7/bubble-ops-miranda"; make_dept "$DEPT7"
unset BUBBLE_FRAMEWORK_ROOT
"$SCRIPT_UNDER_TEST" "$DEPT7" >/dev/null 2>&1   # first run (sync)
out7="$("$SCRIPT_UNDER_TEST" "$DEPT7" 2>&1)"      # second run (idempotent)
# On idempotent run, vendored count in the log should be "0 file(s) refreshed"
chk_contains "T7 second run reports 0 files refreshed" "0 file(s) refreshed" "$out7"

# =============================================================================
# T8 (board #1115): /opt/bubble-ops-loop (root-owned checkout) candidate
#     present in resolution order, checked ahead of the legacy /home/claude
#     fallback. We can't create /opt on most CI/dev boxes without root, so
#     (like T3's VPS-fallback check) this is source inspection plus a live
#     sub-test IF /opt/bubble-ops-loop happens to already exist.
# =============================================================================
echo "== T8: root-owned /opt/bubble-ops-loop candidate (board #1115) =="
if grep -q "/opt/bubble-ops-loop" "$SCRIPT_UNDER_TEST"; then
  echo "  PASS: T8 /opt/bubble-ops-loop candidate present in script"; PASS=$((PASS+1))
else
  echo "  FAIL: T8 /opt/bubble-ops-loop not found in script"; FAIL=$((FAIL+1))
fi
# Ordering: the /opt candidate must appear BEFORE the /home/claude fallback
# in source (best-effort proxy for "checked first" — the real guarantee is
# the runtime resolution below when both exist).
_opt_idx="$(grep -n '"/opt/bubble-ops-loop"' "$SCRIPT_UNDER_TEST" | head -1 | cut -d: -f1)"
_vps_idx="$(grep -n '"/home/claude/bubble-ops-loop"' "$SCRIPT_UNDER_TEST" | head -1 | cut -d: -f1)"
if [[ -n "$_opt_idx" && -n "$_vps_idx" && "$_opt_idx" -lt "$_vps_idx" ]]; then
  echo "  PASS: T8 /opt candidate appears before /home/claude fallback in source"; PASS=$((PASS+1))
else
  echo "  FAIL: T8 /opt candidate does not precede /home/claude fallback (opt@$_opt_idx vps@$_vps_idx)"; FAIL=$((FAIL+1))
fi
if [[ -d "/opt/bubble-ops-loop" ]]; then
  DEPT8="$FIX/opt-priority-dept"; mkdir -p "$DEPT8/scripts/lib"
  git -C "$DEPT8" init -q 2>/dev/null
  git -C "$DEPT8" config user.email "fixture@test"
  git -C "$DEPT8" config user.name "fixture"
  echo "# stale" > "$DEPT8/scripts/lib/dispatch_helpers.py"
  git -C "$DEPT8" add -A && git -C "$DEPT8" commit -q -m "init"
  out8="$(unset BUBBLE_FRAMEWORK_ROOT; "$SCRIPT_UNDER_TEST" "$DEPT8" 2>&1)"
  rc8=$?; chk "T8 /opt path resolves (live machine)" 0 "$rc8"
else
  echo "  SKIP T8 live /opt/bubble-ops-loop check (not present on this host)"
fi

# =============================================================================
# T9 (board #1115): a symlink at DEST must be REFUSED, never written through.
# =============================================================================
echo "== T9: symlink DEST is refused, not written through =="
PARENT9="$FIX/parent9"
FW9="$PARENT9/bubble-ops-loop"; make_framework "$FW9"
DEPT9="$PARENT9/bubble-ops-miranda"; make_dept "$DEPT9"
# Replace the dept's dispatch_helpers.py with a symlink pointing at a
# canary file OUTSIDE the dept tree — if the script ever writes through it
# (instead of refusing), the canary's content changes.
CANARY="$FIX/canary.txt"
echo "canary — must never change" > "$CANARY"
rm -f "$DEPT9/scripts/lib/dispatch_helpers.py"
ln -s "$CANARY" "$DEPT9/scripts/lib/dispatch_helpers.py"
out9="$(unset BUBBLE_FRAMEWORK_ROOT; "$SCRIPT_UNDER_TEST" "$DEPT9" 2>&1)"
rc9=$?
chk "T9 exits 0 (fail-open, not a hard failure)" 0 "$rc9"
chk_contains "T9 logs a refusal, not a silent write-through" "refusing" "$out9"
CANARY_AFTER="$(cat "$CANARY")"
chk_eq "T9 canary file untouched (symlink target NOT written through)" \
  "canary — must never change" "$CANARY_AFTER"
if [[ -L "$DEPT9/scripts/lib/dispatch_helpers.py" ]]; then
  echo "  PASS: T9 dest is left as a symlink (refused, not replaced)"; PASS=$((PASS+1))
else
  echo "  FAIL: T9 dest symlink was removed/replaced"; FAIL=$((FAIL+1))
fi

# =============================================================================
echo
echo "RESULTS: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
