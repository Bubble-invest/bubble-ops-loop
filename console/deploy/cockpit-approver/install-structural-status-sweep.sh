#!/usr/bin/env bash
# install-structural-status-sweep.sh — install the periodic structural-approval
# status sweep timer on the VPS (board #1462).
#
# Separate installer from install-cockpit-approver-refresh.sh on purpose: that
# one installs ROOT-ONLY pieces (the minter holds the SOPS-decrypt path to the
# App's private key, root:root 0750 — must never be bubble-console-executable).
# This one installs a `bubble-console`-run wrapper (it only calls the GitHub
# API with an already-minted token) — different owning group, different unit
# `User=`, so keeping them as two small scripts avoids one install script
# quietly needing two different trust levels.
#
# Board #1463: runs as the dedicated `bubble-console` uid, NOT `claude` — the
# whole point of #1463's isolation is that `claude` (which also runs
# cloud-wiki-compile's agentic session) cannot read the approver token; a
# sweep running as `claude` would reopen that hole. Mirrors the console unit's
# own uid + WorkingDirectory (/opt/bubble-ops-loop, root-owned read-only infra
# clone — see console/deploy/bubble-ops-console.service.template).
#
# WHAT IT INSTALLS
#   /usr/local/bin/bubble-structural-status-sweep.sh (root:bubble-console, 0750)
#   /etc/systemd/system/bubble-structural-status-sweep.service (0644, User=bubble-console)
#   /etc/systemd/system/bubble-structural-status-sweep.timer  (0644)
#   then `systemctl daemon-reload` + `enable --now` the timer.
#
# WHAT IT DOES NOT DO
#   - Does not touch the App private key or the token minter/refresh timer —
#     those are install-cockpit-approver-refresh.sh's job, run that FIRST (the
#     sweep is a no-op, fail-closed, until the token file it reads exists).
#   - Does not start the one-shot sweep immediately by default (pass
#     --run-now to also run `systemctl start` once).
#
# Usage (on the box, as a sudoer):
#   bash console/deploy/cockpit-approver/install-structural-status-sweep.sh
#   bash console/deploy/cockpit-approver/install-structural-status-sweep.sh --dry-run
#   bash console/deploy/cockpit-approver/install-structural-status-sweep.sh --run-now
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${BUBBLE_APPROVER_BIN_DIR:-/usr/local/bin}"
SYSTEMD_DIR="${BUBBLE_APPROVER_SYSTEMD_DIR:-/etc/systemd/system}"
TIMER_NAME=bubble-structural-status-sweep.timer
SERVICE_NAME=bubble-structural-status-sweep.service
WRAPPER_NAME=bubble-structural-status-sweep.sh

DRY=0
RUN_NOW=0
for a in "$@"; do
    case "$a" in
        --dry-run) DRY=1 ;;
        --run-now) RUN_NOW=1 ;;
        --help|-h)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "usage: $0 [--dry-run] [--run-now]" >&2; exit 64 ;;
    esac
done

say() { echo "[install-structural-status-sweep] $*"; }
run() { if [[ "$DRY" == "1" ]]; then echo "  DRY: $*"; else eval "$*"; fi; }

for f in "$WRAPPER_NAME" "$SERVICE_NAME" "$TIMER_NAME"; do
    [[ -f "$SCRIPT_DIR/$f" ]] || { echo "ERR: missing $SCRIPT_DIR/$f" >&2; exit 2; }
done

say "installing wrapper -> $BIN_DIR (root:bubble-console, 0750)"
run "install -o root -g bubble-console -m 0750 '$SCRIPT_DIR/$WRAPPER_NAME' '$BIN_DIR/$WRAPPER_NAME'"

say "installing unit + timer -> $SYSTEMD_DIR (0644)"
run "install -o root -g root -m 0644 '$SCRIPT_DIR/$SERVICE_NAME' '$SYSTEMD_DIR/$SERVICE_NAME'"
run "install -o root -g root -m 0644 '$SCRIPT_DIR/$TIMER_NAME' '$SYSTEMD_DIR/$TIMER_NAME'"

run "systemctl daemon-reload"
say "enabling + starting $TIMER_NAME"
run "systemctl enable --now '$TIMER_NAME'"

if [[ "$RUN_NOW" == "1" ]]; then
    say "--run-now: firing $SERVICE_NAME once immediately"
    run "systemctl start '$SERVICE_NAME'"
fi

say "done."
say "reminder: this sweep is a no-op until install-cockpit-approver-refresh.sh's"
say "token file exists (/run/bubble-cockpit-approver/token) AND the App has"
say "'Commit statuses: write' accepted on the installation (Joris, board #1462)."
say "verify: journalctl -u $SERVICE_NAME -n 40 --no-pager"
