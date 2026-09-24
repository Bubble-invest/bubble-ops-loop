#!/usr/bin/env bash
# install-decision-signing-key.sh — install the boot-time decrypt oneshot for
# the cockpit's static Ed25519 decision-signing PRIVATE key (board #1498,
# follow-up to board #1476 / PR #492's deploy runbook).
#
# RENDER-ONLY BY DEFAULT: prints exactly what it would install/run and
# writes nothing to disk, touches no systemd state. Pass --activate to
# actually install the files and enable+start the unit. This is stricter
# than console/deploy/cockpit-approver/install-cockpit-approver-refresh.sh's
# default (which installs unless --dry-run is passed) — a root-owned
# systemd unit that decrypts a private key onto tmpfs should require an
# explicit opt-in step, not merely the absence of a flag, especially since
# this script can be run from a fresh `git pull`/review pass without an
# operator meaning to touch the box yet.
#
# WHAT IT INSTALLS (only with --activate)
#   /usr/local/bin/bubble-cockpit-decision-key-decrypt.sh    (root:root, 0750)
#   /etc/systemd/system/bubble-cockpit-decision-key.service  (0644)
#   then `systemctl daemon-reload` + `enable --now` the unit — which RUNS the
#   decrypt oneshot immediately (it's a oneshot with `WantedBy=multi-user.target`,
#   not a timer; `enable --now` both arms it for future boots and fires it now).
#
# WHAT IT DOES NOT DO
#   - Does not place, fetch, or decrypt the SOPS-encrypted private key file
#     itself — board #1498 step 1 already dropped it at
#     /srv/bubble-secrets/decision-signing-ed25519.private-key.sops.pem
#     (root:root, 0440). If it's missing when the unit runs, the decrypt
#     script fails closed with a clear message (see its own header) — it
#     does NOT take bubble-ops-console down (that already degrades to
#     unsigned/DECISION_SIGNATURES=warn without this key).
#   - Does not modify bubble-ops-console.service or its drop-ins — ordering
#     is one-way (`Before=bubble-ops-console.service` inside the unit
#     shipped here); no drop-in is written onto the live console unit.
#   - Does not print, log, or decrypt any key material itself.
#   - Does not run the smoke tests (see README.md's "Smoke tests" section —
#     run those by hand after activating).
#
# Usage (on the box, as root):
#   bash console/deploy/decision-signing-key/install-decision-signing-key.sh              # render-only (default)
#   bash console/deploy/decision-signing-key/install-decision-signing-key.sh --activate    # actually install + enable + start
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${BUBBLE_DECISION_KEY_BIN_DIR:-/usr/local/bin}"
SYSTEMD_DIR="${BUBBLE_DECISION_KEY_SYSTEMD_DIR:-/etc/systemd/system}"
SCRIPT_NAME=bubble-cockpit-decision-key-decrypt.sh
SERVICE_NAME=bubble-cockpit-decision-key.service

ACTIVATE=0
for a in "$@"; do
    case "$a" in
        --activate) ACTIVATE=1 ;;
        --help|-h)
            sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "usage: $0 [--activate]" >&2; exit 64 ;;
    esac
done

say() { echo "[install-decision-signing-key] $*"; }
run() {
    if [[ "$ACTIVATE" == "1" ]]; then
        eval "$*"
    else
        echo "  RENDER: $*"
    fi
}

for f in "$SCRIPT_NAME" "$SERVICE_NAME"; do
    [[ -f "$SCRIPT_DIR/$f" ]] || { echo "ERR: missing $SCRIPT_DIR/$f" >&2; exit 2; }
done

if [[ "$ACTIVATE" != "1" ]]; then
    say "render-only (default, no --activate) — nothing will be written."
fi

say "installing decrypt script -> $BIN_DIR (root:root, 0750)"
run "install -o root -g root -m 0750 '$SCRIPT_DIR/$SCRIPT_NAME' '$BIN_DIR/$SCRIPT_NAME'"

say "installing unit -> $SYSTEMD_DIR (0644)"
run "install -o root -g root -m 0644 '$SCRIPT_DIR/$SERVICE_NAME' '$SYSTEMD_DIR/$SERVICE_NAME'"

run "systemctl daemon-reload"
say "enabling + starting $SERVICE_NAME (this RUNS the decrypt oneshot now, not just arms it)"
run "systemctl enable --now '$SERVICE_NAME'"

say "done."
if [[ "$ACTIVATE" != "1" ]]; then
    say "this was a RENDER-ONLY run — nothing was written or started."
    say "re-run with --activate on the VPS, as root, to install for real."
else
    say "verify (README.md 'Smoke tests'):"
    say "  test -s /run/bubble-cockpit-decision-key/key"
    say "  sudo -u bubble-console test -r /run/bubble-cockpit-decision-key/key"
    say "  sudo -u claude test -r /run/bubble-cockpit-decision-key/key && echo BAD-READABLE || echo 'OK - claude denied'"
    say "then restart bubble-ops-console.service so any in-flight process picks up signing."
fi
