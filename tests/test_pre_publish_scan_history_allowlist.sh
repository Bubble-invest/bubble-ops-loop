#!/usr/bin/env bash
# =============================================================================
# test_pre_publish_scan_history_allowlist.sh — board card #490 (follow-up of
# #372): deploy/bin/pre-publish-scan.sh's git-history-scan block never applied
# the `.pre-publish-allow` filter that the working-tree-scan block applies —
# a documented false positive muted in the working tree STILL flagged when the
# same content was scanned in git history.
#
# Builds a throwaway git fixture repo with:
#   1. A committed `.pre-publish-allow` entry for a placeholder secret-prefix
#      pattern (ghs_DRYRUN_) and a file that only ever contains that pattern.
#   2. A separate commit introducing a genuine, NON-allowlisted secret-prefix
#      leak (AKIA...), later removed from the tree but still present in history.
#
# Asserts:
#   A. Working tree scan: allowlisted pattern muted (no secret-prefix finding).
#   B. History scan: allowlisted pattern is ALSO muted (the bug — this is the
#      regression check for #490).
#   C. History scan: the genuine non-allowlisted leak is STILL caught, both
#      while present in the tree and after being removed from the tree but
#      remaining in history — proves the fix is not more permissive than the
#      working-tree scan, only equally permissive.
#
# Run:  bash tests/test_pre_publish_scan_history_allowlist.sh
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
SCANNER="$REPO_ROOT/deploy/bin/pre-publish-scan.sh"

[[ -f "$SCANNER" ]] || { echo "FATAL: scanner not found: $SCANNER"; exit 2; }

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== test_pre_publish_scan_history_allowlist.sh =="
echo "   scanner: $SCANNER"
echo "   fixture: $WORK"
echo ""

FIXTURE="$WORK/fixture-repo"
mkdir -p "$FIXTURE"
cd "$FIXTURE"
git init -q
git config user.email "test@example.com"
git config user.name "test"

# ── commit 1: allowlisted false positive, present in tree + history ────────
cat > .pre-publish-allow <<'EOF'
# why: synthetic placeholder token used by a DRYRUN diagnostic branch — not a
#      real minted credential. Test fixture for #490.
ghs_DRYRUN_
EOF
cat > placeholder.sh <<'EOF'
echo "ghs_DRYRUN_$(date +%s)"
EOF
git add -A && git commit -qm "init: allowlisted false positive" >/dev/null

# ── A. working-tree scan: allowlisted pattern muted ─────────────────────────
echo "A. working-tree scan mutes the allowlisted secret-prefix pattern"
TREE_OUT="$(bash "$SCANNER" . 2>&1)"
if echo "$TREE_OUT" | grep -q "secret-prefix"; then
  bad "secret-prefix finding present in working-tree scan (should be muted by .pre-publish-allow)"
else
  ok "secret-prefix finding absent from working-tree scan"
fi

# ── B. history scan: allowlisted pattern is ALSO muted (the #490 fix) ───────
echo "B. history scan mutes the SAME allowlisted secret-prefix pattern (#490 regression check)"
HIST_OUT="$(bash "$SCANNER" . --history 2>&1)"
if echo "$HIST_OUT" | grep -A2 "IN HISTORY" | grep -q "placeholder.sh\|\.pre-publish-allow"; then
  bad "allowlisted pattern still flagged IN HISTORY — #490 bug not fixed"
else
  ok "allowlisted pattern NOT flagged in history scan"
fi

# ── C. genuine non-allowlisted leak is still caught (not more permissive) ──
echo "C. a genuine, non-allowlisted secret-prefix leak is still caught in history"
echo 'REAL_LEAK="AKIAABCDEFGHIJKLMNOP"' > leaky.sh
git add leaky.sh && git commit -qm "accidental leak" >/dev/null

LEAK_TREE_OUT="$(bash "$SCANNER" . 2>&1)"
if echo "$LEAK_TREE_OUT" | grep -q "secret-prefix"; then
  ok "genuine leak caught in working-tree scan (sanity check)"
else
  bad "genuine leak NOT caught in working-tree scan — test fixture or scanner broken"
fi

LEAK_HIST_OUT="$(bash "$SCANNER" . --history 2>&1)"
if echo "$LEAK_HIST_OUT" | grep -A3 "IN HISTORY" | grep -q "leaky.sh"; then
  ok "genuine leak caught in history scan while still in tree"
else
  bad "genuine leak NOT caught in history scan while still in tree"
fi

# Remove from the tree (still present in an earlier commit's history) — this
# is the exact case the --history flag exists for: a value purged from HEAD
# but still public to a cloner via history.
git rm -q leaky.sh && git commit -qm "remove leak from tree" >/dev/null

PURGED_TREE_OUT="$(bash "$SCANNER" . 2>&1)"
if echo "$PURGED_TREE_OUT" | grep -q "secret-prefix"; then
  bad "working-tree scan still flags leaky.sh after git rm (expected clean — it's gone from tree)"
else
  ok "working-tree scan is clean after git rm (as expected)"
fi

PURGED_HIST_OUT="$(bash "$SCANNER" . --history 2>&1)"
if echo "$PURGED_HIST_OUT" | grep -A3 "IN HISTORY" | grep -q "leaky.sh"; then
  ok "genuine leak STILL caught in history scan after being purged from the tree"
else
  bad "genuine leak NOT caught in history after git rm — history scan is broken/too permissive"
fi

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
