#!/bin/bash
# install-mac-loop-deploy.sh — install the Mac auto-updater for the
# bubble-ops-loop FRAMEWORK clone itself (board #1477). Mac twin of the VPS
# bubble-deploy-infra.timer (OnCalendar=*:0/15) + bubble-deploy.sh --infra-only,
# rendered as a launchd StartInterval agent that runs bubble-deploy-mac.sh
# (ff-only, clean-tree-only, never executes anything from the pulled tree).
# Style twin of install-session-rotate-mac.sh: render-only unless --activate,
# vendors its runner into the same Application Support dir, plutil-lints the
# rendered plist. ONE instance per Mac (not per-slug) — this updates the
# shared framework checkout the Mac floor/wake-catch/session-rotate installers
# all run out of, not a per-dept clone.
#
# StartInterval (NOT StartCalendarInterval): launchd coalesces a fire missed
# while the Mac was asleep and runs it once on wake, same reasoning as the
# existing backup/wake-catch floors (see deploy/local/README.md).
#
# Usage: install-mac-loop-deploy.sh [--repo-dir DIR] [--support-dir DIR]
#                                    [--interval SECONDS] [--activate]
#        install-mac-loop-deploy.sh --uninstall [--activate]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
RUNNER_SRC="${here}/bubble-deploy-mac.sh"

repo_dir="$HOME/claude-workspaces/bubble-ops-loop"
support_dir="$HOME/Library/Application Support/bubble-ops-loop"
interval=900   # 15 min — mirrors VPS bubble-deploy-infra.timer's OnCalendar=*:0/15
activate=0
uninstall=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo-dir) repo_dir="$2"; shift 2 ;;
        --repo-dir=*) repo_dir="${1#--repo-dir=}"; shift ;;
        --support-dir) support_dir="$2"; shift 2 ;;
        --support-dir=*) support_dir="${1#--support-dir=}"; shift ;;
        --interval) interval="$2"; shift 2 ;;
        --interval=*) interval="${1#--interval=}"; shift ;;
        --activate) activate=1; shift ;;
        --uninstall) uninstall=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "usage: $0 [--repo-dir DIR] [--support-dir DIR] [--interval SECONDS] [--activate]" >&2; exit 2 ;;
    esac
done

label="com.bubble.mac-loop-deploy"
launch_agents_dir="$HOME/Library/LaunchAgents"
plist="${launch_agents_dir}/${label}.plist"
logdir="$HOME/Library/Logs/bubble-ops-loop"
mkdir -p "$launch_agents_dir" "$logdir"

if (( uninstall )); then
    if (( activate )); then
        launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
        echo "[install-mac-loop-deploy] launchctl bootout ${label}"
    fi
    if [[ -f "$plist" ]]; then
        rm -f "$plist"
        echo "[install-mac-loop-deploy] removed $plist"
    else
        echo "[install-mac-loop-deploy] no plist at $plist — nothing to remove"
    fi
    exit 0
fi

[[ -f "$RUNNER_SRC" ]] || { echo "FATAL: $RUNNER_SRC missing"; exit 2; }
[[ "$interval" =~ ^[0-9]+$ ]] || { echo "FATAL: --interval must be a positive integer"; exit 2; }

# Vendor the runner into the stable support dir (twin of /opt on the VPS; the
# same dir install-session-rotate-mac.sh already vendors the rotate script
# into) so the plist references a stable path, not this checkout's location.
mkdir -p "$support_dir"
install -m 0755 "$RUNNER_SRC" "$support_dir/bubble-deploy-mac.sh"
runner="$support_dir/bubble-deploy-mac.sh"

cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${runner}</string>
        <string>--repo-dir</string>
        <string>${repo_dir}</string>
        <string>--support-dir</string>
        <string>${support_dir}</string>
    </array>
    <key>StartInterval</key><integer>${interval}</integer>
    <key>RunAtLoad</key><true/>
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
echo "[install-mac-loop-deploy] wrote $plist (runner=$runner, repo-dir=$repo_dir, interval=${interval}s)"

if (( activate )); then
  launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "[install-mac-loop-deploy] ACTIVATED ${label}"
else
  echo "[install-mac-loop-deploy] rendered only (re-run with --activate to load)"
fi
