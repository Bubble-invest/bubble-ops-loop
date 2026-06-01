#!/usr/bin/env bash
# deploy-console-to-morty.sh — safe console redeploy that ALWAYS hits
# the path systemd actually runs from.
#
# Why this exists: incident 2026-05-24 (msg 3035) — I deployed Wave-3
# Step 0b + gate grouping + retire CTA to /srv/bubble-ops/console/ and
# restarted bubble-ops-console. Restart succeeded, file markers on disk
# verified — but the running service uses WorkingDirectory=
# /home/claude/bubble-ops-loop, so NOTHING was actually live. Joris
# caught it via a screenshot showing the old layout.
#
# This script reads the WorkingDirectory from systemd (single source of
# truth), rsyncs the WHOLE console/ tree under it (preserving subdirs),
# restarts the service, then verifies a sample marker grep before
# declaring success. No more silent ghost-deploys.
#
# Usage:
#   scripts/deploy-console-to-morty.sh
#   scripts/deploy-console-to-morty.sh --dry-run
#
# Requires:
#   - SSH alias `joris-cx33` configured (passwordless to claude user)
#   - sudo NOPASSWD for `systemctl restart bubble-ops-console` on Morty

set -euo pipefail

SSH_HOST="${SSH_HOST:-joris-cx33}"
SERVICE="${SERVICE:-bubble-ops-console}"
LOCAL_CONSOLE="$(cd "$(dirname "$0")/.." && pwd)/console"
DRY=""
[[ "${1:-}" == "--dry-run" ]] && DRY="--dry-run"

if [[ ! -d "$LOCAL_CONSOLE" ]]; then
  echo "ERR: local console dir not found: $LOCAL_CONSOLE" >&2
  exit 2
fi

# Detect if we're already running ON Morty itself (= the host being
# deployed to). If yes, replace ssh+rsync with local cp/systemctl —
# self-SSH fails on hosts that don't have their own known-hosts entry.
# Joris flagged 2026-05-25 (msg 3165): Morty must be able to redeploy
# itself when Joris's Mac is in the shop for 3 days.
ON_MORTY=0
# Detect "on Morty" by checking the actual hostname OR by detecting we're
# inside the Hetzner Linux env (uname -n / /etc/hostname). Any of these
# matches means we're on the target host and should use local commands.
HOSTNAME_DETECTED="$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo unknown)"
if [[ "$HOSTNAME_DETECTED" == "joris-cx33" ]] || [[ "$HOSTNAME_DETECTED" == "morty" ]] || \
   [[ -d /home/claude && "$(uname -s 2>/dev/null)" == "Linux" && -f /etc/systemd/system/bubble-ops-console.service ]]; then
  ON_MORTY=1
  echo "[deploy-console] Running ON Morty itself (hostname=$HOSTNAME_DETECTED) — using local commands (no SSH)."
fi

run_remote() {
  if [[ "$ON_MORTY" == "1" ]]; then
    bash -c "$1"
  else
    ssh "$SSH_HOST" "$1"
  fi
}

run_rsync() {
  # $1 = src, $2 = dest path on remote
  local src="$1" dest="$2"
  if [[ "$ON_MORTY" == "1" ]]; then
    # Local copy preserving the same semantics
    mkdir -p "$dest"
    rsync -avz $DRY --exclude='__pycache__/' --exclude='*.pyc' "$src" "$dest"
  else
    rsync -avz $DRY --exclude='__pycache__/' --exclude='*.pyc' "$src" "$SSH_HOST:$dest"
  fi
}

# ── Step 1: ask systemd where it actually runs from ──────────────────
echo "[deploy-console] Reading WorkingDirectory from systemd…"
WORKDIR="$(run_remote "systemctl show $SERVICE -p WorkingDirectory --value")"
if [[ -z "$WORKDIR" || "$WORKDIR" == "[not set]" ]]; then
  echo "ERR: $SERVICE has no WorkingDirectory — cannot deploy blindly." >&2
  exit 3
fi
TARGET="$WORKDIR/console"
echo "[deploy-console] Active console path on $SSH_HOST: $TARGET"

# ── Step 2: rsync the WHOLE console tree (preserves subdirs) ─────────
# We sync routes/ services/ templates/ static/ explicitly so a stray
# top-level file doesn't ride along.
SUBDIRS=(routes services templates static)
echo "[deploy-console] Rsyncing ${SUBDIRS[*]} → $TARGET …"
for sub in "${SUBDIRS[@]}"; do
  if [[ -d "$LOCAL_CONSOLE/$sub" ]]; then
    run_rsync "$LOCAL_CONSOLE/$sub/" "$TARGET/$sub/"
  fi
done
# Top-level python files (main.py, settings.py if any)
echo "[deploy-console] Rsyncing top-level *.py …"
if [[ "$ON_MORTY" == "1" ]]; then
  mkdir -p "$TARGET"
  rsync -avz $DRY --include='*.py' --exclude='*' "$LOCAL_CONSOLE/" "$TARGET/"
else
  rsync -avz $DRY --include='*.py' --exclude='*' "$LOCAL_CONSOLE/" "$SSH_HOST:$TARGET/"
fi

if [[ -n "$DRY" ]]; then
  echo "[deploy-console] DRY RUN complete — no restart, no verify."
  exit 0
fi

# ── Step 3: restart service ──────────────────────────────────────────
echo "[deploy-console] Restarting $SERVICE on $SSH_HOST …"
run_remote "sudo -n systemctl restart $SERVICE"
sleep 2
ACTIVE="$(run_remote "systemctl is-active $SERVICE")"
if [[ "$ACTIVE" != "active" ]]; then
  echo "ERR: $SERVICE failed to restart (state=$ACTIVE). Check journalctl." >&2
  exit 4
fi
echo "[deploy-console] Service active."

# ── Step 4: verify by sampling a recent marker ───────────────────────
# Grep for the latest 2 markers we just pushed; if they're missing, the
# deploy went to the wrong place (defense in depth).
MARKERS=(
  "$TARGET/routes/dept.py:group_gates_by_kind"
  "$TARGET/templates/dept_detail.html:Mettre à la retraite"
)
echo "[deploy-console] Verifying markers on remote…"
FAIL=0
for entry in "${MARKERS[@]}"; do
  file="${entry%%:*}"
  pat="${entry#*:}"
  count=$(run_remote "grep -c '$pat' '$file' 2>/dev/null || echo 0") || count=0
  count=${count:-0}
  if [[ "$count" -lt 1 ]]; then
    echo "  ✗ MISSING: $pat in $file"
    FAIL=1
  else
    echo "  ✓ found (${count}x): $pat in $file"
  fi
done
if [[ "$FAIL" -ne 0 ]]; then
  echo "ERR: marker verification failed — deploy may be silently inert." >&2
  exit 5
fi

echo "[deploy-console] ✅ Deploy complete + verified live on $SSH_HOST."
