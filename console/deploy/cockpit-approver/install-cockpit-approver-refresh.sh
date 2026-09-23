#!/usr/bin/env bash
# install-cockpit-approver-refresh.sh — install the cockpit-approver token
# refresh timer on the VPS (board #1432, C2 follow-up).
#
# 1:1 mirror of console/deploy/contents-token/README.md's install recipe —
# same file layout, same perms, same systemd install/enable sequence — just
# for the cockpit-approver App instead of bubble-ops-bot. Idempotent: `install`
# only rewrites a destination file when its content differs, so re-running
# this on every deploy is a no-op once the units match. Encodes that README's
# manual steps as a script instead of leaving them to be typed by hand, same
# as scripts/install-loop-tick-watchdog.sh does for its own unit pair.
#
# WHAT IT INSTALLS
#   /usr/local/bin/bubble-cockpit-approver-token.sh          (root:root, 0750)
#   /usr/local/bin/bubble-cockpit-approver-token-refresh.sh  (root:root, 0750)
#   /etc/systemd/system/bubble-cockpit-approver-token-refresh.service (0644)
#   /etc/systemd/system/bubble-cockpit-approver-token-refresh.timer  (0644)
#   then `systemctl daemon-reload` + `enable --now` the timer.
#
# WHAT IT DOES NOT DO (by design — read README.md before relying on this)
#   - Does not place, fetch, or decrypt the App .pem. Until Joris drops it at
#     /srv/bubble-secrets/github-app-cockpit-approver.private-key.sops.pem,
#     the minter — and so the refresh timer — fails closed ("private key not
#     provisioned"); pr_approver.py reports "not_provisioned", no false approval.
#   - Does not grant any sudo/sudoers rule — there is nothing to grant. The
#     console (`claude`, NoNewPrivileges=true) only ever READS the tmpfs
#     token file this timer writes; it never invokes the minter itself.
#   - Does not start the one-shot mint immediately by default (pass --mint-now
#     to also run `systemctl start` once you've placed the key) and does not
#     smoke-test the result (see README.md step 3 for that).
#
# Usage (on the box, as a sudoer — mirrors install-loop-tick-watchdog.sh):
#   bash console/deploy/cockpit-approver/install-cockpit-approver-refresh.sh
#   bash console/deploy/cockpit-approver/install-cockpit-approver-refresh.sh --dry-run
#   bash console/deploy/cockpit-approver/install-cockpit-approver-refresh.sh --mint-now
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${BUBBLE_APPROVER_BIN_DIR:-/usr/local/bin}"
SYSTEMD_DIR="${BUBBLE_APPROVER_SYSTEMD_DIR:-/etc/systemd/system}"
TIMER_NAME=bubble-cockpit-approver-token-refresh.timer
SERVICE_NAME=bubble-cockpit-approver-token-refresh.service

DRY=0
MINT_NOW=0
for a in "$@"; do
    case "$a" in
        --dry-run) DRY=1 ;;
        --mint-now) MINT_NOW=1 ;;
        --help|-h)
            sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "usage: $0 [--dry-run] [--mint-now]" >&2; exit 64 ;;
    esac
done

say() { echo "[install-cockpit-approver-refresh] $*"; }
run() { if [[ "$DRY" == "1" ]]; then echo "  DRY: $*"; else eval "$*"; fi; }

for f in bubble-cockpit-approver-token.sh bubble-cockpit-approver-token-refresh.sh \
         bubble-cockpit-approver-token-refresh.service bubble-cockpit-approver-token-refresh.timer; do
    [[ -f "$SCRIPT_DIR/$f" ]] || { echo "ERR: missing $SCRIPT_DIR/$f" >&2; exit 2; }
done

say "installing minter + refresher -> $BIN_DIR (root:root, 0750)"
run "install -o root -g root -m 0750 '$SCRIPT_DIR/bubble-cockpit-approver-token.sh' '$BIN_DIR/bubble-cockpit-approver-token.sh'"
run "install -o root -g root -m 0750 '$SCRIPT_DIR/bubble-cockpit-approver-token-refresh.sh' '$BIN_DIR/bubble-cockpit-approver-token-refresh.sh'"

say "installing unit + timer -> $SYSTEMD_DIR (0644)"
run "install -o root -g root -m 0644 '$SCRIPT_DIR/$SERVICE_NAME' '$SYSTEMD_DIR/$SERVICE_NAME'"
run "install -o root -g root -m 0644 '$SCRIPT_DIR/$TIMER_NAME' '$SYSTEMD_DIR/$TIMER_NAME'"

run "systemctl daemon-reload"
say "enabling + starting $TIMER_NAME"
run "systemctl enable --now '$TIMER_NAME'"

if [[ "$MINT_NOW" == "1" ]]; then
    say "--mint-now: firing $SERVICE_NAME once immediately"
    run "systemctl start '$SERVICE_NAME'"
fi

say "done."
say "NOT done (deliberately, see this script's header + README.md):"
say "  - the App private key is NOT provisioned by this script"
say "  - no sudoers grant is needed (the console only READS the token file)"
if [[ "$MINT_NOW" != "1" ]]; then
    say "  - no mint has run yet — pass --mint-now once the key is dropped, or wait for OnBootSec=30s"
fi
say "verify (after the key is dropped): test -s /run/bubble-cockpit-approver/token && sudo -u claude test -r /run/bubble-cockpit-approver/token"
say "next: drop the .pem (SOPS), then follow README.md's remaining steps."
