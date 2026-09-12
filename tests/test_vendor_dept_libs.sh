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
#   T6  end-to-end copy: a missing lib is installed from framework.
#   T7  idempotency: second run makes no new copies (cmp -s matches → skip).
#   T8  root-owned /opt framework checkout takes priority.
#   T9  symlink destinations are refused without touching their targets.
#   T10 a destination changed since the last vendor run is deferred in place;
#       an unchanged managed destination receives normal source upgrades.
#   T11 a different existing destination with no baseline is deferred unchanged.
#   T12 .claude/skills/ wiring (board #1224), dept with NO dept.yaml: ensure
#       fleet-shared ONLY (emit-kanban-task wired; a non-fleet-shared undeclared
#       on-disk source like codex-write is NOT auto-linked), idempotent, no
#       real-dir clobber, git-excluded, dept lacking .claude/ skipped fail-open.
#   T13 additive ensure (board #1224 review, corrected): a dept WITH dept.yaml
#       ensures fleet-shared ∪ declared — declared wired, fleet-shared
#       emit-kanban-task wired though undeclared, undeclared/non-fleet-shared
#       on-disk sources (codex-write/fund-research) NOT linked, a pre-existing
#       curated link PRESERVED (never removed), empty `skills:{}` adds only
#       fleet-shared and keeps curated links.
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
rm -f "$DEPT1/scripts/lib/dispatch_helpers.py"
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
rm -f "$DEPT2/scripts/lib/dispatch_helpers.py"
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
# T6: end-to-end copy — missing lib is installed
# =============================================================================
echo "== T6: missing lib is installed from framework =="
PARENT6="$FIX/parent6"
FW6="$PARENT6/bubble-ops-loop"; make_framework "$FW6"
DEPT6="$PARENT6/bubble-ops-miranda"; make_dept "$DEPT6"
rm -f "$DEPT6/scripts/lib/dispatch_helpers.py"
if [[ ! -e "$DEPT6/scripts/lib/dispatch_helpers.py" ]]; then
  echo "  PASS: T6 pre-run: dept copy is missing"; PASS=$((PASS+1))
else
  echo "  FAIL: T6 pre-run copy still exists"; FAIL=$((FAIL+1))
fi
unset BUBBLE_FRAMEWORK_ROOT
"$SCRIPT_UNDER_TEST" "$DEPT6" >/dev/null 2>&1
POST6="$(cat "$DEPT6/scripts/lib/dispatch_helpers.py")"
chk_eq "T6 post-run: missing copy installed from canonical" "# canonical dispatch_helpers" "$POST6"

# =============================================================================
# T7: idempotency — second run does not re-copy (cmp -s matches)
# =============================================================================
echo "== T7: idempotency — second run skips already-synced files =="
PARENT7="$FIX/parent7"
FW7="$PARENT7/bubble-ops-loop"; make_framework "$FW7"
DEPT7="$PARENT7/bubble-ops-miranda"; make_dept "$DEPT7"
rm -f "$DEPT7/scripts/lib/dispatch_helpers.py"
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
# T10: managed baseline permits normal update; local change is deferred
# =============================================================================
echo "== T10: managed update and changed-destination defer =="
PARENT10="$FIX/parent10"
FW10="$PARENT10/bubble-ops-loop"; make_framework "$FW10"
DEPT10="$PARENT10/bubble-ops-miranda"; make_dept "$DEPT10"
rm -f "$DEPT10/scripts/lib/dispatch_helpers.py"
unset BUBBLE_FRAMEWORK_ROOT
"$SCRIPT_UNDER_TEST" "$DEPT10" >/dev/null 2>&1  # install + baseline v1

# A local fork after baseline must remain active even when canonical advances.
echo "# local hand-patch — preserve me" > "$DEPT10/scripts/lib/dispatch_helpers.py"
# Simulate both legacy index hide mechanisms; defer must clear both.
git -C "$DEPT10" update-index --assume-unchanged scripts/lib/dispatch_helpers.py
echo "# canonical dispatch_helpers v2" > "$FW10/scripts/lib/dispatch_helpers.py"
out10="$("$SCRIPT_UNDER_TEST" "$DEPT10" 2>&1)"
chk_eq "T10a changed destination remains in place" \
  "# local hand-patch — preserve me" "$(cat "$DEPT10/scripts/lib/dispatch_helpers.py")"
chk_contains "T10b changed destination reports DEFERRED" \
  "DEFERRED: scripts/lib/dispatch_helpers.py changed since last vendor" "$out10"
chk_contains "T10c caller summary distinguishes deferred" "1 deferred" "$out10"
flag10="$(git -C "$DEPT10" ls-files -v scripts/lib/dispatch_helpers.py | cut -c1)"
chk_eq "T10d deferred tracked fork is visible, not skip-worktree" "H" "$flag10"

