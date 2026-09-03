#!/usr/bin/env bash
# =============================================================================
# install-local-tick-watchdog.sh — install the ops-loop TICK watchdog (board
# #724) on a Mac as ONE launchd agent covering EVERY host:local dept on that
# machine (Miranda/M1, Géraldine/M5, rick+tonio/M4 …). The Mac twin of
# scripts/install-loop-tick-watchdog.sh (VPS systemd timer).
#
# WHAT IT INSTALLS
#   ~/Library/LaunchAgents/com.bubble.loop-tick-watchdog.plist
#   A StartInterval agent (default 600s) that runs
#   scripts/loop-tick-watchdog.py --host local from THIS bubble-ops-loop
#   checkout. Depts are discovered at runtime from
#   ~/Library/LaunchAgents/com.bubble.ops-loop-<slug>.plist + their wrappers —
#   a new local dept is covered with ZERO config.
#
# WHAT IT DOES per dept (see scripts/lib/loop_tick_watchdog.py):
#   - stalled tick (last transcript activity = transient API error, idle ≥10min,
#     no subagent/tool in flight, no CronCreate in that turn) → append ONE
#     re-arm line to <TELEGRAM_STATE_DIR>/inject (delivered into the LIVE tmux
#     session by the bubble-inject plugin patch — no new process).
#   - inject proved deaf (same error line still last after the cooldown) →
#     `tmux kill-session -t ops-loop-<slug>`; the KeepAlive wrapper relaunches
#     with --continue (the documented clean-restart recipe). Only when the
#     wrapper carries --continue; otherwise alert-only.
#   - auth / usage-limit / context errors → alert only (a kick can't fix them).
#   - healthy idle (last turn ended normally) → never touched, however old.
#
# DOCTRINE — StartInterval (NOT StartCalendarInterval): coalesces + fires on
# wake, so a Mac that slept through a window still gets checked when it
# reopens (same as the backup floor).
#
# TEST-SAFE: WITHOUT --activate it only renders the plist (no launchctl).
# Idempotent: re-running overwrites the plist (and reloads under --activate).
# --uninstall removes it.
#
# Usage:
#   install-local-tick-watchdog.sh [--interval <sec>] [--launch-agents-dir <dir>]
#                                  [--log-dir <dir>] [--python <path>] [--dry-run-mode]
#                                  [--activate]
#   install-local-tick-watchdog.sh --uninstall [--launch-agents-dir <dir>] [--activate]
#
#   --dry-run-mode  bake BUBBLE_TICKWD_DRY_RUN=1 into the plist (decide + log
#                   only) — the recommended FIRST install on a new Mac; re-run
#                   without it once the log shows only sane verdicts.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNNER="$REPO_ROOT/scripts/loop-tick-watchdog.py"

INTERVAL=600
LAUNCH_AGENTS_DIR="${LOCAL_LOOP_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${LOCAL_LOOP_LOG_DIR:-$HOME/Library/Logs/bubble-ops-loop}"
PYTHON="${LOCAL_TICKWD_PYTHON:-$(command -v python3 || echo /usr/bin/python3)}"
STATE="${BUBBLE_TICKWD_STATE:-$HOME/Library/Application Support/bubble-ops-loop/loop-tick-watchdog.jsonl}"
DRY_MODE=0
ACTIVATE=0
UNINSTALL=0
LABEL="com.bubble.loop-tick-watchdog"

die() { echo "ERR: $*" >&2; exit 2; }
say() { echo "[install-local-tick-watchdog] $*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --interval)            INTERVAL="${2:?}"; shift 2 ;;
        --interval=*)          INTERVAL="${1#--interval=}"; shift ;;
        --launch-agents-dir)   LAUNCH_AGENTS_DIR="${2:?}"; shift 2 ;;
        --launch-agents-dir=*) LAUNCH_AGENTS_DIR="${1#--launch-agents-dir=}"; shift ;;
        --log-dir)             LOG_DIR="${2:?}"; shift 2 ;;
        --log-dir=*)           LOG_DIR="${1#--log-dir=}"; shift ;;
        --python)              PYTHON="${2:?}"; shift 2 ;;
        --python=*)            PYTHON="${1#--python=}"; shift ;;
        --state)               STATE="${2:?}"; shift 2 ;;
        --dry-run-mode)        DRY_MODE=1; shift ;;
        --activate)            ACTIVATE=1; shift ;;
        --uninstall)           UNINSTALL=1; shift ;;
        -h|--help)             sed -n '2,45p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "unknown argument '$1'" ;;
    esac
done

PLIST_PATH="${LAUNCH_AGENTS_DIR%/}/${LABEL}.plist"

xml_escape() { local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; printf '%s' "$s"; }

if [[ "$UNINSTALL" == "1" ]]; then
    say "uninstalling $LABEL"
    if [[ "$ACTIVATE" == "1" ]]; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
    else
        say "(dry) would: launchctl unload '$PLIST_PATH'  (pass --activate to actually unload)"
    fi
    [[ -f "$PLIST_PATH" ]] && rm -f "$PLIST_PATH" && say "removed $PLIST_PATH"
    exit 0
fi

[[ "$INTERVAL" =~ ^[0-9]+$ ]] || die "--interval must be an integer (seconds)"
[[ -f "$RUNNER" ]] || die "runner not found: $RUNNER"
chmod +x "$RUNNER" 2>/dev/null || true
mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR" "$(dirname "$STATE")"

say "rendering tick-watchdog plist:"
say "  label    = $LABEL"
say "  interval = ${INTERVAL}s (StartInterval — fires on wake if missed)"
say "  runner   = $PYTHON $RUNNER --host local"
say "  state    = $STATE"
say "  dry-mode = $DRY_MODE (BUBBLE_TICKWD_DRY_RUN)"
say "  plist    = $PLIST_PATH"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <!-- ONE watchdog for every host:local dept on this Mac (board #724).
         Discovers com.bubble.ops-loop-<slug>.plist at runtime; decides from
         the dept's transcript tail; re-kicks a stalled tick via the inject
         file; never touches a healthy-idle loop. -->
    <key>ProgramArguments</key>
    <array>
        <string>$(xml_escape "$PYTHON")</string>
        <string>$(xml_escape "$RUNNER")</string>
        <string>--host</string>
        <string>local</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$(xml_escape "$REPO_ROOT")</string>

    <!-- DOCTRINE: StartInterval so the check also fires on wake. -->
    <key>StartInterval</key>
    <integer>${INTERVAL}</integer>
    <key>RunAtLoad</key>
    <true/>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(xml_escape "$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")</string>
        <key>BUBBLE_TICKWD_STATE</key>
        <string>$(xml_escape "$STATE")</string>
        <key>BUBBLE_TICKWD_DRY_RUN</key>
        <string>${DRY_MODE}</string>
    </dict>

    <key>StandardOutPath</key>
    <string>$(xml_escape "${LOG_DIR%/}/${LABEL}.out.log")</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "${LOG_DIR%/}/${LABEL}.err.log")</string>
</dict>
</plist>
PLIST
say "wrote $PLIST_PATH"

if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$PLIST_PATH" >/dev/null 2>&1 && say "plutil -lint OK" || die "rendered plist failed plutil -lint: $PLIST_PATH"
fi

if [[ "$ACTIVATE" == "1" ]]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH" || die "launchctl load failed"
    say "ACTIVATED — $LABEL runs every ${INTERVAL}s (dry-mode=$DRY_MODE)."
else
    say "DRY RENDER complete (no launchctl). To activate: launchctl load '$PLIST_PATH'  # or re-run with --activate"
fi
