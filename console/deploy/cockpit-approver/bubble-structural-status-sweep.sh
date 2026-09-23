#!/usr/bin/env bash
# bubble-structural-status-sweep.sh — run the periodic `structural-approval`
# status sweep (board #1462, console/scripts/structural_status_sweep.py).
#
# Thin wrapper (mirrors the shape of bubble-cockpit-approver-token-refresh.sh
# without its retry/backoff logic — this script does no secret decryption; a
# failed sweep tick is harmless, the next tick 2 minutes later just re-runs
# it) so the systemd unit has a fixed, auditable ExecStart with the right
# venv + PYTHONPATH, exactly like the console's own unit
# (bubble-ops-console.service.template).
#
# Runs as the `claude` user — NOT root. It only READS the tmpfs token file
# `/run/bubble-cockpit-approver/token` (0640 root:claude) that the root-owned
# refresh timer writes; it never touches the App's private key.
set -euo pipefail

REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-/home/claude/bubble-ops-loop}"
PYTHON="${BUBBLE_OPS_LOOP_PYTHON:-$REPO_ROOT/venv/bin/python}"

cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m console.scripts.structural_status_sweep