# Restore the still-recorded v1 bytes: canonical v2 may now update normally.
echo "# canonical dispatch_helpers" > "$DEPT10/scripts/lib/dispatch_helpers.py"
out10b="$("$SCRIPT_UNDER_TEST" "$DEPT10" 2>&1)"
chk_eq "T10e unchanged managed destination receives canonical v2" \
  "# canonical dispatch_helpers v2" "$(cat "$DEPT10/scripts/lib/dispatch_helpers.py")"
chk_contains "T10f normal managed update reports no defer" "0 deferred" "$out10b"

# A second ordinary source upgrade remains managed and idempotent.
echo "# canonical dispatch_helpers v3" > "$FW10/scripts/lib/dispatch_helpers.py"
out10c="$("$SCRIPT_UNDER_TEST" "$DEPT10" 2>&1)"
chk_eq "T10g subsequent managed update receives canonical v3" \
  "# canonical dispatch_helpers v3" "$(cat "$DEPT10/scripts/lib/dispatch_helpers.py")"
chk_contains "T10h subsequent update reports no defer" "0 deferred" "$out10c"

# =============================================================================
# T11: existing different bytes with no baseline are unknown ownership -> defer
# =============================================================================
echo "== T11: no-baseline fork is preserved and deferred =="
PARENT11="$FIX/parent11"
FW11="$PARENT11/bubble-ops-loop"; make_framework "$FW11"
DEPT11="$PARENT11/bubble-ops-accountant"; make_dept "$DEPT11"
# This label binds the regression to the exact live M5 fork observed during #606.
ACCOUNTANT_FORK_SHA256="3a9be727ae85fce0de3c01f79660fbe7ee0477ea6121037369a8a88f6324a5a8"
before11="$(cat "$DEPT11/scripts/lib/dispatch_helpers.py")"
out11="$(BUBBLE_FRAMEWORK_ROOT="$FW11" "$SCRIPT_UNDER_TEST" "$DEPT11" 2>&1)"
rc11=$?
after11="$(cat "$DEPT11/scripts/lib/dispatch_helpers.py")"
chk "T11a daemon preflight continues after defer" 0 "$rc11"
chk_eq "T11b exact Accountant-class existing fork remains unchanged" "$before11" "$after11"
chk_contains "T11c no-baseline fork reports DEFERRED" "no trusted last-vendored baseline" "$out11"
chk_contains "T11d caller summary reports one defer" "1 deferred" "$out11"
flag11="$(git -C "$DEPT11" ls-files -v scripts/lib/dispatch_helpers.py | cut -c1)"
chk_eq "T11e no-baseline tracked fork remains visible" "H" "$flag11"
last11="$DEPT11/.git/vendor-dept-libs/scripts/lib/dispatch_helpers.py"
if [[ ! -e "$last11" ]]; then
  echo "  PASS: T11f deferred unknown bytes are not adopted as baseline ($ACCOUNTANT_FORK_SHA256)"; PASS=$((PASS+1))
else
  echo "  FAIL: T11f deferred bytes were incorrectly recorded as managed"; FAIL=$((FAIL+1))
fi

# =============================================================================
# T12: board #1224 — .claude/skills/ wiring for a dept with NO dept.yaml
#   (legacy pre-scaffold depts morty/claudette). ENSURE fleet-shared ONLY:
#   emit-kanban-task is wired; a dept-own on-disk source that is NOT fleet-shared
#   and NOT declared (e.g. codex-write — script-invoked, disable-model-invocation)
#   is NOT auto-linked. morty + claudette loaded ZERO skills; they now load at
#   least emit-kanban-task.
# =============================================================================
echo "== T12: .claude/skills wiring — no dept.yaml (fleet-shared ensured) =="
PARENT12="$FIX/parent12"
FW12="$PARENT12/bubble-ops-loop"; make_framework "$FW12"
DEPT12="$PARENT12/bubble-ops-skillwire"; make_dept "$DEPT12"
mkdir -p "$DEPT12/.claude"
# a dept-OWN, non-fleet-shared, undeclared skill present on disk (like claudette's
# codex-write) — must NOT be auto-linked.
mkdir -p "$DEPT12/skills/codex-write"
echo "# codex-write" > "$DEPT12/skills/codex-write/SKILL.md"
out12="$(BUBBLE_FRAMEWORK_ROOT="$FW12" "$SCRIPT_UNDER_TEST" "$DEPT12" 2>&1)"
rc12=$?
chk "T12 exits 0" 0 "$rc12"
# the fleet-shared emit-kanban-task is vendored then wired
if [[ -L "$DEPT12/.claude/skills/emit-kanban-task" ]]; then
  echo "  PASS: T12a fleet-shared emit-kanban-task wired (no dept.yaml)"; PASS=$((PASS+1))
