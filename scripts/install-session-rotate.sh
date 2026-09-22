#!/usr/bin/env bash
# install-session-rotate.sh — deploy the daily fresh-session rotation (board #1195).
# Idempotent: safe to re-run. Part of the bubble-ops-loop install manifest.
#
# WHY: a dept in --continue re-parses its whole on-disk transcript every restart;
# auto-compaction bounds only the in-memory context per turn, NOT the .jsonl, so a
# forever-session eventually overflows the window and wedges the agent ("Prompt is
# too long" — maya @12MB, ben @16.7MB, 2026-09-21). bubble-session-rotate.sh rotates
# a dept to a FRESH session once per day; the L4 `session_handoff` mission wrote
# HANDOFF.md so the fresh session's first tick recovers context (see the script
# header + wiki claude-session-context-overflow).
#
# This installs the rotation SCRIPT + the templated @.service/@.timer, but does NOT
# enable any instance. Enabling is a per-dept, explicit follow-up (staged rollout —
# prototype on tony first, then VPS depts, then Macs, then ben last), and requires
# the dept to carry the session_handoff L4 mission first (else the fail-safe SKIPs):
#   sudo systemctl enable --now bubble-session-rotate@<dept>.timer
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$REPO_ROOT/deploy"
UNIT_DIR="/etc/systemd/system"
SCRIPT_DIR="/opt/bubble-ops-loop/scripts"

echo "[install-session-rotate] installing rotation script -> $SCRIPT_DIR ..."
sudo install -d -m 0755 "$SCRIPT_DIR"
# On the VPS the repo checkout IS /opt/bubble-ops-loop, so src==dst — the script is
# already in place via git. `install` errors on identical paths, so skip the copy then.
SRC_SCRIPT="$REPO_ROOT/scripts/bubble-session-rotate.sh"
DST_SCRIPT="$SCRIPT_DIR/bubble-session-rotate.sh"
if [[ "$(readlink -f "$SRC_SCRIPT")" != "$(readlink -f "$DST_SCRIPT" 2>/dev/null || echo /nonexistent)" ]]; then
  sudo install -m 0755 "$SRC_SCRIPT" "$DST_SCRIPT"
else
  echo "  (repo checkout is the /opt target — rotation script already in place, skipping self-copy)"
  sudo chmod 0755 "$DST_SCRIPT"
fi

echo "[install-session-rotate] installing units ..."
sudo cp "$DEPLOY/templates/bubble-session-rotate@.service" "$UNIT_DIR/bubble-session-rotate@.service"
sudo cp "$DEPLOY/templates/bubble-session-rotate@.timer"   "$UNIT_DIR/bubble-session-rotate@.timer"
sudo systemctl daemon-reload
echo "[install-session-rotate] done — per-dept bubble-session-rotate@<dept>.timer installed but NOT enabled."
echo "                          enable per dept (staged): sudo systemctl enable --now bubble-session-rotate@<dept>.timer"
