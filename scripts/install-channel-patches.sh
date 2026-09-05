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
#   2. Takes an EXCLUSIVE LOCK scoped to that plugin's telegram/ dir (see
#      CONCURRENCY below) before touching anything.
#   3. boot_rearm    — delegates to the existing, tested scripts/install-boot-rearm.sh
#      (grep-preflight -> patch --dry-run check -> backup -> apply -> `bun build`
#      validate -> restore-on-failure). This script does NOT reimplement that
#      logic; it just points the tested installer at the resolved plugin dir.
#   4. bubble-inject — same rigor, freshly implemented here (the sibling patch
#      is a verbatim insert-after-anchor, not a `patch(1)` diff): grep-preflight
#      -> anchor-presence check -> timestamped backup -> insert the CANONICAL
#      block from deploy/telegram-plugin/bubble-inject.block.ts -> `bun build`
#      validate -> restore-on-failure.
#   5. Releases the lock. Logs a one-line outcome per patch (to stderr + syslog
#      via `logger`) so a fleet-health scan can grep for repeated failures.
#
# CONCURRENCY (board #956 review fix): on the VPS, EVERY dept runs as the same
# `claude` user with the same $HOME, so ALL depts share ONE telegram plugin
# cache — one `server.ts` per host. A fleet-wide restart (watchdog or an
# operator) starts many depts ~together, so this script's own ExecStartPre/
# pre-launch invocations run CONCURRENTLY against that SAME file. Without
# mutual exclusion that's a real race: double-insert, a half-written file
# failing `bun build`, or one run's restore-on-failure reverting another run's
# good patch moments after it landed.
#
#   Lock scope: one lock per HOST, covering the telegram/ dir that holds every
#   plugin version (not just the resolved PLUGIN_DIR) — because all versions
#   under it share the same underlying race (a mid-bump moment could have two
#   dirs briefly relevant) and because the lock must be stable across a
#   version bump, not recomputed per-version.
#     default lock path: <dirname of resolved PLUGIN_DIR>/.install-channel-patches.lock
#     override:           CHANNEL_PATCHES_LOCK_PATH
#
#   Mechanism: prefers real `flock(1)` (present on the VPS via util-linux —
#   confirmed on the box this actually races on) taking an exclusive lock on an
#   fd tied to the lock file, bounded by -w so a wedged holder can never hang a
#   dept's startup. Stock macOS ships NO `flock` binary (only Linux does, via
#   util-linux), so when `flock` isn't on PATH this falls back to a portable
#   mkdir-based lock (mkdir is atomic on every POSIX filesystem — no external
#   dependency) with the SAME timeout + a stale-lock reap (a crashed holder's
#   lock dir older than CHANNEL_PATCHES_LOCK_STALE_SEC is force-reaped rather
#   than wedging the fleet forever). Both paths give the identical guarantee:
#   only one invocation is ever inside the boot_rearm+bubble-inject critical
#   section at a time, on a given host.
#
#   Fail-open on lock timeout: if the lock can't be acquired within
#   CHANNEL_PATCHES_LOCK_TIMEOUT_SEC (default 30s — comfortably above a full
#   boot_rearm+bubble-inject run, which is dominated by two `bun build` calls),
#   this run SKIPS the critical section entirely (never touches server.ts) and
#   logs loudly, rather than blocking the dept from starting. In practice two
#   concurrent invocations just SERIALIZE: the second waits for the first,
#   then finds both patches already present and no-ops (idempotency, unchanged
#   from before this fix) — it does not need the timeout path at all.
#
# IDEMPOTENT: re-running when both patches are already present is a pure no-op
# (two `grep -q` preflights, no writes, no backups). Safe to run on every
# service/session start, including many times concurrently (serialized by the
# lock above).
#
# FAIL-OPEN BY DEFAULT (the pre-launch-hook contract): never blocks a dept from
# starting — this script exits 0 by default even if a patch failed to apply,
# or the lock could not be acquired in time (the failure is logged loudly; the
# dept just runs without that patch this time, same as it would have with NO
# installer at all). Pass --strict to get a real nonzero exit code instead
# (used by the test harness / manual runs where you WANT to know if it
# actually worked).
#
# REVERSIBLE: every write is preceded by a timestamped backup
# (server.ts.bak-boot-rearm-<ts> / server.ts.bak-bubble-inject-<ts>); a failed
# `bun build` after either patch restores that patch's own backup, never
# touching a patch applied earlier in the same run.
#
# Usage:
#   install-channel-patches.sh                # hook mode (fail-open, exit 0)
#   install-channel-patches.sh --dry-run       # report what would happen, touch nothing
#   install-channel-patches.sh --strict        # exit nonzero if either patch (or the lock) failed
#   install-channel-patches.sh --dry-run --strict
#
# Env overrides (host-agnostic; both default to $HOME so the SAME script works
# unmodified on a Mac local dept and a VPS `claude`-user dept):
#   CHANNEL_PATCHES_PLUGIN_GLOB      default: $HOME/.claude/plugins/cache/claude-plugins-official/telegram/*/
#   CHANNEL_PATCHES_BUN              default: $HOME/.bun/bin/bun (falls back to `command -v bun`)
#   CHANNEL_PATCHES_LOCK_PATH        default: <telegram-dir>/.install-channel-patches.lock
#   CHANNEL_PATCHES_LOCK_TIMEOUT_SEC default: 30
#   CHANNEL_PATCHES_LOCK_STALE_SEC   default: 120 (mkdir-lock fallback only)
#   CHANNEL_PATCHES_DEBUG_LOCK_SLEEP test-only: seconds to sleep AFTER acquiring
#                                     the lock, before doing any work — used by
#                                     tests/test_install_channel_patches.sh to
#                                     deterministically widen the race window.
#
# Exit codes (only meaningful with --strict; default mode always exits 0):
#   0  both patches present/applied OK (or nothing to do — no plugin found)
#   1  at least one patch (or the lock acquisition itself) failed
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BOOT_REARM_INSTALLER="$SCRIPT_DIR/install-boot-rearm.sh"
INJECT_BLOCK="$PROJECT_ROOT/deploy/telegram-plugin/bubble-inject.block.ts"

