#!/usr/bin/env bash
# bubble-board-token.sh — mint a SHORT-LIVED, issues:write-ONLY GitHub App token
# for the kanban control plane (Bubble-invest/bubble-ops-board), and print it.
#
# WHY: the `claude` user runs the dept agents + the emitter, but must NOT hold the
# bubble-ops-bot App private key (PEM is root-only by design). So this root-owned
# minter is exposed to `claude` via a tight sudoers NOPASSWD rule. It mints a token
# with the MINIMUM scope needed (issues:write + metadata:read) — it can create board
# issues, nothing else (no contents, no pushes, no other repo). The token lives ~1h.
#
# Prints ONLY the token to stdout (nothing else), or nothing + exit 1 on failure.
# Mirrors the JWT-mint logic of bubble-gh-credential-helper.sh.
#
# CONFIG (env, set privately at runtime — e.g. from SOPS / the install drop-in):
#   BUBBLE_GH_APP_ID       GitHub App ID of the board bot          (required)
#   BUBBLE_GH_INSTALL_ID   App installation ID (covers the org)    (required)
#   BUBBLE_BOARD_PEM_ENC   path to the SOPS-encrypted App PEM       (default below)
#   BUBBLE_AGE_KEY_FILE    age key file for SOPS decryption         (default below)
set -euo pipefail

APP_ID="${BUBBLE_GH_APP_ID:-}"
INST_ID="${BUBBLE_GH_INSTALL_ID:-}"     # App installation (covers all <org>/* repos)
PEM_ENC="${BUBBLE_BOARD_PEM_ENC:-/srv/bubble-secrets/github-app-bubble-ops-bot.private-key.sops.pem}"
AGE_KEY="${BUBBLE_AGE_KEY_FILE:-/etc/age/key.txt}"

[ -n "$APP_ID" ] && [ -n "$INST_ID" ] || {
  echo "bubble-board-token: BUBBLE_GH_APP_ID and BUBBLE_GH_INSTALL_ID must be set" >&2
  exit 1
}

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

# Scope the token to issues:write + metadata only — the minimum to create board issues.
RESP=$(curl -s -X POST \
  -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" \
  -d '{"permissions":{"issues":"write","metadata":"read"}}' \
  "https://api.github.com/app/installations/$INST_ID/access_tokens")
TOKEN=$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)

[ -n "$TOKEN" ] || exit 1
case "$TOKEN" in ghs_*) printf '%s' "$TOKEN" ;; *) exit 1 ;; esac
