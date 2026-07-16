#!/usr/bin/env bash
# install-deploy-freshness.sh — deploy the deploy-freshness watchdog (script +
# service + timer). Idempotent: safe to re-run. Part of bubble-ops-loop install
# manifest (mirrors install-secrets-sweep.sh's shape).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$REPO_ROOT/deploy"
UNIT_DIR="/etc/systemd/system"
SCRIPT_DEST="/home/claude/scripts/deploy-freshness.sh"

echo "[install-deploy-freshness] deploying units..."
mkdir -p /home/claude/scripts
sudo cp "$DEPLOY/templates/deploy-freshness.service" "$UNIT_DIR/deploy-freshness.service"
sudo cp "$DEPLOY/templates/deploy-freshness.timer" "$UNIT_DIR/deploy-freshness.timer"
sudo cp "$DEPLOY/templates/deploy-freshness.sh" "$SCRIPT_DEST"
sudo chmod 0755 "$SCRIPT_DEST"
sudo systemctl daemon-reload
sudo systemctl enable --now deploy-freshness.timer
echo "[install-deploy-freshness] done"
