#!/usr/bin/env bash
# =============================================================================
# install-local-loop-backup.sh — install the Mac BACKUP FLOOR for one host:local
# dept as a launchd agent. The Mac twin of the VPS loop-backup floor
# (scripts/install-loop-backup.sh, systemd timers), for ONE local dept.
#
# WHAT IT INSTALLS:
#   ~/Library/LaunchAgents/com.bubble.ops-loop-backup-<slug>.plist
#   A StartInterval launchd agent that, on a cadence (default every few hours),
#   runs local-loop-backup-runner.sh, which checks the dept's heartbeat
#   staleness and force-ticks the /loop ONLY if it's stale (a safety net for
#   when the main loop session is wedged or the Mac just woke).
#
# WHY a separate floor on the Mac: the VPS loop-backup SKIPS host:local depts
# (B1 — the VPS can't reach the Mac). So a local dept needs its OWN backstop on
# its own machine. Same staleness definition as the VPS (shared loop_backup.py).
#
# DOCTRINE — StartInterval (NOT StartCalendarInterval): the backstop also
# coalesces + fires on wake, so a Mac that was asleep through a backup window
# still gets checked when it reopens.
#
# GENERIC + TEST-SAFE + idempotent — same contract as install-local-loop.sh:
# WITHOUT --activate it only renders the plist (no launchctl). --activate loads
# it. --uninstall removes it.
#
# Usage:
#   install-local-loop-backup.sh --dept-dir <path> --slug <slug>
#                               [--interval <sec>] [--stale-sec <sec>]
#                               [--claude-bin <path>] [--workspace-dir <path>]
#                               [--extra-path <path>] [--dry-tick]
#                               [--launch-agents-dir <dir>] [--log-dir <dir>]
#                               [--activate]
#   install-local-loop-backup.sh --uninstall --slug <slug> [--launch-agents-dir <dir>]
#
# By default the rendered floor ACTUALLY force-ticks when the heartbeat is stale
# (the runner gets --activate-tick baked in). Pass --dry-tick for a detect-only
# floor. For a dept whose loop needs an explicit claude path / workspace skills
# (e.g. content/Miranda), pass --claude-bin + --workspace-dir + --extra-path so
# the baked tick can find claude and load the workspace's .claude/skills.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/local_loop_lib.sh
. "$SCRIPT_DIR/lib/local_loop_lib.sh"

DEPT_DIR=""
SLUG=""
INTERVAL=10800                      # 3h default — a backstop, not the primary cadence
STALE_SEC="$LOCAL_LOOP_STALE_SEC_DEFAULT"   # 90 min, mirrors VPS BUBBLE_BACKUP_STALE_SEC
LAUNCH_AGENTS_DIR="${LOCAL_LOOP_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${LOCAL_LOOP_LOG_DIR:-$HOME/Library/Logs/bubble-ops-loop}"
RUNNER="${LOCAL_LOOP_BACKUP_RUNNER:-$SCRIPT_DIR/local-loop-backup-runner.sh}"
CLAUDE_BIN="${LOCAL_LOOP_CLAUDE_BIN:-}"     # baked into the plist so the tick finds claude (launchd PATH is minimal)
WORKSPACE_DIR="${LOCAL_LOOP_WORKSPACE_DIR:-}"  # --add-dir for brain↔body skill reuse
EXTRA_PATH="${LOCAL_LOOP_EXTRA_PATH:-}"     # prepended to PATH in the runner
DRY_TICK=0                          # default: the floor ACTUALLY ticks when stale. --dry-tick = detect-only (old behaviour)
ACTIVATE=0
UNINSTALL=0

die() { echo "ERR: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dept-dir)            DEPT_DIR="${2:?--dept-dir needs a value}"; shift 2 ;;
        --dept-dir=*)          DEPT_DIR="${1#--dept-dir=}"; shift ;;
        --slug)                SLUG="${2:?--slug needs a value}"; shift 2 ;;
        --slug=*)              SLUG="${1#--slug=}"; shift ;;
        --interval)            INTERVAL="${2:?--interval needs a value}"; shift 2 ;;
        --interval=*)          INTERVAL="${1#--interval=}"; shift ;;
        --stale-sec)           STALE_SEC="${2:?--stale-sec needs a value}"; shift 2 ;;
        --stale-sec=*)         STALE_SEC="${1#--stale-sec=}"; shift ;;
        --launch-agents-dir)   LAUNCH_AGENTS_DIR="${2:?}"; shift 2 ;;
        --launch-agents-dir=*) LAUNCH_AGENTS_DIR="${1#--launch-agents-dir=}"; shift ;;
        --log-dir)             LOG_DIR="${2:?}"; shift 2 ;;
        --log-dir=*)           LOG_DIR="${1#--log-dir=}"; shift ;;
        --runner)              RUNNER="${2:?}"; shift 2 ;;
        --runner=*)            RUNNER="${1#--runner=}"; shift ;;
        --claude-bin)          CLAUDE_BIN="${2:?}"; shift 2 ;;
        --claude-bin=*)        CLAUDE_BIN="${1#--claude-bin=}"; shift ;;
        --workspace-dir)       WORKSPACE_DIR="${2:?}"; shift 2 ;;
        --workspace-dir=*)     WORKSPACE_DIR="${1#--workspace-dir=}"; shift ;;
        --extra-path)          EXTRA_PATH="${2:?}"; shift 2 ;;
        --extra-path=*)        EXTRA_PATH="${1#--extra-path=}"; shift ;;
        --dry-tick)            DRY_TICK=1; shift ;;
        --activate)            ACTIVATE=1; shift ;;
        --uninstall)           UNINSTALL=1; shift ;;
        -h|--help)             sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "unknown argument '$1'" ;;
    esac
