#!/usr/bin/env bash
# Idempotently install from the framework checkout, like fleet-export-check.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
if [[ "$EUID" -ne 0 ]]; then
  echo "install-fleet-cost-summary: run as root" >&2
  exit 1
fi
if [[ "$REPO_ROOT" != /opt/bubble-ops-loop ]]; then
  echo "install-fleet-cost-summary: install from /opt/bubble-ops-loop" >&2
  exit 1
fi
python3 -I -c 'import sqlite3, zoneinfo; zoneinfo.ZoneInfo("Europe/Paris")'
# Refuse symlinked destination components; establish traversable directories.
python3 -I -c 'import sys; from pathlib import Path; sys.path.insert(0, "/opt/bubble-ops-loop/console/services"); from cost_io import atomic_json, public_directory; atomic_json(Path("/var/lib/bubble-fleet/costs/.install-check.json"), {}); Path("/var/lib/bubble-fleet/costs/.install-check.json").unlink(); public_directory(Path("/var/lib/bubble-fleet")); public_directory(Path("/var/lib/bubble-fleet/costs"))'
for unit in hermes-usage-export.service hermes-usage-export.timer fleet-cost-summary.service fleet-cost-summary.timer; do
  install -m 0644 "$REPO_ROOT/deploy/templates/$unit" "$UNIT_DIR/$unit"
done
"$SYSTEMCTL_BIN" daemon-reload
"$SYSTEMCTL_BIN" enable --now hermes-usage-export.timer fleet-cost-summary.timer
"$SYSTEMCTL_BIN" start fleet-cost-summary.service
echo "[install-fleet-cost-summary] installed; Tony reads /var/lib/bubble-fleet/costs/latest.json"
