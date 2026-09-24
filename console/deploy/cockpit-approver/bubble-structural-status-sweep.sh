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
# Runs as the `bubble-console` user — NOT root, NOT `claude` (board #1463: the
# whole point of the uid split is that the general-purpose `claude` uid, which
# also runs cloud-wiki-compile's agentic `claude -p` session, must NOT be able
# to read the App-signed approval token or post a status as this App — a sweep
# running as `claude` would defeat #1462/#1463 outright). It only READS the
# tmpfs token file `/run/bubble-cockpit-approver/token` (0640 root:bubble-console)
# that the root-owned refresh timer writes; it never touches the App's private
# key. Reads from the same root-owned, read-only infra clone the console itself
# runs from (/opt/bubble-ops-loop), not the claude-writable checkout.
set -euo pipefail

REPO_ROOT="${BUBBLE_OPS_LOOP_ROOT:-/opt/bubble-ops-loop}"
PYTHON="${BUBBLE_OPS_LOOP_PYTHON:-$REPO_ROOT/venv/bin/python}"

cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m console.scripts.structural_status_sweep
