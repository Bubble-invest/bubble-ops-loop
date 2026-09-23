#!/usr/bin/env bash
# bubble-cockpit-approver-token.sh — mint a SHORT-LIVED, pull_requests:write +
# statuses:write GitHub App token for the `cockpit-approver` App, so the
# cockpit can (a) submit an APPROVED review on a structural/mission-path PR
# AS the App (board #1432 option C2) and (b) POST the App-only
# `structural-approval` commit status (board #1462) — the unforgeable
# required-check the fleet's own workflow-editable `guard` check cannot be.
# Prints ONLY the token (ghs_...) to stdout.
#
# Why a separate App (not bubble-ops-bot): the guard (.github/workflows/
# structural-merge-guard.yml) accepts an approval ONLY from an identity the fleet's
# agents cannot impersonate. `vdk888` (Rick + all workers) holds `repo` scope and can
# self-review, so a @vdk888 approval is forgeable. The cockpit-approver App's private
# key is held ONLY here (root-owned SOPS), so an approval authored by
# `cockpit-approver[bot]` proves it came through Joris's authenticated cockpit action.
#
# Sibling of bubble-ops-contents-token.sh / bubble-board-token.sh — same JWT→installation
# -token flow, different App + narrower scope (pull_requests:write + statuses:write
# + metadata:read). `statuses:write` added for board #1462 — Joris granted +
# accepted "Commit statuses: write" on the App installation; without it, the
# App token gets a 403 "Resource not accessible by integration" on
# `POST /repos/{owner}/{repo}/statuses/{sha}` (console/services/
# structural_status.py's `post_status`).
set -euo pipefail

APP_ID=5019127
INST_ID=163454332                       # Bubble-invest installation of the cockpit-approver App
PEM_ENC=/srv/bubble-secrets/github-app-cockpit-approver.private-key.sops.pem
AGE_KEY=/etc/age/key.txt

[ -f "$PEM_ENC" ] || { echo "cockpit-approver: private key not provisioned ($PEM_ENC)" >&2; exit 1; }

PEM="$(mktemp)"; chmod 600 "$PEM"
trap 'rm -f "$PEM"' EXIT
SOPS_AGE_KEY_FILE="$AGE_KEY" /usr/local/bin/sops -d \
  --input-type binary --output-type binary --output "$PEM" "$PEM_ENC" 2>/dev/null
[ -s "$PEM" ] || { echo "cockpit-approver: could not decrypt private key" >&2; exit 1; }

b64() { openssl base64 -e -A | tr -- '+/' '-_' | tr -d '='; }
NOW=$(date +%s)
H=$(printf '{"alg":"RS256","typ":"JWT"}' | b64)
P=$(printf '{"iat":%d,"exp":%d,"iss":%d}' $((NOW-60)) $((NOW+540)) "$APP_ID" | b64)
S=$(printf '%s' "$H.$P" | openssl dgst -sha256 -sign "$PEM" -binary | b64)
JWT="$H.$P.$S"

# Scope to the minimum needed to submit a PR review AND post the
# `structural-approval` commit status: pull_requests:write + statuses:write +
# metadata:read.
RESP=$(curl -s -X POST \
  -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" \
  -d '{"permissions":{"pull_requests":"write","statuses":"write","metadata":"read"}}' \
  "https://api.github.com/app/installations/$INST_ID/access_tokens")
TOKEN=$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)

[ -n "$TOKEN" ] || { echo "cockpit-approver: no token in installation response" >&2; exit 1; }
case "$TOKEN" in ghs_*) printf '%s' "$TOKEN" ;; *) echo "cockpit-approver: unexpected token shape" >&2; exit 1 ;; esac
