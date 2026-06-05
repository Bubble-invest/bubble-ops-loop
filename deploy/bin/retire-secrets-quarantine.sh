#!/usr/bin/env bash
# =============================================================================
# retire-secrets-quarantine.sh — quarantine a retired dept's secrets.
# Deploy: /usr/local/bin/retire-secrets-quarantine.sh (root:root 0755)
# Invoked by retire_dept.py (Side effect 5) via `sudo -n` on Morty.
#
# DOCTRINE ({{OPERATOR}} 2026-06-05): a retired dept keeps its HISTORY (GitHub repo +
# transcripts) but loses live ACCESS. This:
#   1. Locks the Telegram bot — access.json -> dmPolicy=denied, allowFrom=[].
#      No one can DM the retired bot (immediate, code-level lockout).
#   2. Archives the SOPS env -> /etc/bubble/retired/secrets-<slug>.sops.env.<ts>
#      (reversible, audit trail — NOT deleted, in case of un-retire).
#   3. Wipes the runtime decrypted secrets: /run/bubble-<slug>, 
#      /run/claude-agent-<slug> (tmpfs — gone on reboot anyway, but wipe now).
#   4. Logs to the security audit trail + prints the MANUAL steps still needed
#      (BotFather token revoke, GitHub App install removal — human actions).
#
# Idempotent. Never deletes the SOPS env (archive only). Exit 0 unless a slug
# is missing.
# =============================================================================
set -uo pipefail

SLUG="${1:-}"
[[ -n "$SLUG" ]] || { echo "ERROR: dept slug required" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "ERROR: must run as root" >&2; exit 2; }

TS=$(date -u +%Y%m%d-%H%M%S)
SEC_LOG="/var/log/bubble-security/secrets-retire-${SLUG}-${TS}.log"
ACCESS_JSON="/home/claude/.claude/channels/telegram-${SLUG}/access.json"
SOPS_ENV="/etc/bubble/secrets-${SLUG}.sops.env"
ARCHIVE_DIR="/etc/bubble/retired"

mkdir -p /var/log/bubble-security "$ARCHIVE_DIR"
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$SEC_LOG"; }

log "=== Secret quarantine for retired dept: $SLUG ==="

# ── 1. Lock the Telegram bot (deny-all access.json) ─────────────────────────
if [[ -f "$ACCESS_JSON" ]]; then
    cp -p "$ACCESS_JSON" "${ACCESS_JSON}.bak-retire-${TS}"
    python3 - "$ACCESS_JSON" <<'PY'
import json, sys
f = sys.argv[1]
try:
    d = json.load(open(f))
except Exception:
    d = {}
d["dmPolicy"] = "denied"          # plugin: no DMs accepted
d["allowFrom"] = []               # no chat ids allowed
d["pending"] = {}
json.dump(d, open(f, "w"), indent=2); open(f, "a").write("\n")
PY
    chown claude:claude "$ACCESS_JSON" 2>/dev/null || true
    log "1. Telegram access LOCKED (dmPolicy=denied, allowFrom=[]) — backup ${ACCESS_JSON}.bak-retire-${TS}"
else
    log "1. SKIP — no access.json at $ACCESS_JSON (bot already gone?)"
fi

# ── 2. Archive the SOPS env (reversible, NOT deleted) ───────────────────────
if [[ -f "$SOPS_ENV" ]]; then
    mv "$SOPS_ENV" "${ARCHIVE_DIR}/secrets-${SLUG}.sops.env.${TS}"
    chmod 0600 "${ARCHIVE_DIR}/secrets-${SLUG}.sops.env.${TS}"
    log "2. SOPS env ARCHIVED -> ${ARCHIVE_DIR}/secrets-${SLUG}.sops.env.${TS} (encrypted, root 0600)"
else
    log "2. SKIP — no SOPS env at $SOPS_ENV"
fi

# ── 3. Wipe runtime decrypted secrets (tmpfs) ───────────────────────────────
for rd in "/run/bubble-${SLUG}" "/run/claude-agent-${SLUG}"; do
    if [[ -d "$rd" ]]; then
        rm -rf "${rd:?}"/* 2>/dev/null || true
        rmdir "$rd" 2>/dev/null || true
        log "3. Runtime secrets WIPED: $rd"
    fi
done

# ── 4. Manual steps still required (human / BotFather / GitHub) ──────────────
log "4. MANUAL revocation still required (cannot be automated):"
log "   - Telegram: revoke the bot token via @BotFather (/revoke or /deletebot for @bubbleops${SLUG}_bot)"
log "   - GitHub: remove the dept repo from the bubble-gh App installation (or its team write access)"
log "   - These are flagged here; the live ACCESS is already cut by steps 1-3."

log "=== Quarantine complete for $SLUG (history preserved; live access revoked) ==="
echo "QUARANTINE_OK slug=$SLUG log=$SEC_LOG"
exit 0
