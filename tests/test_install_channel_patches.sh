#!/usr/bin/env bash
# =============================================================================
# test_install_channel_patches.sh — bash harness for scripts/install-channel-patches.sh
# (board #956 — the durable fold of boot_rearm + bubble-inject re-apply).
#
# Covers, in order:
#
#   A. FRESH / un-patched plugin dir (simulating a version-bump re-extract that
#      wiped both patches) -> running the installer re-applies BOTH: boot_rearm
#      markers present, bubble-inject markers present, `bun build` passes, exit 0.
#
#   B. Idempotency — re-running against the now-patched dir is a clean no-op:
#      no new backups, both patches still present exactly once, exit 0.
#
#   C. Partial state — a dir with boot_rearm ALREADY applied but bubble-inject
#      missing -> the installer leaves boot_rearm untouched and applies ONLY
#      bubble-inject (proves the two patches are handled independently, per
#      the mechanism.md doctrine: "a plugin can have neither, one, or both").
#
#   D. --dry-run footprint — against a fresh dir, --dry-run touches NOTHING:
#      no boot_rearm.ts copy, no server.ts edit, no backups, exit 0.
#
#   E. --strict exit code — against a dir where the bubble-inject anchor is
#      missing (drift), --strict surfaces a nonzero exit; default (non-strict)
#      mode still exits 0 (the fail-open pre-launch-hook contract).
#
#   F. Host-agnostic glob — the SAME script, pointed via
#      CHANNEL_PATCHES_PLUGIN_GLOB at a Mac-shaped path (no /home/claude
#      anywhere in it), still finds and patches the plugin. This is the actual
#      #956 regression: bubble-inject's old hook (apply-inject-patch.sh) was
#      VPS-only (/home/claude hardcoded), so a Mac plugin bump had nothing
#      re-applying it.
#
# Hermetic: builds throw-away fake plugin dirs under a tmpdir, each seeded from
# a genuinely PRISTINE server.ts (neither patch) + the real package.json + a
# SYMLINK to the real plugin node_modules, so `bun build` resolves grammy /
# @modelcontextprotocol without a network install. The live plugin cache is
# NEVER modified.
#
# Requirements on the box: bun, and a PRISTINE (unpatched) telegram plugin
# server.ts to seed fixtures from. On a box where the live plugin is already
# patched (the common case once #956 lands), point these at a saved
# pre-patch backup instead of the live server.ts:
#   CHANNEL_PATCHES_TEST_PLUGIN_SRC       dir supplying package.json + node_modules
#                                          (default: newest live telegram plugin dir)
#   CHANNEL_PATCHES_TEST_PRISTINE_SERVER  the pristine server.ts to seed fixtures
#                                          with (default: $CHANNEL_PATCHES_TEST_PLUGIN_SRC/server.ts)
#   CHANNEL_PATCHES_TEST_BUN              bun binary (default: $HOME/.bun/bin/bun,
#                                          falls back to `command -v bun`)
#
# Run:  bash tests/test_install_channel_patches.sh
#       bash tests/test_install_channel_patches.sh -v   # verbose (show installer out)
# =============================================================================
set -uo pipefail

VERBOSE=0
[[ "${1:-}" == "-v" ]] && VERBOSE=1

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
INSTALLER="$REPO_ROOT/scripts/install-channel-patches.sh"
REPO_BOOT_REARM="$REPO_ROOT/deploy/telegram-plugin/boot_rearm.ts"
REPO_INJECT_BLOCK="$REPO_ROOT/deploy/telegram-plugin/bubble-inject.block.ts"
BUN_BIN="${CHANNEL_PATCHES_TEST_BUN:-$HOME/.bun/bin/bun}"
[[ -x "$BUN_BIN" ]] || BUN_BIN="$(command -v bun 2>/dev/null || true)"

[[ -f "$INSTALLER" ]]         || { echo "FATAL: installer not found: $INSTALLER"; exit 2; }
[[ -f "$REPO_BOOT_REARM" ]]   || { echo "FATAL: repo boot_rearm.ts not found: $REPO_BOOT_REARM"; exit 2; }
[[ -f "$REPO_INJECT_BLOCK" ]] || { echo "FATAL: repo bubble-inject.block.ts not found: $REPO_INJECT_BLOCK"; exit 2; }
[[ -n "$BUN_BIN" && -x "$BUN_BIN" ]] || { echo "FATAL: bun not found (set CHANNEL_PATCHES_TEST_BUN)"; exit 2; }

