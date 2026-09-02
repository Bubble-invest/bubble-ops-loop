#!/usr/bin/env bash
# install-channel-patches.sh — the DURABLE re-applier for BOTH telegram-plugin
# patches (boot_rearm + bubble-inject), board #956 (supersedes the one-off
# manual re-apply in #1047).
#
# WHY THIS EXISTS
#   The telegram channel plugin lives in a non-git, VOLATILE cache dir:
#     ~/.claude/plugins/cache/claude-plugins-official/telegram/<version>/server.ts
#   Every time the plugin auto-updates to a new <version>, that dir is
#   re-extracted from scratch — WIPING any hand-applied patch. Two independent
#   patches live there:
#     - boot_rearm    : re-arms a dept's /loop on poller startup
#                       (deploy/telegram-plugin/boot_rearm.ts +
#                        server.ts.boot-rearm.patch; installer:
#                        scripts/install-boot-rearm.sh)
#     - bubble-inject : lets an external process deliver a message INTO a live
#                       session (deploy/telegram-plugin/bubble-inject.block.ts;
#                       previously applied ad hoc / via scripts/apply-inject-patch.sh)
#   Before #956, boot_rearm had NO auto-reapply hook at all, and bubble-inject's
#   hook (apply-inject-patch.sh) only ran on the VPS (hardcoded /home/claude
#   path, no bun-build validation, no dry-run) — so a Mac-side plugin bump (the
#   2026-08-15 incident, board #1047) silently wiped the patches with nothing to
#   catch it. This script folds BOTH into one idempotent, HOST-AGNOSTIC
#   installer (Mac or VPS — the plugin glob defaults to $HOME-relative) meant to
#   be re-run on every dept (re)start, from either a systemd ExecStartPre or a
#   Mac launchd wrapper's pre-launch step (see deploy/local/lib/local_loop_lib.sh
#   render_loop_wrapper and deploy/templates/ops-loop-dept.service.template).
#
# WHAT IT DOES, per run
#   1. Resolves the newest installed telegram plugin dir (handles version bumps
#      — a plugin update always sorts higher, so `sort -V` picks it up with no
#      config change needed).
#   2. boot_rearm    — delegates to the existing, tested scripts/install-boot-rearm.sh
#      (grep-preflight -> patch --dry-run check -> backup -> apply -> `bun build`
#      validate -> restore-on-failure). This script does NOT reimplement that
#      logic; it just points the tested installer at the resolved plugin dir.
#   3. bubble-inject — same rigor, freshly implemented here (the sibling patch
#      is a verbatim insert-after-anchor, not a `patch(1)` diff): grep-preflight
#      -> anchor-presence check -> timestamped backup -> insert the CANONICAL
#      block from deploy/telegram-plugin/bubble-inject.block.ts -> `bun build`
#      validate -> restore-on-failure.
#   4. Logs a one-line outcome per patch (to stderr + syslog via `logger`) so a
#      fleet-health scan can grep for repeated failures.
#
# IDEMPOTENT: re-running when both patches are already present is a pure no-op
# (two `grep -q` preflights, no writes, no backups). Safe to run on every
# service/session start.
#
# FAIL-OPEN BY DEFAULT (the pre-launch-hook contract): never blocks a dept from
# starting — this script exits 0 by default even if a patch failed to apply
# (the failure is logged loudly; the dept just runs without that patch this
# time, same as it would have with NO installer at all). Pass --strict to get a
# real nonzero exit code instead (used by the test harness / manual runs where
# you WANT to know if it actually worked).
#
# REVERSIBLE: every write is preceded by a timestamped backup
# (server.ts.bak-boot-rearm-<ts> / server.ts.bak-bubble-inject-<ts>); a failed
# `bun build` after either patch restores that patch's own backup, never
# touching a patch applied earlier in the same run.
#
# Usage:
#   install-channel-patches.sh                # hook mode (fail-open, exit 0)
#   install-channel-patches.sh --dry-run       # report what would happen, touch nothing
#   install-channel-patches.sh --strict        # exit nonzero if either patch failed
#   install-channel-patches.sh --dry-run --strict
#
# Env overrides (host-agnostic; both default to $HOME so the SAME script works
# unmodified on a Mac local dept and a VPS `claude`-user dept):
#   CHANNEL_PATCHES_PLUGIN_GLOB  default: $HOME/.claude/plugins/cache/claude-plugins-official/telegram/*/
#   CHANNEL_PATCHES_BUN          default: $HOME/.bun/bin/bun (falls back to `command -v bun`)
#
# Exit codes (only meaningful with --strict; default mode always exits 0):
#   0  both patches present/applied OK (or nothing to do — no plugin found)
#   1  at least one patch failed to apply (see logged detail)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BOOT_REARM_INSTALLER="$SCRIPT_DIR/install-boot-rearm.sh"
INJECT_BLOCK="$PROJECT_ROOT/deploy/telegram-plugin/bubble-inject.block.ts"

PLUGIN_GLOB="${CHANNEL_PATCHES_PLUGIN_GLOB:-$HOME/.claude/plugins/cache/claude-plugins-official/telegram/*/}"
BUN_BIN="${CHANNEL_PATCHES_BUN:-$HOME/.bun/bin/bun}"
[[ -x "$BUN_BIN" ]] || BUN_BIN="$(command -v bun 2>/dev/null || true)"

STRICT=0
DRY=0
for a in "$@"; do
  case "$a" in
    --strict) STRICT=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "install-channel-patches.sh: unknown arg '$a'" >&2; exit 2 ;;
  esac
done

log() { logger -t install-channel-patches "$*" 2>/dev/null || true; echo "[install-channel-patches] $*" >&2; }