done

[[ -n "$SLUG" ]] || die "--slug is required"
LABEL="com.bubble.ops-loop-backup-${SLUG}"
PLIST_PATH="${LAUNCH_AGENTS_DIR%/}/${LABEL}.plist"

say() { echo "[install-local-loop-backup] $*"; }

# ── uninstall ────────────────────────────────────────────────────────────────
if [[ "$UNINSTALL" == "1" ]]; then
    say "uninstalling $LABEL"
    if [[ "$ACTIVATE" == "1" ]]; then
        say "launchctl unload '$PLIST_PATH'"
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
    else
        say "(dry) would: launchctl unload '$PLIST_PATH'  (pass --activate to actually unload)"
    fi
    if [[ -f "$PLIST_PATH" ]]; then
        rm -f "$PLIST_PATH" && say "removed $PLIST_PATH"
    else
        say "no plist at $PLIST_PATH — nothing to remove"
    fi
    exit 0
fi

# ── install / render ─────────────────────────────────────────────────────────
[[ -n "$DEPT_DIR" ]] || die "--dept-dir is required (the dept repo clone on the Mac)"
[[ "$INTERVAL" =~ ^[0-9]+$ ]] || die "--interval must be an integer (seconds)"
[[ "$STALE_SEC" =~ ^[0-9]+$ ]] || die "--stale-sec must be an integer (seconds)"
[[ -f "$RUNNER" ]] || die "backup runner not found: $RUNNER"
chmod +x "$RUNNER" 2>/dev/null || true

[[ -d "$DEPT_DIR" ]] || say "WARNING: dept-dir '$DEPT_DIR' does not exist yet (backup will treat it as stale→would tick once it appears)"

mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR"

ACTIVATE_TICK_VAL=1
[[ "$DRY_TICK" == "1" ]] && ACTIVATE_TICK_VAL=""

say "rendering backup-floor plist:"
say "  label      = $LABEL"
say "  dept-dir   = $DEPT_DIR"
say "  slug       = $SLUG"
say "  interval   = ${INTERVAL}s (StartInterval — backstop, fires on wake if missed)"
say "  stale-sec  = ${STALE_SEC}s (heartbeat older than this → force-tick)"
say "  runner     = $RUNNER"
say "  claude-bin = ${CLAUDE_BIN:-<PATH lookup — set --claude-bin if launchd PATH is minimal>}"
say "  workspace  = ${WORKSPACE_DIR:-<none — tick will be skill-blind unless dept is self-contained>}"
say "  force-tick = $([[ "$DRY_TICK" == "1" ]] && echo 'DRY (detect-only)' || echo 'LIVE (actually ticks when stale)')"
say "  plist      = $PLIST_PATH"

[[ -z "$CLAUDE_BIN" ]] && say "NOTE: no --claude-bin — the tick relies on PATH finding 'claude'. Under launchd the PATH is minimal; pass --claude-bin (and/or --extra-path) or the force-tick will fail 'command not found'."
[[ -z "$WORKSPACE_DIR" && "$DRY_TICK" != "1" ]] && say "NOTE: no --workspace-dir — a tick will not see workspace skills. Fine for a self-contained dept; pass --workspace-dir for a brain↔body dept like content (Miranda_Socials)."

render_backup_plist "$LABEL" "$DEPT_DIR" "$SLUG" "$INTERVAL" "$RUNNER" "$LOG_DIR" \
    "$CLAUDE_BIN" "$WORKSPACE_DIR" "$EXTRA_PATH" "$ACTIVATE_TICK_VAL" > "$PLIST_PATH" \
    || die "failed to render plist to $PLIST_PATH"
say "wrote $PLIST_PATH"

if command -v plutil >/dev/null 2>&1; then
    if plutil -lint "$PLIST_PATH" >/dev/null 2>&1; then
        say "plutil -lint OK"
    else
        die "rendered plist failed plutil -lint: $PLIST_PATH"
    fi
fi

if [[ "$ACTIVATE" == "1" ]]; then
    say "activating: launchctl unload (if loaded) then load '$PLIST_PATH'"
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH" || die "launchctl load failed"
    say "ACTIVATED — $LABEL now checks staleness every ${INTERVAL}s."
else
    say "DRY RENDER complete (no launchctl). To activate:"
    say "  launchctl load '$PLIST_PATH'   # or re-run with --activate"
fi
