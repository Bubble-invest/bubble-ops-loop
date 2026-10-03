#!/usr/bin/env bash
# Install from the framework checkout; never copy libraries into departments.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
if [[ "$EUID" -ne 0 ]]; then
  echo "install-fleet-export-check: run as root" >&2
  exit 1
fi
if [[ "$REPO_ROOT" != /opt/bubble-ops-loop ]]; then
  echo "install-fleet-export-check: install from /opt/bubble-ops-loop" >&2
  exit 1
fi
python3 -I -c 'import yaml, zoneinfo; zoneinfo.ZoneInfo("Europe/Paris")'
install -m 0644 "$REPO_ROOT/deploy/templates/fleet-export-check.service" "$UNIT_DIR/fleet-export-check.service"
install -m 0644 "$REPO_ROOT/deploy/templates/fleet-export-check.timer" "$UNIT_DIR/fleet-export-check.timer"
"$SYSTEMCTL_BIN" daemon-reload
"$SYSTEMCTL_BIN" enable --now fleet-export-check.timer
echo "[install-fleet-export-check] installed; verify with scripts/fleet-export-check.sh --dry-run"
