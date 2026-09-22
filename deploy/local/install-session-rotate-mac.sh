#!/bin/bash
# install-session-rotate-mac.sh — install the daily fresh-session rotation (#1195)
# for a MAC dept. Renders a launchd StartCalendarInterval agent that runs
# bubble-session-rotate-mac.sh at 07:30 local, and (with --activate) loads it.
# Idempotent. Mac twin of scripts/install-session-rotate.sh (VPS).
#
# Usage: install-session-rotate-mac.sh <slug> [--workdir DIR] [--hour H] [--minute M] [--activate]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ROTATE_SRC="${here}/bubble-session-rotate-mac.sh"

slug="${1:?usage: install-session-rotate-mac.sh <slug> [--workdir DIR] [--hour H] [--minute M] [--activate]}"; shift || true
workdir=""; hour=7; minute=30; activate=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workdir) workdir="$2"; shift 2 ;;
    --hour)    hour="$2"; shift 2 ;;
    --minute)  minute="$2"; shift 2 ;;
    --activate) activate=1; shift ;;
    *) shift ;;
  esac
done
[[ -n "$workdir" ]] || workdir="$HOME/claude-workspaces/bubble-ops-${slug}"
[[ -f "$ROTATE_SRC" ]] || { echo "FATAL: $ROTATE_SRC missing"; exit 2; }

# Install the rotation script into a stable support dir (twin of /opt on the VPS).
SUPPORT="$HOME/Library/Application Support/bubble-ops-loop"
mkdir -p "$SUPPORT"
install -m 0755 "$ROTATE_SRC" "$SUPPORT/bubble-session-rotate-mac.sh"
ROTATE="$SUPPORT/bubble-session-rotate-mac.sh"

label="com.bubble.session-rotate-${slug}"
plist="$HOME/Library/LaunchAgents/${label}.plist"
logdir="$HOME/Library/Logs/bubble-ops-loop"; mkdir -p "$logdir"

cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${ROTATE}</string>
        <string>${slug}</string>
        <string>--workdir</string>
        <string>${workdir}</string>
    </array>
    <!-- Daily fresh-session rotation (board #1195). NOT KeepAlive — a one-shot
         per-day job. StartCalendarInterval fires at ${hour}:$(printf '%02d' "$minute") local;
         if the Mac was asleep, launchd runs it on the next wake. -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>${hour}</integer>
        <key>Minute</key><integer>${minute}</integer>
    </dict>
    <key>StandardOutPath</key><string>${logdir}/${label}.out.log</string>
    <key>StandardErrorPath</key><string>${logdir}/${label}.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$plist" >/dev/null || { echo "FATAL: rendered plist failed plutil -lint"; exit 3; }
fi
echo "[install-session-rotate-mac] wrote $plist (rotate=$ROTATE, ${hour}:$(printf '%02d' "$minute") local)"

if (( activate )); then
  launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "[install-session-rotate-mac] ACTIVATED ${label}"
else
  echo "[install-session-rotate-mac] rendered only (re-run with --activate to load)"
fi