# Locate a plugin dir to source package.json + node_modules from (deps only).
PLUGIN_SRC="${CHANNEL_PATCHES_TEST_PLUGIN_SRC:-}"
if [[ -z "$PLUGIN_SRC" ]]; then
  for d in $(ls -d "$HOME"/.claude/plugins/cache/claude-plugins-official/telegram/*/ 2>/dev/null | sort -V); do
    [[ -d "$d" ]] && PLUGIN_SRC="$d"
  done
fi
PLUGIN_SRC="${PLUGIN_SRC%/}"
[[ -d "$PLUGIN_SRC/node_modules" ]] || { echo "FATAL: no node_modules at $PLUGIN_SRC (needed for bun build)"; exit 2; }
[[ -f "$PLUGIN_SRC/package.json" ]] || { echo "FATAL: no package.json at $PLUGIN_SRC"; exit 2; }

# The PRISTINE server.ts to seed fixtures with (must carry NEITHER marker —
# this is what a genuinely fresh version-bump extraction looks like).
PRISTINE_SERVER="${CHANNEL_PATCHES_TEST_PRISTINE_SERVER:-$PLUGIN_SRC/server.ts}"
[[ -f "$PRISTINE_SERVER" ]] || { echo "FATAL: no pristine server.ts at $PRISTINE_SERVER"; exit 2; }
if grep -q "bootRearmNotification\|bubble-inject" "$PRISTINE_SERVER"; then
  echo "FATAL: $PRISTINE_SERVER is ALREADY patched (has bootRearmNotification and/or"
  echo "       bubble-inject) — cannot build a pristine fixture from it. Point"
  echo "       CHANNEL_PATCHES_TEST_PRISTINE_SERVER at a genuinely unpatched"
  echo "       server.ts (e.g. a pre-patch .bak-* file from the live plugin dir)."
  exit 2
fi
grep -qF 'await mcp.connect(new StdioServerTransport())' "$PRISTINE_SERVER" \
  || { echo "FATAL: pristine server.ts lacks the bubble-inject anchor line — wrong fixture source"; exit 2; }

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# make_fixture <root> [pristine|boot_rearm_only] -> echoes the plugin dir path.
# "boot_rearm_only" pre-wires boot_rearm (via install-boot-rearm.sh itself,
# so the fixture reflects a REAL post-patch server.ts, not a hand-rolled one).
make_fixture() {
  local root="$1" mode="${2:-pristine}"
  local tgt="$root/claude-plugins-official/telegram/9.9.9"
  mkdir -p "$tgt"
  cp "$PRISTINE_SERVER" "$tgt/server.ts"
  cp "$PLUGIN_SRC/package.json" "$tgt/package.json"
  ln -s "$PLUGIN_SRC/node_modules" "$tgt/node_modules"
  if [[ "$mode" == "boot_rearm_only" ]]; then
    BOOT_REARM_PLUGIN_GLOB="$root/claude-plugins-official/telegram/*/" \
      BOOT_REARM_BUN="$BUN_BIN" \
      bash "$REPO_ROOT/scripts/install-boot-rearm.sh" >/dev/null 2>&1
  fi
  echo "$tgt"
}

run_installer() {
  # usage: run_installer <glob> [extra args...]; sets RC + OUT
  local glob="$1"; shift || true
  OUT="$(CHANNEL_PATCHES_PLUGIN_GLOB="$glob" CHANNEL_PATCHES_BUN="$BUN_BIN" bash "$INSTALLER" "$@" 2>&1)"
  RC=$?
  [[ "$VERBOSE" == "1" ]] && { echo "---- installer output ----"; echo "$OUT"; echo "--------------------------"; }
}

echo "== test_install_channel_patches.sh =="
echo "   installer:        $INSTALLER"
echo "   plugin deps src:   $PLUGIN_SRC"
echo "   pristine server:   $PRISTINE_SERVER"
echo ""

