#!/usr/bin/env bash
# bubble-cockpit-decision-key-decrypt.sh — boot-time decrypt of the cockpit's
# STATIC Ed25519 decision-signing PRIVATE key into tmpfs, for
# scripts/lib/decision_signing.py::load_private_key() /
# console/services/github_reader.py::_sign_decision() to read.
#
# Board #1498 (follow-up to board #1476 / PR #492's deploy runbook). 1:1
# shape-mirror of console/deploy/cockpit-approver/
# bubble-cockpit-approver-token-refresh.sh's atomic-write + fail-closed
# pattern, adapted for:
#   - a STATIC key (no repeating 45-min mint — run once at boot by a oneshot
#     unit, see bubble-cockpit-decision-key.service)
#   - a plain SOPS binary decrypt instead of a GitHub App JWT->installation
#     token mint.
#
# Never decrypts to stdout — sops-guard (the Layer-2 wrapper around the real
# `sops` binary) blocks decrypt-to-stdout outright, so `--output FILE` is
# mandatory, not a style choice. Never logs key material, only paths/outcomes.
set -euo pipefail

SOPS_PEM="${BUBBLE_DECISION_KEY_SOPS_SRC:-/srv/bubble-secrets/decision-signing-ed25519.private-key.sops.pem}"
AGE_KEY="${BUBBLE_DECISION_KEY_AGE_KEY_FILE:-/etc/age/key.txt}"
DEST_DIR="${RUNTIME_DIRECTORY:-/run/bubble-cockpit-decision-key}"
DEST="$DEST_DIR/key"
SOPS_BIN="${BUBBLE_DECISION_KEY_SOPS_BIN:-/usr/local/bin/sops}"
DEST_GROUP="${BUBBLE_DECISION_KEY_GROUP:-bubble-console}"

# Fail CLOSED with a clear message if the encrypted source isn't there yet.
# This is the expected pre-provisioning state (board #1498 step 1 already
# happened on the VPS, but this script must degrade cleanly on any other
# box/rerun too) — scripts/lib/decision_signing.py::load_private_key() will
# raise FileNotFoundError against the (never-written) $DEST, and
# console/services/github_reader.py::_sign_decision() already catches that
# and writes the decision UNSIGNED (DECISION_SIGNATURES=warn degrade path).
# So this unit failing does not, and must not, take bubble-ops-console down.
if [[ ! -f "$SOPS_PEM" ]]; then
  echo "bubble-cockpit-decision-key: SOPS source not found at $SOPS_PEM — decision-signing private key is NOT provisioned yet (console will write gate decisions UNSIGNED under DECISION_SIGNATURES=warn until this is fixed)" >&2
  exit 1
fi

install -d -m 0750 -o root -g "$DEST_GROUP" "$DEST_DIR"
umask 027

TMP="$DEST.tmp.$$"
trap 'rm -f "$TMP"' EXIT

# Exact decrypt invocation per board #1498's runbook (verified round-trip by
# the key-generation step): --input-type json (the SOPS binary-format
# envelope), --output-type binary (the plaintext PEM), --output (mandatory
# under sops-guard).
SOPS_AGE_KEY_FILE="$AGE_KEY" "$SOPS_BIN" --decrypt --input-type json --output-type binary --output "$TMP" "$SOPS_PEM" 2>/dev/null

if [[ ! -s "$TMP" ]]; then
  echo "bubble-cockpit-decision-key: sops decrypt produced no output — refusing to write $DEST (any previously-decrypted key, if present, is left untouched)" >&2
  exit 1
fi

# Atomic: write to a tmp file in the same dir, then rename — a reader never
# observes a partially-written key.
chown "root:$DEST_GROUP" "$TMP"
chmod 0640 "$TMP"
mv -f "$TMP" "$DEST"

echo "bubble-cockpit-decision-key: decrypted to $DEST (root:$DEST_GROUP 0640)" >&2
