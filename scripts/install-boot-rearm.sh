#!/usr/bin/env bash
# install-boot-rearm.sh — wire the ops-loop "/loop boot re-arm" into the
# live telegram channel plugin.
#
# Part of the bubble-ops-loop install package. Idempotent: safe to re-run on
# every deploy / fresh box bring-up.
#
# WHAT IT DOES
#   The telegram channel plugin can re-arm a dept's /loop on poller startup by
#   self-injecting ONE synthetic "boot" turn straight into Claude via the same
#   MCP channel notification a real inbound message uses — bypassing Telegram
#   entirely (which a bot's own outbound message can never trigger; that was
#   the bug in the retired bubble-loop-reinit.sh). The mechanism lives in two
#   files inside the plugin dir:
#     - boot_rearm.ts  : bootRearmNotification(env) → payload | null
#                        (fires only when OPS_LOOP_BOOT_REARM=1; reads OPS_LOOP_DEPT)
#     - server.ts      : 3-line wiring that calls it at the END of bot.start()'s
#                        onStart callback.
#   The plugin cache is VOLATILE (re-extracted on plugin update), so the
#   source-of-truth lives in this repo and this installer re-applies it.
#
#   This installer:
#     1. Locates the telegram plugin dir (newest version under the cache).
#     2. Installs boot_rearm.ts into it (copy; only if missing or differs).
#     3. Applies the server.ts wiring patch IDEMPOTENTLY:
#          - if `grep -q bootRearmNotification server.ts` → already wired, skip.
#          - else backup server.ts, then `patch -p0 < the.patch`.
#          - if the patch does NOT apply (plugin version drift), FAIL LOUDLY
#            and restore the backup — never leave server.ts half-patched.
#     4. Validates with `bun build server.ts` from WITHIN the plugin dir (so
#          grammy/@modelcontextprotocol deps resolve). Must exit 0; else
#          restore the backup and abort.
#
#   It does NOT restart any service. Rick controls restarts.
#
# Usage (on the box, as the `claude` user):
#   bash scripts/install-boot-rearm.sh
#   bash scripts/install-boot-rearm.sh --dry-run
#
# Exit codes:
#   0  installed (or already wired — no-op)
#   2  structural error (repo files / plugin dir / bun missing)
#   3  patch failed to apply (plugin version drift) — server.ts restored
#   4  bun build failed on the patched result — server.ts restored

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$PROJECT_ROOT/deploy/telegram-plugin"
SRC_BOOT_REARM="$SRC_DIR/boot_rearm.ts"
SRC_PATCH="$SRC_DIR/server.ts.boot-rearm.patch"

# Plugin cache glob (newest version wins). Overridable for tests.
PLUGIN_GLOB="${BOOT_REARM_PLUGIN_GLOB:-/home/claude/.claude/plugins/cache/claude-plugins-official/telegram/*/}"
BUN_BIN="${BOOT_REARM_BUN:-/home/claude/.bun/bin/bun}"

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

say() { echo "[install-boot-rearm] $*"; }
run() { if [[ "$DRY" == "1" ]]; then echo "  DRY: $*"; else eval "$*"; fi; }

# ── structural preconditions ────────────────────────────────────────────────
[[ -f "$SRC_BOOT_REARM" ]] || { echo "ERR: missing $SRC_BOOT_REARM" >&2; exit 2; }
[[ -f "$SRC_PATCH" ]]      || { echo "ERR: missing $SRC_PATCH" >&2; exit 2; }

# Resolve the newest plugin dir from the glob.
PLUGIN_DIR=""
for d in $(ls -d $PLUGIN_GLOB 2>/dev/null | sort -V); do
  [[ -d "$d" ]] && PLUGIN_DIR="$d"
done
if [[ -z "$PLUGIN_DIR" ]]; then
  echo "ERR: no telegram plugin dir matched: $PLUGIN_GLOB" >&2
  exit 2
