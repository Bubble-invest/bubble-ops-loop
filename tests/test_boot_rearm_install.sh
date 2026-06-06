#!/usr/bin/env bash
# =============================================================================
# test_boot_rearm_install.sh — bash harness for install-boot-rearm.sh + the
# boot_rearm.ts re-arm logic.
#
# Covers, in order:
#
#   A. DRY-RUN footprint — `--dry-run` against a FRESH (unwired) fake plugin
#      dir must touch NOTHING: no boot_rearm.ts copied, no server.ts edit,
#      no backup file.
#
#   B. Fresh install — into an unwired fake plugin dir: boot_rearm.ts is
#      copied, server.ts gains the `bootRearmNotification` wiring (twice:
#      import + call site), a backup is created, the post-patch `bun build`
#      passes, and the script exits 0.
#
#   C. Idempotency — re-running the installer is a clean no-op: server.ts
#      stays wired exactly once, NO new backup is created, exit 0.
#
#   D. Drift / fail-loud — when server.ts has drifted so the patch cannot
#      apply, the installer FAILS LOUDLY (exit 3), leaves server.ts BYTE-FOR-
#      BYTE unchanged (no partial wiring), and creates no backup.
#
#   E. boot_rearm.ts logic — OPS_LOOP_BOOT_REARM unset → returns null;
#      =1 → returns a payload with the right method / source / user_id and
#      the dept threaded through from OPS_LOOP_DEPT.
#
# Hermetic: builds throw-away fake plugin dirs under a tmpdir, each with a
# COPY of the pristine plugin server.ts + the real package.json + a SYMLINK to
# the real plugin node_modules (so `bun build` can resolve grammy /
# @modelcontextprotocol without a network install). The live plugin cache and
# live units are NEVER touched.
#
# Requirements on the box: bun at /home/claude/.bun/bin/bun, the live telegram
# plugin present (used only as the source of a PRISTINE server.ts + node_modules
# for the fixtures). Override via env:
#   BOOT_REARM_TEST_PLUGIN_SRC  (default: newest /home/claude/.claude/plugins/
#                                cache/claude-plugins-official/telegram/*/)
#   BOOT_REARM_BUN              (default: /home/claude/.bun/bin/bun)
#
# Run:  bash tests/test_boot_rearm_install.sh
#       bash tests/test_boot_rearm_install.sh -v   # verbose (show installer out)
# =============================================================================
set -uo pipefail

VERBOSE=0
[[ "${1:-}" == "-v" ]] && VERBOSE=1

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$HERE/.." && pwd)}"
INSTALLER="$REPO_ROOT/scripts/install-boot-rearm.sh"
REPO_BOOT_REARM="$REPO_ROOT/deploy/telegram-plugin/boot_rearm.ts"
REPO_PATCH="$REPO_ROOT/deploy/telegram-plugin/server.ts.boot-rearm.patch"
BUN_BIN="${BOOT_REARM_BUN:-/home/claude/.bun/bin/bun}"

[[ -f "$INSTALLER" ]]       || { echo "FATAL: installer not found: $INSTALLER"; exit 2; }
[[ -f "$REPO_BOOT_REARM" ]] || { echo "FATAL: repo boot_rearm.ts not found: $REPO_BOOT_REARM"; exit 2; }
[[ -f "$REPO_PATCH" ]]      || { echo "FATAL: repo patch not found: $REPO_PATCH"; exit 2; }
[[ -x "$BUN_BIN" ]]         || { echo "FATAL: bun not found: $BUN_BIN"; exit 2; }

# Locate a PRISTINE plugin source (newest version) to copy fixtures from.
PLUGIN_SRC="${BOOT_REARM_TEST_PLUGIN_SRC:-}"
if [[ -z "$PLUGIN_SRC" ]]; then
  for d in $(ls -d /home/claude/.claude/plugins/cache/claude-plugins-official/telegram/*/ 2>/dev/null | sort -V); do
    [[ -d "$d" ]] && PLUGIN_SRC="$d"
  done
fi
PLUGIN_SRC="${PLUGIN_SRC%/}"
[[ -f "$PLUGIN_SRC/server.ts" ]]   || { echo "FATAL: no pristine server.ts at $PLUGIN_SRC"; exit 2; }
[[ -d "$PLUGIN_SRC/node_modules" ]] || { echo "FATAL: no node_modules at $PLUGIN_SRC (needed for bun build)"; exit 2; }

# A pristine server.ts MUST be unwired — guard against testing against an
# already-patched live cache (which would make the "fresh install" tests lie).
if grep -q "bootRearmNotification" "$PLUGIN_SRC/server.ts"; then
  echo "FATAL: source server.ts at $PLUGIN_SRC is ALREADY wired — cannot build a"
  echo "       pristine fixture. Point BOOT_REARM_TEST_PLUGIN_SRC at an unwired copy."
  exit 2
fi

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Build a fresh fake plugin cache rooted at $1; echoes the plugin dir path.
make_fixture() {
  local root="$1" wired="${2:-pristine}"
  local tgt="$root/claude-plugins-official/telegram/9.9.9"
  mkdir -p "$tgt"
  if [[ "$wired" == "drift" ]]; then
    printf '// drifted server.ts — no path import anchor, no onStart\nconst x = 1\n' > "$tgt/server.ts"
  else
    cp "$PLUGIN_SRC/server.ts" "$tgt/server.ts"
  fi
  cp "$PLUGIN_SRC/package.json" "$tgt/package.json"
  ln -s "$PLUGIN_SRC/node_modules" "$tgt/node_modules"
  echo "$tgt"
}

run_installer() {
  # usage: run_installer <plugin-dir> [--dry-run]; sets RC + OUT
  local pdir="$1"; shift || true
  local glob="$(dirname "$pdir")/*/"
  OUT="$(BOOT_REARM_PLUGIN_GLOB="$glob" BOOT_REARM_BUN="$BUN_BIN" bash "$INSTALLER" "$@" 2>&1)"
  RC=$?
  [[ "$VERBOSE" == "1" ]] && { echo "---- installer output ----"; echo "$OUT"; echo "--------------------------"; }
}

