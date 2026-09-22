#!/usr/bin/env bash
# install-cockpit-approver-minter.sh — install the cockpit-approver token
# minter + its scoped sudoers grant on the VPS (board #1432, option C2).
#
# Idempotent: safe to re-run on every deploy (mirrors
# scripts/install-loop-tick-watchdog.sh's install-then-diff shape). Installs
# ONLY the minter binary + the sudoers drop-in — it does NOT touch the App
# private key in any way (no fetch, no decrypt, no reference to the key's
# contents). That step stays a separate, explicit operator action (see
# README.md's "One-time operator step").
#
# WHAT IT INSTALLS
#   /usr/local/bin/bubble-cockpit-approver-token.sh   (root:root, 0750)
#   /etc/sudoers.d/bubble-cockpit-approver             (root:root, 0440)
#     grants: claude ALL=(root) NOPASSWD: /usr/local/bin/bubble-cockpit-approver-token.sh
#     — the exact scoped-grant convention already used for the settings_pr
#     broker-mint wrapper (deploy/templates/bubble-broker-mint.sudoers); see
#     that file's own header for why THIS shape (one absolute binary, no
#     wildcard args, no env passthrough) and not a broader grant.
#
# WHAT IT DOES NOT DO (by design — read the README before relying on this)
#   - Does not place, fetch, or decrypt the App .pem. Until Joris drops it at
#     /srv/bubble-secrets/github-app-cockpit-approver.private-key.sops.pem,
#     the minter fails closed ("private key not provisioned") and
#     pr_approver.py reports "not_provisioned" — no false approval either way.
#   - Does not run the minter or smoke-test it (see README step 3 for that).
#   - Does not resolve the NoNewPrivileges=true vs `sudo -n` conflict flagged
#     in deploy/templates/bubble-cockpit-approver.sudoers's header comment —
#     that needs a decision (flip NoNewPrivileges for this unit, or migrate
#     pr_approver.py to the contents-token timer+tmpfs pattern) before the
#     grant this script installs can actually be exercised in production.
#
# Usage (on the box, as a sudoer — mirrors install-loop-tick-watchdog.sh):
#   bash console/deploy/cockpit-approver/install-cockpit-approver-minter.sh
#   bash console/deploy/cockpit-approver/install-cockpit-approver-minter.sh --dry-run
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

BIN_SRC="$SCRIPT_DIR/bubble-cockpit-approver-token.sh"
BIN_DEST="${BUBBLE_APPROVER_BIN_DEST:-/usr/local/bin/bubble-cockpit-approver-token.sh}"
SUDOERS_SRC="$PROJECT_ROOT/deploy/templates/bubble-cockpit-approver.sudoers"
SUDOERS_DEST="${BUBBLE_APPROVER_SUDOERS_DEST:-/etc/sudoers.d/bubble-cockpit-approver}"
VISUDO_BIN="${BUBBLE_APPROVER_VISUDO:-visudo}"

DRY=0
for a in "$@"; do
    case "$a" in
        --dry-run) DRY=1 ;;
        --help|-h)
            sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "usage: $0 [--dry-run]" >&2; exit 64 ;;
    esac
done

say() { echo "[install-cockpit-approver-minter] $*"; }
run() { if [[ "$DRY" == "1" ]]; then echo "  DRY: $*"; else eval "$*"; fi; }

[[ -f "$BIN_SRC" ]] || { echo "ERR: minter source missing at $BIN_SRC" >&2; exit 2; }
[[ -f "$SUDOERS_SRC" ]] || { echo "ERR: sudoers template missing at $SUDOERS_SRC" >&2; exit 2; }

# Validate the sudoers fragment BEFORE installing it — a malformed drop-in in
# /etc/sudoers.d/ can break sudo for every user on the box. `visudo -cf` checks
# syntax without installing (same guard scripts/dept-cutover.sh and the
# broker-mint docs already call for on this exact file class).
if command -v "$VISUDO_BIN" >/dev/null 2>&1; then
    say "validating sudoers fragment syntax"
    if [[ "$DRY" == "1" ]]; then
        echo "  DRY: $VISUDO_BIN -cf '$SUDOERS_SRC'"
    else
        "$VISUDO_BIN" -cf "$SUDOERS_SRC" || {
            echo "ERR: $SUDOERS_SRC failed visudo syntax check — refusing to install" >&2
            exit 3
        }
    fi
else
    say "WARNING: '$VISUDO_BIN' not found — skipping syntax validation (install anyway)"
fi

say "installing minter -> $BIN_DEST (root:root, 0750)"
run "install -o root -g root -m 0750 '$BIN_SRC' '$BIN_DEST'"

say "installing sudoers grant -> $SUDOERS_DEST (root:root, 0440)"
run "install -o root -g root -m 0440 '$SUDOERS_SRC' '$SUDOERS_DEST'"

if [[ "$DRY" != "1" ]]; then
    say "re-validating the INSTALLED sudoers fragment (belt-and-suspenders)"
    "$VISUDO_BIN" -cf "$SUDOERS_DEST" || {
        echo "ERR: installed $SUDOERS_DEST failed visudo check — removing it" >&2
        rm -f "$SUDOERS_DEST"
        exit 3
    }
fi

say "done."
say "NOT done (deliberately, see this script's header + README.md):"
say "  - the App private key is NOT provisioned by this script"
say "  - the minter has NOT been run/smoke-tested"
say "  - the NoNewPrivileges=true vs sudo-n conflict (see the sudoers template's"
say "    header comment) has NOT been resolved — verify before relying on this"
say "next: drop the .pem (SOPS), then follow README.md's remaining steps."
