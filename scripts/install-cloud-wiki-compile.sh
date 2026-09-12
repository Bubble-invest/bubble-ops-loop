#!/usr/bin/env bash
# install-cloud-wiki-compile.sh — deploy the cloud-wiki-compile skill + units to the VPS.
# Idempotent: safe to re-run. Part of bubble-ops-loop install manifest.
#
# Installs: the launcher script, the memory-hygiene notifier (invoked by the
# pruning step), the SKILL, the templated service, and the three timers (compile
# nightly, synthesis + pruning weekly).
#
# Run ON the VPS (joris-cx33) as a user with sudo (typically `claude`).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILL_SRC="$REPO_ROOT/skills/cloud-wiki-compile"
DEPLOY="$REPO_ROOT/deploy"
UNIT_DIR="/etc/systemd/system"

SCRIPT_DST=/home/claude/scripts/cloud-wiki-compile.sh
SKILL_DST=/home/claude/.claude/skills/cloud-wiki-compile/SKILL.md
# The pruning step invokes this notifier by absolute path (see SKILL.md). It was
# previously an untracked, hand-deployed orphan under /home/claude/scripts — which
# is how the #874 path-fix drifted out of version control and could not be
# re-verified. Deploying it from the repo keeps the fix + #1223 WORKING_MEMORY.md
# coverage under source control.
MEM_HYGIENE_DST=/home/claude/scripts/memory_hygiene_notify.py

echo "[1/6] launcher script -> $SCRIPT_DST"
install -m 0755 "$SKILL_SRC/scripts/cloud-wiki-compile.sh" "$SCRIPT_DST"

echo "[2/6] memory-hygiene notifier -> $MEM_HYGIENE_DST"
install -m 0755 "$SKILL_SRC/scripts/memory_hygiene_notify.py" "$MEM_HYGIENE_DST"

echo "[3/6] SKILL -> $SKILL_DST"
install -d -m 0755 "$(dirname "$SKILL_DST")"
install -m 0644 "$SKILL_SRC/SKILL.md" "$SKILL_DST"

echo "[4/6] systemd units -> $UNIT_DIR (needs sudo)"
sudo install -m 0644 "$DEPLOY/templates/cloud-wiki-compile@.service"       "$UNIT_DIR/cloud-wiki-compile@.service"
sudo install -m 0644 "$DEPLOY/templates/cloud-wiki-compile-compile.timer"   "$UNIT_DIR/cloud-wiki-compile-compile.timer"
sudo install -m 0644 "$DEPLOY/templates/cloud-wiki-compile-synthesis.timer" "$UNIT_DIR/cloud-wiki-compile-synthesis.timer"
sudo install -m 0644 "$DEPLOY/templates/cloud-wiki-compile-pruning.timer"   "$UNIT_DIR/cloud-wiki-compile-pruning.timer"

echo "[5/6] daemon-reload + enable timers"
sudo systemctl daemon-reload
sudo systemctl enable --now cloud-wiki-compile-compile.timer
sudo systemctl enable --now cloud-wiki-compile-synthesis.timer
sudo systemctl enable --now cloud-wiki-compile-pruning.timer

echo "[6/6] done. Timers:"
systemctl list-timers --all --no-pager | grep cloud-wiki-compile || true
echo
echo "Manual smoke test (one compile now):"
echo "  sudo systemctl start cloud-wiki-compile@compile.service"
echo "  journalctl -u cloud-wiki-compile@compile.service -f"