PLUGIN_GLOB="${CHANNEL_PATCHES_PLUGIN_GLOB:-$HOME/.claude/plugins/cache/claude-plugins-official/telegram/*/}"
BUN_BIN="${CHANNEL_PATCHES_BUN:-$HOME/.bun/bin/bun}"
[[ -x "$BUN_BIN" ]] || BUN_BIN="$(command -v bun 2>/dev/null || true)"

LOCK_TIMEOUT_SEC="${CHANNEL_PATCHES_LOCK_TIMEOUT_SEC:-30}"
LOCK_STALE_SEC="${CHANNEL_PATCHES_LOCK_STALE_SEC:-120}"

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

# ── the critical section: everything that reads/patches/validates server.ts ─
# Runs ONLY while the lock (below) is held. Sets no variables the caller
# depends on — its outcome is entirely conveyed by its return code, so it is
# safe to invoke from inside a subshell (the flock path below needs exactly
# that: `exec {fd}> ...; flock ... 9 && run_critical_section` runs in a
# subshell, whose variable writes would NOT be visible to the parent shell).
run_critical_section() {
  local section_rc=0

  # Test-only hook: widen the race window deterministically so the
  # concurrency test doesn't depend on real scheduling luck. Unset in normal
  # (hook/production) use.
  if [[ -n "${CHANNEL_PATCHES_DEBUG_LOCK_SLEEP:-}" ]]; then
    log "debug: sleeping ${CHANNEL_PATCHES_DEBUG_LOCK_SLEEP}s while holding the lock (test-only, pid $$)"
    sleep "${CHANNEL_PATCHES_DEBUG_LOCK_SLEEP}"
  fi

  # ── 1. boot_rearm — delegate to the tested, dedicated installer ───────────
  if [[ -f "$BOOT_REARM_INSTALLER" ]]; then
    local br_args=()
    [[ "$DRY" == "1" ]] && br_args+=(--dry-run)
    # NOTE: no `-t` flag — BSD mktemp (macOS) accepts `-t PREFIX` with no X's
    # and appends its own random suffix, but GNU mktemp (the VPS) requires the
    # template itself to end in a run of X's and errors loudly
    # ("too few X's in template") on a bare prefix. A template path with
    # trailing X's and NO `-t` behaves identically on both implementations.
    local br_out; br_out="$(mktemp "${TMPDIR:-/tmp}/install-channel-patches-boot-rearm.XXXXXX")"
    if [[ -z "$br_out" || ! -f "$br_out" ]]; then
      # Defensive: if mktemp itself ever fails (disk full, no /tmp write
      # access, etc.), fail loudly HERE instead of cascading into a confusing
      # "tail: cannot open '' " error from a downstream command run against an
      # empty path (exactly how the GNU-mktemp template bug first surfaced).
      log "boot_rearm: mktemp failed to create a scratch file — skipping this run's boot_rearm step"
      section_rc=1
    elif BOOT_REARM_PLUGIN_GLOB="$PLUGIN_GLOB" BOOT_REARM_BUN="$BUN_BIN" \
        bash "$BOOT_REARM_INSTALLER" "${br_args[@]+"${br_args[@]}"}" >"$br_out" 2>&1; then
      log "boot_rearm: OK ($(tail -1 "$br_out"))"
    else
      local br_rc=$?
      log "boot_rearm: FAILED (rc=$br_rc) — $(tail -3 "$br_out" | tr '\n' ' ')"
      section_rc=1
    fi
    [[ -n "$br_out" ]] && rm -f "$br_out"
  else
    log "boot_rearm installer not found at $BOOT_REARM_INSTALLER — skip"
    section_rc=1
  fi

  # ── 2. bubble-inject — idempotent anchor-insert + bun-build validate ──────
  apply_bubble_inject
  local inject_rc=$?
  [[ "$inject_rc" != "0" ]] && section_rc=1

  return "$section_rc"
}

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

  local build_out build_log
  build_out="$(mktemp -d)"
  # Board #1150: per-invocation mktemp, not a fixed
  # /tmp/install-channel-patches-inject-build.log — under per-dept OS-user
  # isolation each agent user runs this hook independently; a shared fixed
  # path is owned by whichever user creates it first, so every OTHER user's
  # redirect into it fails with Permission denied (the build itself
  # succeeds; only the log redirect breaks, corrupting the exit check).
  build_log="$(mktemp "${TMPDIR:-/tmp}/install-channel-patches-inject-build.XXXXXX")"
  if ( cd "$PLUGIN_DIR" && PATH="$(dirname "$BUN_BIN"):$PATH" "$BUN_BIN" build server.ts --target=node --outdir="$build_out" ) \
      >"$build_log" 2>&1; then
    log "bubble-inject: applied + bun build OK ($SERVER_TS)"
    rm -rf "$build_out"
    rm -f "$build_log"
    return 0
  else
    log "bubble-inject: bun build FAILED — restoring backup (see $build_log)"
    cp "$bak" "$SERVER_TS"
    rm -rf "$build_out"
    # Deliberately NOT removed on failure — the path is named in the log
    # line above so an operator can inspect it; each invocation gets its
    # own uniquely-named mktemp file, so leftovers never collide.
    return 4
  fi
}