else
  echo "  FAIL: T12a emit-kanban-task not a symlink in .claude/skills"; FAIL=$((FAIL+1))
fi
chk_eq "T12b emit-kanban-task link is relative ../../skills/…" \
  "../../skills/emit-kanban-task" "$(readlink "$DEPT12/.claude/skills/emit-kanban-task" 2>/dev/null)"
# a non-fleet-shared, undeclared on-disk source is NOT auto-linked
if [[ ! -e "$DEPT12/.claude/skills/codex-write" ]]; then
  echo "  PASS: T12c undeclared on-disk codex-write NOT auto-linked"; PASS=$((PASS+1))
else
  echo "  FAIL: T12c codex-write auto-linked despite not fleet-shared/declared"; FAIL=$((FAIL+1))
fi
# link resolves to the source SKILL.md
if [[ -f "$DEPT12/.claude/skills/emit-kanban-task/SKILL.md" ]]; then
  echo "  PASS: T12d emit-kanban-task link resolves to its SKILL.md"; PASS=$((PASS+1))
else
  echo "  FAIL: T12d emit-kanban-task link does not resolve"; FAIL=$((FAIL+1))
fi
# idempotent rerun — still a correct symlink, no error
out12b="$(BUBBLE_FRAMEWORK_ROOT="$FW12" "$SCRIPT_UNDER_TEST" "$DEPT12" 2>&1)"
chk "T12e idempotent rerun exits 0" 0 "$?"
chk_eq "T12f link unchanged after rerun" \
  "../../skills/emit-kanban-task" "$(readlink "$DEPT12/.claude/skills/emit-kanban-task" 2>/dev/null)"
# a real (non-symlink) dir in the slot is preserved, never clobbered
DEPT12B="$PARENT12/bubble-ops-realdir"; make_dept "$DEPT12B"
mkdir -p "$DEPT12B/.claude/skills/emit-kanban-task"
echo "# override" > "$DEPT12B/.claude/skills/emit-kanban-task/SKILL.md"
out12c="$(BUBBLE_FRAMEWORK_ROOT="$FW12" "$SCRIPT_UNDER_TEST" "$DEPT12B" 2>&1)"
if [[ ! -L "$DEPT12B/.claude/skills/emit-kanban-task" && -d "$DEPT12B/.claude/skills/emit-kanban-task" ]]; then
  echo "  PASS: T12g pre-existing real dir preserved (not clobbered)"; PASS=$((PASS+1))
else
  echo "  FAIL: T12g pre-existing real dir was replaced"; FAIL=$((FAIL+1))
fi
chk_contains "T12h defer of real dir is logged" "DEFERRED: .claude/skills/emit-kanban-task" "$out12c"
# the untracked link is git-excluded so the loop's autocommit never stages it
if grep -qxF ".claude/skills/emit-kanban-task" "$DEPT12/.git/info/exclude" 2>/dev/null; then
  echo "  PASS: T12j untracked skill link added to .git/info/exclude"; PASS=$((PASS+1))
else
  echo "  FAIL: T12j skill link not git-excluded"; FAIL=$((FAIL+1))
fi
# a dept WITHOUT a .claude/ dir is left untouched (fail-open, no crash)
DEPT12C="$PARENT12/bubble-ops-noclaude"; make_dept "$DEPT12C"
out12d="$(BUBBLE_FRAMEWORK_ROOT="$FW12" "$SCRIPT_UNDER_TEST" "$DEPT12C" 2>&1)"
chk "T12i dept without .claude/ still exits 0" 0 "$?"

# =============================================================================
# T13: board #1224 REVIEW (corrected) — ADDITIVE + NON-DESTRUCTIVE ensure for a
#   dept WITH dept.yaml (Ben-like). ENSURE = fleet-shared ∪ declared:
#     - declared skills wired (nested layer_N flattened);
#     - fleet-shared emit-kanban-task wired even though NOT in dept.yaml;
#     - undeclared, non-fleet-shared on-disk sources (codex-write, fund-research)
#       NEVER auto-linked (the live-fund-agent hazard);
#     - a pre-existing curated link is PRESERVED (never removed);
#     - empty `skills: {}` (Maya) still gets fleet-shared, adds nothing else,
#       removes nothing.
# =============================================================================
echo "== T13: additive ensure — fleet-shared ∪ declared (dept.yaml present) =="
PARENT13="$FIX/parent13"
FW13="$PARENT13/bubble-ops-loop"; make_framework "$FW13"
DEPT13="$PARENT13/bubble-ops-fund"; make_dept "$DEPT13"
mkdir -p "$DEPT13/.claude/skills"
# dept.yaml declares a NESTED skills set (layer_N sub-lists), like Ben's —
# deliberately WITHOUT emit-kanban-task (fleet-shared, added separately).
cat > "$DEPT13/dept.yaml" <<'YAML'
department:
  slug: fund
