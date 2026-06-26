#!/usr/bin/env bash
# bubble-ops-contents-token.sh — mint a SHORT-LIVED, contents:write GitHub App
# token for committing gate decisions to host=local dept repos (e.g.
# Bubble-invest/bubble-ops-content). Prints ONLY the token to stdout.
#
# WHY: the cockpit (console, user `claude`, NoNewPrivileges) must commit an
# operator's decision to a host=local dept's GitHub repo so the dept's Mac loop
# pulls it (host=local delivery — Miranda/content). It has no `gh auth` and must
# NOT hold the bubble-ops-bot App private key (root-only). So this root-owned
# minter (exposed to the refresher only) mints the MINIMUM scope: contents:write
# + metadata:read on the Bubble-invest installation. Token lives ~1h.
#
# This is the contents:write sibling of bubble-board-token.sh (issues:write).
# Same App, same installation, narrower-purpose token.
set -euo pipefail

APP_ID=3782718
INST_ID=135214360                       # Bubble-invest installation (covers all Bubble-invest/*)
PEM_ENC=/srv/bubble-secrets/github-app-bubble-ops-bot.private-key.sops.pem
AGE_KEY=/etc/age/key.txt

PEM="$(mktemp)"; chmod 600 "$PEM"
trap 'rm -f "$PEM"' EXIT
SOPS_AGE_KEY_FILE="$AGE_KEY" /usr/local/bin/sops -d \
  --input-type binary --output-type binary --output "$PEM" "$PEM_ENC" 2>/dev/null
[ -s "$PEM" ] || exit 1

b64() { openssl base64 -e -A | tr -- '+/' '-_' | tr -d '='; }
NOW=$(date +%s)
H=$(printf '{"alg":"RS256","typ":"JWT"}' | b64)
P=$(printf '{"iat":%d,"exp":%d,"iss":%d}' $((NOW-60)) $((NOW+540)) "$APP_ID" | b64)
S=$(printf '%s' "$H.$P" | openssl dgst -sha256 -sign "$PEM" -binary | b64)
JWT="$H.$P.$S"

# Scope the token to contents:write + metadata:read — the minimum needed to PUT
# a decision file into a dept repo. No issues, no admin, no other scope.
RESP=$(curl -s -X POST \
  -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" \
  -d '{"permissions":{"contents":"write","metadata":"read"}}' \
  "https://api.github.com/app/installations/$INST_ID/access_tokens")
TOKEN=$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)

[ -n "$TOKEN" ] || exit 1
case "$TOKEN" in ghs_*) printf '%s' "$TOKEN" ;; *) exit 1 ;; esac