# ── mkdir-based portable lock fallback (no `flock` binary on this host) ─────
# mkdir is atomic on every POSIX filesystem, so "did I create the dir" is a
# race-free test-and-set with zero external dependencies — the same guarantee
# flock(1) gives, just without needing util-linux (which stock macOS lacks).
_mkdir_lock_acquire() {  # $1 = lock dir path
  local dir="$1" waited=0
  while ! mkdir "$dir" 2>/dev/null; do
    if [[ -d "$dir" ]]; then
      local mtime now age
      mtime="$(stat -f %m "$dir" 2>/dev/null || stat -c %Y "$dir" 2>/dev/null || echo 0)"
      now="$(date +%s)"
      age=$(( now - mtime ))
      if (( age > LOCK_STALE_SEC )); then
        log "mkdir-lock: stale lock at $dir (age ${age}s > ${LOCK_STALE_SEC}s) — reaping a crashed holder"
        rm -rf "$dir" 2>/dev/null || true
        continue
      fi
    fi
    sleep 1
    waited=$((waited + 1))
    if (( waited >= LOCK_TIMEOUT_SEC )); then
      return 1
    fi
  done
  echo "$$" > "$dir/pid" 2>/dev/null || true
  return 0
}
_mkdir_lock_release() { rm -rf "$1" 2>/dev/null || true; }

# ── acquire the lock, run the critical section, release ─────────────────────
LOCK_TELEGRAM_DIR="$(dirname "$PLUGIN_DIR")"
LOCK_PATH="${CHANNEL_PATCHES_LOCK_PATH:-$LOCK_TELEGRAM_DIR/.install-channel-patches.lock}"

if command -v flock >/dev/null 2>&1; then
  log "lock: using flock(1) on ${LOCK_PATH}.fd (timeout ${LOCK_TIMEOUT_SEC}s)"
  (
    exec 9>"${LOCK_PATH}.fd"
    if flock -w "$LOCK_TIMEOUT_SEC" 9; then
      run_critical_section
      exit $?
    else
      exit 75   # sentinel: lock not acquired (distinct from a real patch failure)
    fi
  )
  SECTION_RC=$?
else
  log "lock: flock(1) not found on PATH — using the portable mkdir-based lock at $LOCK_PATH (timeout ${LOCK_TIMEOUT_SEC}s)"
  if _mkdir_lock_acquire "$LOCK_PATH"; then
    trap '_mkdir_lock_release "$LOCK_PATH"' EXIT
    run_critical_section
    SECTION_RC=$?
    _mkdir_lock_release "$LOCK_PATH"
    trap - EXIT
  else
    SECTION_RC=75
  fi
fi

OVERALL_RC=0
if [[ "$SECTION_RC" == "75" ]]; then
  log "lock: could not acquire within ${LOCK_TIMEOUT_SEC}s — skipping this run untouched (fail-open; another invocation is very likely applying the same patches right now)"
  OVERALL_RC=1
elif [[ "$SECTION_RC" != "0" ]]; then
  OVERALL_RC=1
fi

log "done. overall_rc=$OVERALL_RC (strict=$STRICT dry_run=$DRY)"
finish "$OVERALL_RC"
