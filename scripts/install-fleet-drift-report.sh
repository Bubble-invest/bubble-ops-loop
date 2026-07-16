#!/usr/bin/env bash
# install-fleet-drift-report.sh — deploy the fleet-drift-report watchdog
# (script + service + timer). Idempotent: safe to re-run. Part of
# bubble-ops-loop install manifest (mirrors install-secrets-sweep.sh's shape).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$REPO_ROOT/deploy"
UNIT_DIR="/etc/systemd/system"
SCRIPT_DEST="/home/claude/scripts/fleet-drift-report.sh"

echo "[install-fleet-drift-report] deploying units..."
mkdir -p /home/claude/scripts
sudo cp "$DEPLOY/templates/fleet-drift-report.service" "$UNIT_DIR/fleet-drift-report.service"
sudo cp "$DEPLOY/templates/fleet-drift-report.timer" "$UNIT_DIR/fleet-drift-report.timer"
sudo cp "$DEPLOY/templates/fleet-drift-report.sh" "$SCRIPT_DEST"
sudo chmod 0755 "$SCRIPT_DEST"
sudo systemctl daemon-reload
sudo systemctl enable --now fleet-drift-report.timer
echo "[install-fleet-drift-report] done"
echo "[install-fleet-drift-report] dept agents can read /run/bubble-fleet/drift.json at loop-start"
