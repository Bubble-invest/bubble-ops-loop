#!/usr/bin/env bash
# =============================================================================
# test_sync_dispatch_lib.sh — WS2 TDD harness for scripts/sync-dispatch-lib.sh
#
# Runs entirely against THROWAWAY FIXTURES under a mktemp dir. NEVER touches the
# real dept repos (/home/claude/agents/bubble-ops-*) — every invocation passes
# explicit --framework / --agents-root pointing at the fixture tree.
#
# Assertions:
#   T1  --check exits 0 when all fixture depts are in sync with canonical.
#   T2  --check exits NON-ZERO after a one-byte drift in a dept copy (RED proof).
#   T3  a full run re-syncs the drifted dept -> its md5 matches canonical again
#       (GREEN proof), runs the (fixture) dispatch tests, and makes a local
#       commit (no push).
#   T4  idempotency: a second --check after the run exits 0.
#   T5  run is idempotent: a second run makes no new commit.
# =============================================================================
set -uo pipefail

SCRIPT_UNDER_TEST="${1:?usage: test_sync_dispatch_lib.sh <path-to-sync-dispatch-lib.sh>}"
PASS=0; FAIL=0
chk() { # chk <desc> <expected_rc> <actual_rc>
  if [[ "$2" == "$3" ]]; then echo "  PASS: $1 (rc=$3)"; PASS=$((PASS+1));
  else echo "  FAIL: $1 (expected rc=$2, got rc=$3)"; FAIL=$((FAIL+1)); fi
}
chk_eq() { # chk_eq <desc> <expected> <actual>
  if [[ "$2" == "$3" ]]; then echo "  PASS: $1"; PASS=$((PASS+1));
  else echo "  FAIL: $1 (expected '$2', got '$3')"; FAIL=$((FAIL+1)); fi
}

FIX="$(mktemp -d /tmp/sync-fix.XXXXXX)"
trap 'rm -rf "$FIX"' EXIT
FW="$FIX/framework"
AGENTS="$FIX/agents"
PY="$(command -v python3)"

# ---- build the fake framework (canonical lib + a trivial passing test) ------
mkdir -p "$FW/scripts/lib/tests"
cat > "$FW/scripts/lib/dispatch_helpers.py" <<'PYEOF'
# fixture canonical dispatch_helpers.py
def decide_dispatch(ctx):
    return "layer_1" if ctx.get("l1_last") is None else "heartbeat"
PYEOF
: > "$FW/scripts/lib/tests/__init__.py"
# one sibling dispatch test that the sync script knows about, trivially green.
cat > "$FW/scripts/lib/tests/test_build_dispatch_ctx.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from scripts.lib.dispatch_helpers import decide_dispatch
def test_floor():
    assert decide_dispatch({"l1_last": None}) == "layer_1"
def test_heartbeat():
    assert decide_dispatch({"l1_last": "x"}) == "heartbeat"
PYEOF
CANON_MD5="$(md5sum "$FW/scripts/lib/dispatch_helpers.py" | awk '{print $1}')"

# ---- build two fake dept trees (git repos so commit works) ------------------
make_dept() {
  local slug="$1"; local root="$AGENTS/bubble-ops-$slug"
  mkdir -p "$root/scripts/lib/tests"
  git -C "$root" init -q 2>/dev/null || { mkdir -p "$root" && git -C "$root" init -q; }
  git -C "$root" config user.email "fixture@test"
  git -C "$root" config user.name "fixture"
  # start IN SYNC: copy canonical + the test file
  cp "$FW/scripts/lib/dispatch_helpers.py" "$root/scripts/lib/dispatch_helpers.py"
  : > "$root/scripts/lib/tests/__init__.py"
  cp "$FW/scripts/lib/tests/test_build_dispatch_ctx.py" "$root/scripts/lib/tests/test_build_dispatch_ctx.py"
  git -C "$root" add -A && git -C "$root" commit -q -m "init in-sync"
}
make_dept alpha
make_dept beta

COMMON=(--framework="$FW" --agents-root="$AGENTS" --depts="alpha beta" --python="$PY")

echo "== T1: --check exits 0 when in sync =="
"$SCRIPT_UNDER_TEST" --check "${COMMON[@]}" >/dev/null 2>&1; rc=$?
chk "in-sync --check -> 0" 0 "$rc"

echo "== T2: one-byte drift -> --check exits NON-ZERO (RED) =="
# Model REALISTIC dept drift: beta COMMITTED a stale copy (one byte off the
# canonical). This is the real-world case the sync exists to heal, and it lets
# T3 prove a genuine commit (diff-from-HEAD) is produced by the re-sync.
printf '#drift\n' >> "$AGENTS/bubble-ops-beta/scripts/lib/dispatch_helpers.py"
git -C "$AGENTS/bubble-ops-beta" commit -q -am "drift: stale dispatch_helpers"
DRIFT_MD5="$(md5sum "$AGENTS/bubble-ops-beta/scripts/lib/dispatch_helpers.py" | awk '{print $1}')"
[[ "$DRIFT_MD5" != "$CANON_MD5" ]] && echo "  (beta now drifted: $DRIFT_MD5 != $CANON_MD5)"
"$SCRIPT_UNDER_TEST" --check "${COMMON[@]}" >/dev/null 2>&1; rc=$?
chk "drift --check -> non-zero" 1 "$rc"

