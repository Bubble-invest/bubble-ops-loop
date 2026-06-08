#!/usr/bin/env bash
# install-cache-sync.sh — deploy bubble-cache-sync timer + service
# Idempotent: safe to re-run. Part of bubble-ops-loop install manifest.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$REPO_ROOT/deploy"
UNIT_DIR="/etc/systemd/system"
SCRIPT_DEST="/usr/local/bin/bubble-cache-sync.sh"

echo "[install-cache-sync] deploying units..."
sudo cp "$DEPLOY/templates/bubble-cache-sync.service" "$UNIT_DIR/bubble-cache-sync.service" 2>/dev/null || true
sudo cp "$DEPLOY/templates/bubble-cache-sync.timer" "$UNIT_DIR/bubble-cache-sync.timer" 2>/dev/null || true
sudo cp "$DEPLOY/templates/bubble-cache-sync.sh" "$SCRIPT_DEST" 2>/dev/null || true
sudo chmod 0755 "$SCRIPT_DEST" 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable --now bubble-cache-sync.timer 2>/dev/null || true
echo "[install-cache-sync] done"
