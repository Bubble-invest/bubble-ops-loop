#!/usr/bin/env bash
# test_1463_hide_marker_restore_acl_preserve.sh — board #1463 follow-up: prod
# incident, cockpit 500 at 14:15Z.
#
# Root cause: scripts/sync-local-dept-clones.sh's preserve_hide_markers() /
# restore_hide_markers() used `cp -a "$src/." "$dest/"` (the trailing "/."
# merge-into-existing-dir idiom). With -a that applies the SOURCE directory's
# OWN mode to the DESTINATION directory as a side effect — not just the
# copied entries' attributes. restore_hide_markers's stash is `mktemp -d`
# (0700), so every ~15min sync chmod'd /home/claude/agents/*/inbox/decisions
# to 0700 — zeroing the POSIX ACL mask the board #1463 follow-up's
# `setfacl -R -m g:bubble-console:rwX` grant depends on, EACCES-ing
# bubble-console out of inbox/decisions/.processed.
#
# Fix: copy each top-level entry of the source BY NAME (dotfiles included —
# `find` lists them by default) instead of the "/." merge idiom, on both the
# preserve and restore sides — so neither $stash's nor $dest's OWN directory
# attributes (mode, and on Linux, the ACL) are ever touched by these copies.
#
# This test extracts the REAL preserve_hide_markers / restore_hide_markers
# function bodies from the production script (not a hand-copied
# reimplementation) and exercises them directly against throwaway fixtures —
# never touches a real dept mirror.
#
# Run: bash tests/test_1463_hide_marker_restore_acl_preserve.sh
# Returns 0 on pass, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/sync-local-dept-clones.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Extract the two real function bodies (not reimplemented) and source them.
FUNCS="$WORK/funcs.sh"
{
  sed -n '/^preserve_hide_markers() {/,/^}/p' "$SCRIPT"
  echo
  sed -n '/^restore_hide_markers() {/,/^}/p' "$SCRIPT"
} > "$FUNCS"
grep -q "^preserve_hide_markers() {" "$FUNCS" || fail "could not extract preserve_hide_markers() from $SCRIPT — did its signature change?"
grep -q "^restore_hide_markers() {" "$FUNCS" || fail "could not extract restore_hide_markers() from $SCRIPT — did its signature change?"
# The whole point of this regression test: neither function may use the
# "/."-merge-into-existing-dir idiom that leaks a source dir's mode onto an
# existing destination dir.
grep -qE '"\$src/\."|"\$stash/\."' "$FUNCS" && fail "extracted functions still use the \"DIR/.\" merge idiom that caused the #1463 14:15Z incident"
# shellcheck disable=SC1090
source "$FUNCS"

octal_mode() {
  stat -f "%Lp" "$1" 2>/dev/null || stat -c "%a" "$1" 2>/dev/null
}

HAVE_ACL=0
command -v setfacl >/dev/null 2>&1 && command -v getfacl >/dev/null 2>&1 && HAVE_ACL=1

# === Case 1 (the actual incident): restore_hide_markers must not touch
#     dest's own mode/ACL, even though the stash it copies FROM is a
#     mktemp -d 0700 dir. ===
DIR="$WORK/dept1"
DEST="$DIR/inbox/decisions"
mkdir -p "$DEST"
chmod 0770 "$DEST"
if [[ "$HAVE_ACL" -eq 1 ]]; then
  setfacl -m g:bubble-console:rwX "$DEST" || fail "setfacl setup failed"
  BEFORE_ACL="$(getfacl -p "$DEST" 2>/dev/null)"
fi
BEFORE_MODE="$(octal_mode "$DEST")"
[[ "$BEFORE_MODE" == "770" ]] || fail "setup: expected $DEST at mode 770, got $BEFORE_MODE"

STASH1="$WORK/stash1"
mkdir -p "$STASH1"
chmod 0700 "$STASH1"
echo "gate-approval" > "$STASH1/gate1.yaml"
mkdir -p "$STASH1/.processed"
echo "old" > "$STASH1/.processed/marker"

restore_hide_markers "$DIR" "$STASH1"