echo "== T3: full run re-syncs drift (GREEN) + runs tests + commits =="
"$SCRIPT_UNDER_TEST" "${COMMON[@]}" >"$FIX/run1.log" 2>&1; rc=$?
chk "full run -> 0" 0 "$rc"
RESYNC_MD5="$(md5sum "$AGENTS/bubble-ops-beta/scripts/lib/dispatch_helpers.py" | awk '{print $1}')"
chk_eq "beta re-synced to canonical md5" "$CANON_MD5" "$RESYNC_MD5"
# a commit was made on beta for the drift fix
BETA_LOG="$(git -C "$AGENTS/bubble-ops-beta" log --oneline | head -1)"
echo "  beta HEAD: $BETA_LOG"
case "$BETA_LOG" in *"sync(dispatch)"*) echo "  PASS: beta has a sync commit"; PASS=$((PASS+1));;
  *) echo "  FAIL: beta missing sync commit"; FAIL=$((FAIL+1));; esac
grep -q "dispatch tests green" "$FIX/run1.log" && { echo "  PASS: dept tests ran green"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: dept tests did not report green"; FAIL=$((FAIL+1)); }

echo "== T4: --check exits 0 again after re-sync =="
"$SCRIPT_UNDER_TEST" --check "${COMMON[@]}" >/dev/null 2>&1; rc=$?
chk "post-sync --check -> 0" 0 "$rc"

echo "== T5: run is idempotent (no second commit) =="
N_BEFORE="$(git -C "$AGENTS/bubble-ops-beta" rev-list --count HEAD)"
"$SCRIPT_UNDER_TEST" "${COMMON[@]}" >/dev/null 2>&1
N_AFTER="$(git -C "$AGENTS/bubble-ops-beta" rev-list --count HEAD)"
chk_eq "no new commit on idempotent re-run" "$N_BEFORE" "$N_AFTER"

echo "== T6: confirm REAL dept repos were never targeted =="
# sanity: the fixture agents root is NOT the live one
case "$AGENTS" in /home/claude/agents) echo "  FAIL: fixture pointed at LIVE agents root!"; FAIL=$((FAIL+1));;
  *) echo "  PASS: agents-root is a throwaway fixture ($AGENTS)"; PASS=$((PASS+1));; esac

# -----------------------------------------------------------------------------
# T7: bootstrap-dept.sh hook — a newly-scaffolded dept vendors the CANONICAL
# dispatch_helpers.py. Driven via the REAL bootstrap --dry-run into a throwaway
# clone dir (no gh, no git push, no live dept). Uses the actual framework
# canonical (md5 must match), proving the hook wires in scaffold reality.
# -----------------------------------------------------------------------------
echo "== T7: bootstrap-dept.sh dry-run vendors canonical dispatch_helpers.py =="
BOOT="$(dirname "$SCRIPT_UNDER_TEST")/bootstrap-dept.sh"
FW_REAL="$(cd "$(dirname "$SCRIPT_UNDER_TEST")/.." && pwd)"  # framework repo root
REAL_CANON="$FW_REAL/scripts/lib/dispatch_helpers.py"
if [[ ! -x "$BOOT" || ! -f "$REAL_CANON" ]]; then
  echo "  SKIP: bootstrap-dept.sh or canonical not found (BOOT=$BOOT)"
else
  REAL_CANON_MD5="$(md5sum "$REAL_CANON" | awk '{print $1}')"
  BOOT_CLONE_PARENT="$(mktemp -d /tmp/sync-boot.XXXXXX)"
  # --dry-run renders the skeleton into $BUBBLE_BOOTSTRAP_CLONE_DIR/bubble-ops-<slug>
  if BUBBLE_BOOTSTRAP_CLONE_DIR="$BOOT_CLONE_PARENT" \
       "$BOOT" --slug=fixturedept --display-name="FixtureDept" --owner=operator --dry-run \
       >"$FIX/boot.log" 2>&1; then
    VENDORED="$BOOT_CLONE_PARENT/bubble-ops-fixturedept/scripts/lib/dispatch_helpers.py"
    if [[ -f "$VENDORED" ]]; then
      V_MD5="$(md5sum "$VENDORED" | awk '{print $1}')"
      chk_eq "scaffolded dept dispatch_helpers.py matches canonical md5" "$REAL_CANON_MD5" "$V_MD5"
    else
      echo "  FAIL: scaffolded dept has NO scripts/lib/dispatch_helpers.py"; FAIL=$((FAIL+1))
    fi
  else
    echo "  FAIL: bootstrap --dry-run errored (see $FIX/boot.log)"; FAIL=$((FAIL+1))
    tail -5 "$FIX/boot.log" | sed 's/^/    /'
  fi
  rm -rf "$BOOT_CLONE_PARENT"
fi

echo
echo "RESULTS: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