echo "== test_boot_rearm_install.sh =="
echo "   installer:  $INSTALLER"
echo "   plugin src: $PLUGIN_SRC"
echo ""

# ── A. dry-run footprint ─────────────────────────────────────────────────────
echo "A. dry-run touches nothing"
ROOT_A="$WORK/a"; TGT_A="$(make_fixture "$ROOT_A")"
run_installer "$TGT_A" --dry-run
[[ "$RC" == "0" ]] && ok "dry-run exits 0" || bad "dry-run exit was $RC"
[[ ! -f "$TGT_A/boot_rearm.ts" ]] && ok "dry-run did NOT copy boot_rearm.ts" || bad "dry-run copied boot_rearm.ts"
! grep -q "bootRearmNotification" "$TGT_A/server.ts" && ok "dry-run did NOT edit server.ts" || bad "dry-run edited server.ts"
[[ -z "$(ls "$TGT_A"/server.ts.bak-* 2>/dev/null)" ]] && ok "dry-run created NO backup" || bad "dry-run created a backup"

# ── B. fresh install ─────────────────────────────────────────────────────────
echo "B. fresh install wires + builds"
ROOT_B="$WORK/b"; TGT_B="$(make_fixture "$ROOT_B")"
run_installer "$TGT_B"
[[ "$RC" == "0" ]] && ok "fresh install exits 0" || bad "fresh install exit was $RC"
cmp -s "$REPO_BOOT_REARM" "$TGT_B/boot_rearm.ts" && ok "boot_rearm.ts copied (identical)" || bad "boot_rearm.ts not copied/identical"
[[ "$(grep -c bootRearmNotification "$TGT_B/server.ts")" == "2" ]] && ok "server.ts wired (import + call site)" || bad "server.ts wiring count != 2"
[[ "$(ls "$TGT_B"/server.ts.bak-boot-rearm-* 2>/dev/null | wc -l)" == "1" ]] && ok "one backup created" || bad "expected exactly one backup"
echo "$OUT" | grep -q "bun build OK" && ok "post-patch bun build passed" || bad "bun build did not report OK"

# ── C. idempotency ───────────────────────────────────────────────────────────
echo "C. re-run is idempotent"
run_installer "$TGT_B"
[[ "$RC" == "0" ]] && ok "re-run exits 0" || bad "re-run exit was $RC"
[[ "$(grep -c bootRearmNotification "$TGT_B/server.ts")" == "2" ]] && ok "still wired exactly once" || bad "wiring count changed on re-run"
[[ "$(ls "$TGT_B"/server.ts.bak-boot-rearm-* 2>/dev/null | wc -l)" == "1" ]] && ok "no NEW backup on re-run" || bad "re-run created an extra backup"
echo "$OUT" | grep -q "no-op" && ok "re-run reports no-op" || bad "re-run did not report no-op"

# ── D. drift / fail-loud ─────────────────────────────────────────────────────
echo "D. drift fails loudly, leaves server.ts intact"
ROOT_D="$WORK/d"; TGT_D="$(make_fixture "$ROOT_D" drift)"
BEFORE="$(md5sum "$TGT_D/server.ts" | cut -d' ' -f1)"
run_installer "$TGT_D"
AFTER="$(md5sum "$TGT_D/server.ts" | cut -d' ' -f1)"
[[ "$RC" == "3" ]] && ok "drift exits 3" || bad "drift exit was $RC (expected 3)"
[[ "$BEFORE" == "$AFTER" ]] && ok "server.ts byte-for-byte unchanged" || bad "server.ts was modified on drift"
! grep -q "bootRearmNotification" "$TGT_D/server.ts" && ok "no partial wiring" || bad "partial wiring left behind"
[[ -z "$(ls "$TGT_D"/server.ts.bak-* 2>/dev/null)" ]] && ok "no backup on drift-fail" || bad "drift-fail left a backup"
echo "$OUT" | grep -qi "does NOT apply" && ok "drift error message is loud" || bad "drift error message missing"

# ── E. boot_rearm.ts logic ───────────────────────────────────────────────────
echo "E. boot_rearm.ts re-arm logic"
LOGIC_OUT="$(cd "$REPO_ROOT/deploy/telegram-plugin" && PATH="$(dirname "$BUN_BIN"):$PATH" "$BUN_BIN" -e '
import { bootRearmNotification } from "./boot_rearm.ts";
const off = bootRearmNotification({});
const on  = bootRearmNotification({ OPS_LOOP_BOOT_REARM: "1", OPS_LOOP_DEPT: "maya" });
const checks = {
  unset_null:    off === null,
  set_payload:   on !== null,
  method:        on?.method === "notifications/claude/channel",
  source:        on?.params?.meta?.source === "ops-loop-boot-rearm",
  user_id:       on?.params?.meta?.user_id === "system",
  dept_threaded: on?.params?.meta?.dept === "maya",
};
for (const [k, v] of Object.entries(checks)) console.log(k + "=" + v);
' 2>&1)"
[[ "$VERBOSE" == "1" ]] && echo "$LOGIC_OUT"
for key in unset_null set_payload method source user_id dept_threaded; do
  echo "$LOGIC_OUT" | grep -q "^${key}=true" && ok "boot_rearm.ts: $key" || bad "boot_rearm.ts: $key (got: $(echo "$LOGIC_OUT" | grep "^${key}=" ))"
done

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