finish() {
  # Fail-open contract: only surface a nonzero exit when explicitly asked
  # (--strict). A pre-launch hook must never keep a dept from starting because
  # a plugin patch failed to (re)apply.
  local rc="$1"
  if [[ "$STRICT" == "1" ]]; then
    exit "$rc"
  fi
  exit 0
}

OVERALL_RC=0

# ── resolve the newest telegram plugin dir (version-bump-proof) ─────────────
PLUGIN_DIR=""
for d in $(ls -d $PLUGIN_GLOB 2>/dev/null | sort -V); do
  [[ -d "$d" ]] && PLUGIN_DIR="$d"
done
if [[ -z "$PLUGIN_DIR" ]]; then
  log "no telegram plugin dir matched glob '$PLUGIN_GLOB' — nothing to patch (fail-open)"
  finish 0
fi
PLUGIN_DIR="${PLUGIN_DIR%/}"
SERVER_TS="$PLUGIN_DIR/server.ts"
if [[ ! -f "$SERVER_TS" ]]; then
  log "no server.ts under $PLUGIN_DIR — skip (fail-open)"
  finish 0
fi
log "plugin dir: $PLUGIN_DIR"

# ── 1. boot_rearm — delegate to the tested, dedicated installer ─────────────
if [[ -f "$BOOT_REARM_INSTALLER" ]]; then
  BR_ARGS=()
  [[ "$DRY" == "1" ]] && BR_ARGS+=(--dry-run)
  BR_OUT="$(mktemp -t install-channel-patches-boot-rearm)"
  if BOOT_REARM_PLUGIN_GLOB="$PLUGIN_GLOB" BOOT_REARM_BUN="$BUN_BIN" \
      bash "$BOOT_REARM_INSTALLER" "${BR_ARGS[@]+"${BR_ARGS[@]}"}" >"$BR_OUT" 2>&1; then
    log "boot_rearm: OK ($(tail -1 "$BR_OUT"))"
  else
    br_rc=$?
    log "boot_rearm: FAILED (rc=$br_rc) — $(tail -3 "$BR_OUT" | tr '\n' ' ')"
    OVERALL_RC=1
  fi
  rm -f "$BR_OUT"
else
  log "boot_rearm installer not found at $BOOT_REARM_INSTALLER — skip"
  OVERALL_RC=1
fi

# ── 2. bubble-inject — idempotent anchor-insert + bun-build validate ────────
apply_bubble_inject() {
  if grep -q "bubble-inject" "$SERVER_TS" 2>/dev/null; then
    log "bubble-inject: already present — no-op"
    return 0
  fi

  if [[ ! -f "$INJECT_BLOCK" ]]; then
    log "bubble-inject: canonical block missing at $INJECT_BLOCK — skip"
    return 2
  fi

  local anchor='await mcp.connect(new StdioServerTransport())'
  if ! grep -qF "$anchor" "$SERVER_TS" 2>/dev/null; then
    log "bubble-inject: anchor not found in $SERVER_TS (plugin drift?) — skip"
    return 3
  fi

  if [[ "$DRY" == "1" ]]; then
    log "bubble-inject: DRY — would back up server.ts, insert the canonical block, bun build validate"
    return 0
  fi

  local ts bak
  ts="$(date -u +%Y%m%d-%H%M%S)"
  bak="${SERVER_TS}.bak-bubble-inject-${ts}"
  cp "$SERVER_TS" "$bak"

  if ! SRV="$SERVER_TS" ANCHOR="$anchor" BLOCK="$INJECT_BLOCK" python3 - <<'PY'
import os, re

p = os.environ["SRV"]
anchor = os.environ["ANCHOR"]
block_path = os.environ["BLOCK"]

BEGIN = "// === BUBBLE-INJECT PATCH BEGIN ==="
END = "// === BUBBLE-INJECT PATCH END ==="
raw = open(block_path).read()
i, j = raw.index(BEGIN), raw.index(END)
patch = raw[i:j + len(END)]

s = open(p).read()
line = anchor + "\n"
if line in s:
    s = s.replace(line, line + patch + "\n", 1)
else:
    m = re.search(re.escape(anchor), s)
    if not m:
        raise SystemExit("anchor vanished")
    idx = s.index("\n", m.end()) + 1
    s = s[:idx] + patch + "\n" + s[idx:]
open(p, "w").write(s)
print("inserted")
PY
  then
    log "bubble-inject: insertion failed — restoring backup"
    cp "$bak" "$SERVER_TS"
    return 3
  fi

  if [[ -z "$BUN_BIN" || ! -x "$BUN_BIN" ]]; then
    log "bubble-inject: bun not found (checked \$CHANNEL_PATCHES_BUN and PATH) — cannot validate, restoring backup"
    cp "$bak" "$SERVER_TS"
    return 2
  fi

  local build_out
  build_out="$(mktemp -d)"
  if ( cd "$PLUGIN_DIR" && PATH="$(dirname "$BUN_BIN"):$PATH" "$BUN_BIN" build server.ts --target=node --outdir="$build_out" ) \
      >/tmp/install-channel-patches-inject-build.log 2>&1; then
    log "bubble-inject: applied + bun build OK ($SERVER_TS)"
    rm -rf "$build_out"
    return 0
  else
    log "bubble-inject: bun build FAILED — restoring backup (see /tmp/install-channel-patches-inject-build.log)"
    cp "$bak" "$SERVER_TS"
    rm -rf "$build_out"
    return 4
  fi
}

apply_bubble_inject
inject_rc=$?
[[ "$inject_rc" != "0" ]] && OVERALL_RC=1

log "done. overall_rc=$OVERALL_RC (strict=$STRICT dry_run=$DRY)"
finish "$OVERALL_RC"
