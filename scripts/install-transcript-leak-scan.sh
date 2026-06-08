#!/usr/bin/env bash
# install-transcript-leak-scan.sh — deploy transcript-leak-scan timer + service
# Idempotent: safe to re-run. Part of bubble-ops-loop install manifest.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$REPO_ROOT/deploy"
UNIT_DIR="/etc/systemd/system"
SCRIPT_DEST="/home/claude/scripts/transcript-leak-scan.sh"

echo "[install-transcript-leak-scan] deploying units..."
mkdir -p /home/claude/scripts
sudo cp "$DEPLOY/templates/transcript-leak-scan.service" "$UNIT_DIR/transcript-leak-scan.service" 2>/dev/null || true
sudo cp "$DEPLOY/templates/transcript-leak-scan.timer" "$UNIT_DIR/transcript-leak-scan.timer" 2>/dev/null || true
sudo cp "$DEPLOY/templates/transcript-leak-scan.sh" "$SCRIPT_DEST" 2>/dev/null || true
sudo chmod 0755 "$SCRIPT_DEST" 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable --now transcript-leak-scan.timer 2>/dev/null || true
echo "[install-transcript-leak-scan] done"