# ── A. fresh/un-patched dir -> BOTH patches applied ─────────────────────────
echo "A. fresh (simulated version-bump) plugin dir gets BOTH patches"
ROOT_A="$WORK/a"; TGT_A="$(make_fixture "$ROOT_A")"
GLOB_A="$ROOT_A/claude-plugins-official/telegram/*/"
run_installer "$GLOB_A"
[[ "$RC" == "0" ]] && ok "installer exits 0 (default/hook mode)" || bad "installer exit was $RC"
[[ "$(grep -c bootRearmNotification "$TGT_A/server.ts")" == "2" ]] && ok "boot_rearm wired (import + call site)" || bad "boot_rearm not wired"
grep -q "bubble-inject" "$TGT_A/server.ts" && ok "bubble-inject marker present" || bad "bubble-inject marker missing"
[[ -f "$TGT_A/boot_rearm.ts" ]] && ok "boot_rearm.ts copied" || bad "boot_rearm.ts not copied"
[[ "$(ls "$TGT_A"/server.ts.bak-boot-rearm-* 2>/dev/null | wc -l | tr -d ' ')" == "1" ]] && ok "one boot_rearm backup" || bad "expected exactly one boot_rearm backup"
[[ "$(ls "$TGT_A"/server.ts.bak-bubble-inject-* 2>/dev/null | wc -l | tr -d ' ')" == "1" ]] && ok "one bubble-inject backup" || bad "expected exactly one bubble-inject backup"
( cd "$TGT_A" && PATH="$(dirname "$BUN_BIN"):$PATH" "$BUN_BIN" build server.ts --target=node --outdir="$WORK/a-build-check" ) >"$WORK/a-build-check.log" 2>&1
[[ "$?" == "0" ]] && ok "independently: patched server.ts still bun-builds" || bad "independent bun build FAILED (see $WORK/a-build-check.log)"

# ── B. idempotency — re-run on the now-patched dir is a clean no-op ─────────
echo "B. re-run on an already-fully-patched dir is a no-op"
run_installer "$GLOB_A"
[[ "$RC" == "0" ]] && ok "re-run exits 0" || bad "re-run exit was $RC"
[[ "$(grep -c bootRearmNotification "$TGT_A/server.ts")" == "2" ]] && ok "boot_rearm still wired exactly once" || bad "boot_rearm wiring count changed"
[[ "$(ls "$TGT_A"/server.ts.bak-boot-rearm-* 2>/dev/null | wc -l | tr -d ' ')" == "1" ]] && ok "no NEW boot_rearm backup" || bad "re-run created an extra boot_rearm backup"
[[ "$(ls "$TGT_A"/server.ts.bak-bubble-inject-* 2>/dev/null | wc -l | tr -d ' ')" == "1" ]] && ok "no NEW bubble-inject backup" || bad "re-run created an extra bubble-inject backup"
echo "$OUT" | grep -q "bubble-inject: already present" && ok "reports bubble-inject no-op" || bad "missing bubble-inject no-op log line"

# ── C. partial state — boot_rearm present, bubble-inject missing ───────────
echo "C. partial state: only the MISSING patch gets applied"
ROOT_C="$WORK/c"; TGT_C="$(make_fixture "$ROOT_C" boot_rearm_only)"
GLOB_C="$ROOT_C/claude-plugins-official/telegram/*/"
[[ "$(grep -c bootRearmNotification "$TGT_C/server.ts")" == "2" ]] || { echo "FATAL: fixture setup for C failed (boot_rearm not pre-wired)"; exit 2; }
! grep -q "bubble-inject" "$TGT_C/server.ts" || { echo "FATAL: fixture setup for C failed (bubble-inject already present)"; exit 2; }
BEFORE_C_MD5="$(md5sum "$TGT_C/boot_rearm.ts" 2>/dev/null | cut -d' ' -f1)"
run_installer "$GLOB_C"
[[ "$RC" == "0" ]] && ok "partial-state run exits 0" || bad "partial-state run exit was $RC"
[[ "$(grep -c bootRearmNotification "$TGT_C/server.ts")" == "2" ]] && ok "pre-existing boot_rearm wiring untouched" || bad "boot_rearm wiring changed unexpectedly"
grep -q "bubble-inject" "$TGT_C/server.ts" && ok "bubble-inject now applied" || bad "bubble-inject was not applied"
AFTER_C_MD5="$(md5sum "$TGT_C/boot_rearm.ts" 2>/dev/null | cut -d' ' -f1)"
[[ "$BEFORE_C_MD5" == "$AFTER_C_MD5" ]] && ok "boot_rearm.ts left byte-identical" || bad "boot_rearm.ts was rewritten unnecessarily"

