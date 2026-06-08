#!/usr/bin/env bash
# install-secrets-sweep.sh — deploy secrets-tmp-sweep timer + service
# Idempotent: safe to re-run. Part of bubble-ops-loop install manifest.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$REPO_ROOT/deploy"
UNIT_DIR="/etc/systemd/system"
SCRIPT_DEST="/home/claude/scripts/secrets-tmp-sweep.sh"

echo "[install-secrets-sweep] deploying units..."
mkdir -p /home/claude/scripts
sudo cp "$DEPLOY/templates/secrets-tmp-sweep.service" "$UNIT_DIR/secrets-tmp-sweep.service" 2>/dev/null || true
sudo cp "$DEPLOY/templates/secrets-tmp-sweep.timer" "$UNIT_DIR/secrets-tmp-sweep.timer" 2>/dev/null || true
sudo cp "$DEPLOY/templates/secrets-tmp-sweep.sh" "$SCRIPT_DEST" 2>/dev/null || true
sudo chmod 0755 "$SCRIPT_DEST" 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable --now secrets-tmp-sweep.timer 2>/dev/null || true
echo "[install-secrets-sweep] done"