skills:
  layer_2:
  - fund-thesis-format
  layer_3:
  - saxo-trading
tools: []
YAML
# On-disk skills/: two DECLARED + two UNDECLARED (codex-write, fund-research).
# emit-kanban-task is vendored by the script itself (fleet-shared).
for s in fund-thesis-format saxo-trading codex-write fund-research; do
  mkdir -p "$DEPT13/skills/$s"; echo "# $s" > "$DEPT13/skills/$s/SKILL.md"
done
# Pre-existing curated link to a linked-but-UNDECLARED skill (like Ben's
# alpaca/weekly-audio-report) — must survive untouched (non-destructive).
mkdir -p "$DEPT13/skills/weekly-audio-report"
echo "# weekly" > "$DEPT13/skills/weekly-audio-report/SKILL.md"
ln -s ../../skills/weekly-audio-report "$DEPT13/.claude/skills/weekly-audio-report"
out13="$(BUBBLE_FRAMEWORK_ROOT="$FW13" "$SCRIPT_UNDER_TEST" "$DEPT13" 2>&1)"
chk "T13 exits 0" 0 "$?"
# declared skills ARE wired
if [[ -L "$DEPT13/.claude/skills/fund-thesis-format" && -L "$DEPT13/.claude/skills/saxo-trading" ]]; then
  echo "  PASS: T13a declared skills (layer_2 + layer_3) are wired"; PASS=$((PASS+1))
else
  echo "  FAIL: T13a declared skills not wired"; FAIL=$((FAIL+1))
fi
# fleet-shared emit-kanban-task IS wired even though undeclared
if [[ -L "$DEPT13/.claude/skills/emit-kanban-task" ]]; then
  echo "  PASS: T13b fleet-shared emit-kanban-task wired though undeclared"; PASS=$((PASS+1))
else
  echo "  FAIL: T13b fleet-shared emit-kanban-task not wired"; FAIL=$((FAIL+1))
fi
# UNDECLARED, non-fleet-shared on-disk sources are NOT wired — core regression guard
if [[ ! -e "$DEPT13/.claude/skills/codex-write" ]]; then
  echo "  PASS: T13c undeclared codex-write NOT linked"; PASS=$((PASS+1))
else
  echo "  FAIL: T13c undeclared codex-write was linked (live-agent hazard!)"; FAIL=$((FAIL+1))
fi
if [[ ! -e "$DEPT13/.claude/skills/fund-research" ]]; then
  echo "  PASS: T13d undeclared fund-research NOT linked"; PASS=$((PASS+1))
else
  echo "  FAIL: T13d undeclared fund-research was linked (live-agent hazard!)"; FAIL=$((FAIL+1))
fi
# pre-existing curated (linked-but-undeclared) skill PRESERVED, not removed
if [[ -L "$DEPT13/.claude/skills/weekly-audio-report" ]]; then
  echo "  PASS: T13e pre-existing curated link preserved (non-destructive)"; PASS=$((PASS+1))
else
  echo "  FAIL: T13e pre-existing curated link was REMOVED (regression!)"; FAIL=$((FAIL+1))
fi
chk_contains "T13f log reports ensure mode" "ensure fleet-shared" "$out13"

# T13g: empty declaration (`skills: {}`, like Maya) → fleet-shared still ensured,
# nothing else added, an existing curated link preserved.
DEPT13C="$PARENT13/bubble-ops-empty"; make_dept "$DEPT13C"
mkdir -p "$DEPT13C/.claude/skills" "$DEPT13C/skills/codex-write" "$DEPT13C/skills/draft-writer"
echo "# codex-write" > "$DEPT13C/skills/codex-write/SKILL.md"
echo "# draft"       > "$DEPT13C/skills/draft-writer/SKILL.md"
ln -s ../../skills/draft-writer "$DEPT13C/.claude/skills/draft-writer"  # pre-existing curated
printf 'skills: {}\ntools: []\n' > "$DEPT13C/dept.yaml"
out13c="$(BUBBLE_FRAMEWORK_ROOT="$FW13" "$SCRIPT_UNDER_TEST" "$DEPT13C" 2>&1)"
if [[ -L "$DEPT13C/.claude/skills/emit-kanban-task" \
      && ! -e "$DEPT13C/.claude/skills/codex-write" \
      && -L "$DEPT13C/.claude/skills/draft-writer" ]]; then
  echo "  PASS: T13h empty skills:{} → fleet-shared added, curated kept, nothing else"; PASS=$((PASS+1))
else
  echo "  FAIL: T13h empty-declaration ensure behaved wrong"; FAIL=$((FAIL+1))
fi

# =============================================================================
echo
echo "RESULTS: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