fi
PLUGIN_DIR="${PLUGIN_DIR%/}"
say "plugin dir: $PLUGIN_DIR"

SERVER_TS="$PLUGIN_DIR/server.ts"
DST_BOOT_REARM="$PLUGIN_DIR/boot_rearm.ts"
[[ -f "$SERVER_TS" ]] || { echo "ERR: server.ts not found in plugin dir: $SERVER_TS" >&2; exit 2; }

# ── step 1: install boot_rearm.ts (copy only if missing or differs) ──────────
if [[ -f "$DST_BOOT_REARM" ]] && cmp -s "$SRC_BOOT_REARM" "$DST_BOOT_REARM"; then
  say "boot_rearm.ts already present and identical — skip copy"
else
  say "installing boot_rearm.ts → $DST_BOOT_REARM"
  run "cp '$SRC_BOOT_REARM' '$DST_BOOT_REARM'"
fi

# ── step 2: wire server.ts (idempotent) ─────────────────────────────────────
if grep -q "bootRearmNotification" "$SERVER_TS"; then
  say "server.ts already wired (bootRearmNotification present) — no-op"
  say "done (idempotent no-op)."
  exit 0
fi

# Not yet wired — verify the patch WOULD apply before touching anything.
if ! patch -p0 --dry-run --directory="$PLUGIN_DIR" < "$SRC_PATCH" >/dev/null 2>&1; then
  echo "ERR: server.ts.boot-rearm.patch does NOT apply to $SERVER_TS" >&2
  echo "     The plugin server.ts has drifted from the version this patch was" >&2
  echo "     built against. REGENERATE the patch (see deploy/telegram-plugin/" >&2
  echo "     README or INSTALL.md) — refusing to leave the loop unwired." >&2
  exit 3
fi

TS="$(date -u +%Y%m%d-%H%M%S)"
BAK="$SERVER_TS.bak-boot-rearm-$TS"

if [[ "$DRY" == "1" ]]; then
  say "DRY: would back up server.ts → $BAK"
  say "DRY: would apply patch:  patch -p0 --directory='$PLUGIN_DIR' < '$SRC_PATCH'"
  say "DRY: would bun build for validation; restore backup on failure"
  say "done (dry-run)."
  exit 0
fi

say "backing up server.ts → $BAK"
cp "$SERVER_TS" "$BAK"

say "applying server.ts boot-rearm patch"
if ! patch -p0 --directory="$PLUGIN_DIR" < "$SRC_PATCH" >/dev/null 2>&1; then
  echo "ERR: patch apply failed unexpectedly after dry-run passed — restoring backup" >&2
  cp "$BAK" "$SERVER_TS"
  exit 3
fi

# ── step 3: validate with bun build (from WITHIN the plugin dir so deps resolve) ──
if [[ ! -x "$BUN_BIN" ]]; then
  echo "ERR: bun not found at $BUN_BIN (set BOOT_REARM_BUN) — restoring backup" >&2
  cp "$BAK" "$SERVER_TS"
  exit 2
fi

say "validating patched server.ts with bun build"
BUILD_OUT="$(mktemp -d)"
if ( cd "$PLUGIN_DIR" && PATH="$(dirname "$BUN_BIN"):$PATH" "$BUN_BIN" build server.ts --target=node --outdir="$BUILD_OUT" ) >/tmp/install-boot-rearm-build.log 2>&1; then
  say "bun build OK — boot re-arm wired into $SERVER_TS"
  rm -rf "$BUILD_OUT"
else
  echo "ERR: bun build FAILED on patched server.ts — restoring backup" >&2
  tail -8 /tmp/install-boot-rearm-build.log >&2 || true
  cp "$BAK" "$SERVER_TS"
  rm -rf "$BUILD_OUT"
  exit 4
fi

say "done. (Rick: restart the dept services to pick up the new plugin code.)"
exit 0
