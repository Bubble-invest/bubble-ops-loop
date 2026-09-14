#!/usr/bin/env bash
# =============================================================================
# test_codex_write_vendoring.sh — proves board #1312's two evaluation criteria:
#
#   (A) codex_write.sh now rides the vendoring path: a ONE-LINE canonical change
#       in the framework propagates to every dept via vendor-dept-libs.sh /
#       revendor-all-depts.sh — no per-dept PR — and lands EXECUTABLE.
#   (B) a deliberately-divergent dept copy is caught LOUDLY by
#       check-vendor-drift.sh (exit 1 + a FORK line), instead of rotting silently.
#
# Runs entirely against THROWAWAY FIXTURES under mktemp. Never touches a real dept
# repo, /home/claude, or /srv. Mirrors the fixture style of test_vendor_dept_libs.sh.
# =============================================================================
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SELF_DIR/../scripts" && pwd)"
VENDOR="${1:-$SCRIPTS_DIR/vendor-dept-libs.sh}"
REVENDOR="${2:-$SCRIPTS_DIR/revendor-all-depts.sh}"
DRIFT="${3:-$SCRIPTS_DIR/check-vendor-drift.sh}"

PASS=0; FAIL=0
chk()    { if [[ "$2" == "$3" ]]; then echo "  PASS: $1 (rc=$3)"; PASS=$((PASS+1)); else echo "  FAIL: $1 (expected rc=$2, got rc=$3)"; FAIL=$((FAIL+1)); fi; }
chk_eq() { if [[ "$2" == "$3" ]]; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (expected '$2', got '$3')"; FAIL=$((FAIL+1)); fi; }
chk_has(){ if echo "$3" | grep -q "$2"; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1 (pattern '$2' not in output)"; FAIL=$((FAIL+1)); fi; }
chk_true(){ if eval "$2"; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1"; FAIL=$((FAIL+1)); fi; }

FIX="$(mktemp -d /tmp/codexvendor-fix.XXXXXX)"
trap 'rm -rf "$FIX"' EXIT

CANON_V1='#!/usr/bin/env bash
# codex_write.sh — canonical v1
echo v1'

make_framework() {  # <dir>
  local dir="$1"
  mkdir -p "$dir/scripts/lib" "$dir/tools"
  echo "# canonical dispatch_helpers" > "$dir/scripts/lib/dispatch_helpers.py"
  echo "# canonical notify"           > "$dir/scripts/lib/notify.py"
  echo "# canonical loop_notify"      > "$dir/scripts/lib/loop_notify.py"
  echo "# canonical notion_logbook"   > "$dir/scripts/lib/notion_logbook.py"
  echo "# canonical budget"           > "$dir/scripts/lib/budget.py"
  echo "# canonical notify_layer"     > "$dir/tools/notify_layer.py"
  printf '%s\n' "$CANON_V1"           > "$dir/scripts/lib/codex_write.sh"
  chmod +x "$dir/scripts/lib/codex_write.sh"   # canonical ships executable
}

make_dept() {  # <dir>  — a dept that already has scripts/lib (so codex_write.sh vendors)
  local dir="$1"
  mkdir -p "$dir/scripts/lib" "$dir/tools"
  git -C "$dir" init -q 2>/dev/null
  git -C "$dir" config user.email fixture@test
  git -C "$dir" config user.name fixture
  echo "placeholder" > "$dir/scripts/lib/.keep"
  git -C "$dir" add -A && git -C "$dir" commit -q -m init
}

# =============================================================================
# (A) PROPAGATION — one canonical file reaches every dept through the sweep
# =============================================================================
echo "== A: framework codex_write.sh propagates to all depts via revendor =="
FW="$FIX/fw/bubble-ops-loop"; make_framework "$FW"
AR="$FIX/agents"; mkdir -p "$AR"
DEPTS=(tony maya ben content claudette)     # the #1312 dept set (host:vps here)
for d in "${DEPTS[@]}"; do make_dept "$AR/bubble-ops-$d"; done

# Nobody has codex_write.sh yet.
missing_before=0
for d in "${DEPTS[@]}"; do [[ -e "$AR/bubble-ops-$d/scripts/lib/codex_write.sh" ]] && missing_before=$((missing_before+1)); done
chk_eq "A0 no dept has codex_write.sh before the sweep" "0" "$missing_before"

out_a="$(BUBBLE_FRAMEWORK_ROOT="$FW" "$REVENDOR" --framework "$FW" --agents-root "$AR" 2>&1)"
chk "A1 revendor sweep exits 0" 0 "$?"

all_ok=1; exec_ok=1
for d in "${DEPTS[@]}"; do
  f="$AR/bubble-ops-$d/scripts/lib/codex_write.sh"
  cmp -s "$FW/scripts/lib/codex_write.sh" "$f" 2>/dev/null || all_ok=0
  [[ -x "$f" ]] || exec_ok=0
done
chk_eq "A2 codex_write.sh vendored identically into ALL ${#DEPTS[@]} depts" "1" "$all_ok"
chk_eq "A3 vendored codex_write.sh is executable in every dept" "1" "$exec_ok"

# The one-line canonical change → propagates on the next sweep, no per-dept PR.
printf '%s\n' "$CANON_V1" | sed 's/echo v1/echo v2-one-line-change/' > "$FW/scripts/lib/codex_write.sh"
chmod +x "$FW/scripts/lib/codex_write.sh"
BUBBLE_FRAMEWORK_ROOT="$FW" "$REVENDOR" --framework "$FW" --agents-root "$AR" >/dev/null 2>&1
prop_ok=1
for d in "${DEPTS[@]}"; do
  grep -q "v2-one-line-change" "$AR/bubble-ops-$d/scripts/lib/codex_write.sh" 2>/dev/null || prop_ok=0
done
chk_eq "A4 a one-line canonical edit reached every dept (mechanical, no PR)" "1" "$prop_ok"

# =============================================================================
# (B) DRIFT — a divergent copy is caught loudly
# =============================================================================
echo "== B: check-vendor-drift catches a divergent copy =="
# Baseline: everything just vendored → clean.
out_b0="$("$DRIFT" --framework "$FW" --agents-root "$AR" 2>&1)"; rc_b0=$?
chk "B0 clean fleet → drift check exits 0" 0 "$rc_b0"
chk_has "B0 reports clean" "RESULT: clean" "$out_b0"

# Deliberately fork ONE dept's codex_write.sh (a hand-edit that != canonical and
# != its last-vendored baseline) — the silent-drift class #1312 exists to surface.
echo '#!/usr/bin/env bash
echo "SNEAKY DIVERGENT COPY"' > "$AR/bubble-ops-maya/scripts/lib/codex_write.sh"
out_b1="$("$DRIFT" --framework "$FW" --agents-root "$AR" 2>&1)"; rc_b1=$?
chk "B1 divergent copy → drift check exits 1 (loud)" 1 "$rc_b1"
chk_has "B1 names the drifted dept+file as FORK" "FORK.*maya.*codex_write.sh" "$out_b1"
chk_has "B1 result line announces DRIFT" "RESULT: DRIFT" "$out_b1"

# A dept whose copy still matches must NOT be falsely flagged.
chk_true "B2 an in-sync dept is not reported as FORK" \
  "! echo \"\$out_b1\" | grep -q 'FORK.*ben.*codex_write.sh'"

# =============================================================================
# (C) STALE vs FORK classification — canonical advances, dept still at baseline
# =============================================================================
echo "== C: stale (pending revendor) is distinguished from fork =="
FW2="$FIX/fw2/bubble-ops-loop"; make_framework "$FW2"
AR2="$FIX/agents2"; mkdir -p "$AR2"
make_dept "$AR2/bubble-ops-tony"
BUBBLE_FRAMEWORK_ROOT="$FW2" "$VENDOR" "$AR2/bubble-ops-tony" >/dev/null 2>&1  # vendor + record baseline
# Advance canonical WITHOUT re-vendoring: dept copy == baseline, != canonical.
printf '%s\n' "$CANON_V1" | sed 's/echo v1/echo v99/' > "$FW2/scripts/lib/codex_write.sh"
chmod +x "$FW2/scripts/lib/codex_write.sh"
out_c="$("$DRIFT" --framework "$FW2" --agents-root "$AR2" 2>&1)"; rc_c=$?
chk "C1 stale-only fleet → exit 0 (sweep will heal it, not a hard drift)" 0 "$rc_c"
chk_has "C1 codex_write.sh classified STALE (revendor pending)" "STALE.*tony.*codex_write.sh" "$out_c"

# =============================================================================
# (D) ZERO-DEPTS GUARD — a wrong --agents-root must NOT report "clean"
# =============================================================================
echo "== D: checked-0-depts is a setup error, never a false 'clean' =="
EMPTY="$FIX/empty-agents"; mkdir -p "$EMPTY"
out_d="$("$DRIFT" --framework "$FW" --agents-root "$EMPTY" 2>&1)"; rc_d=$?
chk "D1 zero depts checked → exit 2 (setup error, not a clean pass)" 2 "$rc_d"
chk_has "D1 refuses to report clean" "NOT reporting clean" "$out_d"
chk_true "D1 does not print a clean result" \
  "! echo \"\$out_d\" | grep -q 'RESULT: clean'"

# =============================================================================
echo
echo "RESULTS: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