# ── D. --dry-run touches nothing ────────────────────────────────────────────
echo "D. --dry-run footprint on a fresh dir"
ROOT_D="$WORK/d"; TGT_D="$(make_fixture "$ROOT_D")"
GLOB_D="$ROOT_D/claude-plugins-official/telegram/*/"
run_installer "$GLOB_D" --dry-run
[[ "$RC" == "0" ]] && ok "dry-run exits 0" || bad "dry-run exit was $RC"
[[ ! -f "$TGT_D/boot_rearm.ts" ]] && ok "dry-run did NOT copy boot_rearm.ts" || bad "dry-run copied boot_rearm.ts"
! grep -q "bootRearmNotification" "$TGT_D/server.ts" && ok "dry-run did NOT wire boot_rearm" || bad "dry-run wired boot_rearm"
! grep -q "bubble-inject" "$TGT_D/server.ts" && ok "dry-run did NOT wire bubble-inject" || bad "dry-run wired bubble-inject"
[[ -z "$(ls "$TGT_D"/server.ts.bak-* 2>/dev/null)" ]] && ok "dry-run created NO backups" || bad "dry-run created a backup"

# ── E. --strict surfaces failure; default mode stays fail-open ─────────────
echo "E. drift (missing anchor): --strict fails loudly, default mode fails open"
ROOT_E="$WORK/e"; TGT_E="$(make_fixture "$ROOT_E")"
GLOB_E="$ROOT_E/claude-plugins-official/telegram/*/"
# Corrupt the anchor so bubble-inject can't find its insertion point (simulates
# plugin drift), while leaving enough of the file intact for boot_rearm's OWN
# anchor (a different string) to still apply cleanly.
sed -i.orig "s/await mcp.connect(new StdioServerTransport())/await mcp_connect_renamed_by_test()/" "$TGT_E/server.ts"
run_installer "$GLOB_E" --strict
[[ "$RC" != "0" ]] && ok "--strict exits nonzero on drift" || bad "--strict should have failed (rc=$RC)"
run_installer "$GLOB_E"
[[ "$RC" == "0" ]] && ok "default (non-strict) mode still exits 0 on the same drift (fail-open)" || bad "default mode should stay fail-open (rc=$RC)"
! grep -q "bubble-inject" "$TGT_E/server.ts" && ok "bubble-inject correctly NOT applied (anchor missing)" || bad "bubble-inject was applied despite missing anchor"

# ── F. host-agnostic: a Mac-shaped glob (no /home/claude anywhere) works ───
echo "F. host-agnostic glob (Mac-shaped path, the actual #956 regression)"
ROOT_F="$WORK/Users/faketestuser/.claude/plugins/cache"; TGT_F="$(make_fixture "$ROOT_F")"
GLOB_F="$ROOT_F/claude-plugins-official/telegram/*/"
case "$GLOB_F" in
  */home/claude/*) echo "FATAL: test setup bug — glob still contains /home/claude"; exit 2 ;;
esac
run_installer "$GLOB_F"
[[ "$RC" == "0" ]] && ok "Mac-shaped glob: installer exits 0" || bad "Mac-shaped glob: installer exit was $RC"
[[ "$(grep -c bootRearmNotification "$TGT_F/server.ts")" == "2" ]] && ok "Mac-shaped glob: boot_rearm applied" || bad "Mac-shaped glob: boot_rearm NOT applied"
grep -q "bubble-inject" "$TGT_F/server.ts" && ok "Mac-shaped glob: bubble-inject applied" || bad "Mac-shaped glob: bubble-inject NOT applied"

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