AFTER_MODE="$(octal_mode "$DEST")"
[[ "$AFTER_MODE" == "770" ]] \
  || fail "restore_hide_markers changed $DEST's mode: was $BEFORE_MODE, now $AFTER_MODE (this IS the #1463 14:15Z incident — a mktemp -d 0700 stash's mode leaking onto the dept's inbox/decisions dir)"
pass "case 1: restore_hide_markers leaves \$dest's own mode at 0770 (was: $BEFORE_MODE, after: $AFTER_MODE)"

if [[ "$HAVE_ACL" -eq 1 ]]; then
  AFTER_ACL="$(getfacl -p "$DEST" 2>/dev/null)"
  [[ "$AFTER_ACL" == "$BEFORE_ACL" ]] \
    || fail "restore_hide_markers changed \$dest's ACL:\n--- before ---\n$BEFORE_ACL\n--- after ---\n$AFTER_ACL"
  pass "case 1: restore_hide_markers leaves \$dest's POSIX ACL (incl. the bubble-console grant) unchanged"
else
  echo "SKIP: setfacl/getfacl not available on this host — mode-only proof above stands in for it"
fi

# Functional correctness: the stashed dotfile-dir and regular file both
# landed, and the stash itself was discarded.
[[ -f "$DEST/gate1.yaml" ]] || fail "case 1: gate1.yaml did not make it into \$dest"
[[ -f "$DEST/.processed/marker" ]] || fail "case 1: dotfile dir .processed/marker did not make it into \$dest"
[[ -d "$STASH1" ]] && fail "case 1: stash dir was not removed after restore"
pass "case 1: stashed file + dotfile-dir land in \$dest, and the stash is discarded"

# NO-CLOBBER still holds: a path already present in \$dest (e.g. origin just
# converged it to a tracked file) must NOT be overwritten by the stash.
DIR2="$WORK/dept2"
DEST2="$DIR2/inbox/decisions"
mkdir -p "$DEST2"
chmod 0770 "$DEST2"
echo "FROM-ORIGIN-AUTHORITATIVE" > "$DEST2/gate1.yaml"
STASH2="$WORK/stash2"
mkdir -p "$STASH2"
echo "stale-local-only-copy" > "$STASH2/gate1.yaml"
restore_hide_markers "$DIR2" "$STASH2"
[[ "$(cat "$DEST2/gate1.yaml")" == "FROM-ORIGIN-AUTHORITATIVE" ]] \
  || fail "case 1b: restore_hide_markers clobbered an already-present (origin-authoritative) path — NO-CLOBBER regression"
pass "case 1b: NO-CLOBBER still holds — an already-present path in \$dest is not overwritten"

# === Case 2 (symmetry): preserve_hide_markers must not touch the STASH's
#     own mode either (it starts mktemp -d 0700; $src here is 0770). ===
DIR3="$WORK/dept3"
SRC3="$DIR3/inbox/decisions"
mkdir -p "$SRC3"
chmod 0770 "$SRC3"
echo "hide-marker" > "$SRC3/gate2.yaml"
STASH3="$WORK/stash3"
mkdir -p "$STASH3"
chmod 0700 "$STASH3"
BEFORE_STASH_MODE="$(octal_mode "$STASH3")"

COUNT="$(preserve_hide_markers "$DIR3" "$STASH3")"

AFTER_STASH_MODE="$(octal_mode "$STASH3")"
[[ "$COUNT" == "1" ]] || fail "case 2: expected preserve_hide_markers to report 1 file, got '$COUNT'"
[[ -f "$STASH3/gate2.yaml" ]] || fail "case 2: gate2.yaml did not make it into the stash"
[[ "$AFTER_STASH_MODE" == "700" ]] \
  || fail "case 2: preserve_hide_markers changed the stash's own mode: was $BEFORE_STASH_MODE, now $AFTER_STASH_MODE (symmetric leak — \$src's mode onto \$stash)"
pass "case 2: preserve_hide_markers leaves the stash's own mode at 0700 (symmetric fix)"

echo "ALL PASS"
exit 0
