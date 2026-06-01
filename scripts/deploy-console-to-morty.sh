#!/usr/bin/env bash
# deploy-console-to-morty.sh — git-pull console deploy (post-migration).
#
# Migration 2026-05-31 (Joris msg 3443/3445): bubble-ops-loop is now its
# OWN GitHub repo (Bubble-invest/bubble-ops-loop). The VPS
# /home/claude/bubble-ops-loop is a git CLONE tracking origin/main. There
# is NO local copy on anyone's Mac anymore.
#
# So "deploy" is no longer rsync-from-Mac. It is:
#   1. (you) commit + push your change to origin/main
#   2. this script: git pull on the VPS, restart the console, verify
#
# This replaces the old rsync flow. Why the old flow existed and its
# ghost-deploy lesson still applies: we always read WorkingDirectory from
# systemd (single source of truth) and verify a marker grep AFTER restart,
# so a misconfigured clone path can never silently serve stale code.
#
# Usage (from anywhere with SSH to the box, OR on the box itself):
#   scripts/deploy-console-to-morty.sh              # pull + restart + verify
#   scripts/deploy-console-to-morty.sh --dry-run    # show what would pull, no restart
#
# Requires:
#   - SSH alias to the box (default: joris-cx33), OR run on the box
#   - sudo NOPASSWD for `systemctl restart bubble-ops-console` on the box
#   - the box's git credential helper can read the private repo (GitHub App)

set -euo pipefail

SSH_HOST="${SSH_HOST:-joris-cx33}"
SERVICE="${SERVICE:-bubble-ops-console}"
BRANCH="${BRANCH:-main}"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

# Detect "on the box" so we use local commands instead of self-SSH (which
# fails on hosts without their own known-hosts entry). Joris flagged
# 2026-05-25 (msg 3165): Morty must be able to redeploy itself.
HOSTNAME_DETECTED="$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo unknown)"
ON_MORTY=0
if [[ "$HOSTNAME_DETECTED" == "joris-cx33" ]] || [[ "$HOSTNAME_DETECTED" == "morty" ]] || \
   [[ -d /home/claude && "$(uname -s 2>/dev/null)" == "Linux" && -f /etc/systemd/system/${SERVICE}.service ]]; then
  ON_MORTY=1
  echo "[deploy-console] Running ON the box (hostname=$HOSTNAME_DETECTED) — local commands, no SSH."
fi

run_remote() {
  if [[ "$ON_MORTY" == "1" ]]; then bash -c "$1"; else ssh "$SSH_HOST" "$1"; fi
}

# ── Step 1: resolve the repo dir from systemd (single source of truth) ─
# WorkingDirectory is where the service actually runs from = the clone we
# must pull. No more guessing the path.
WORKDIR="$(run_remote "systemctl show ${SERVICE} -p WorkingDirectory --value")"
if [[ -z "$WORKDIR" ]]; then
  echo "ERR: could not read WorkingDirectory for ${SERVICE} from systemd." >&2
  exit 2
fi
echo "[deploy-console] Service runs from: $WORKDIR"

# Sanity: it must be a git clone of the expected repo.
REMOTE_URL="$(run_remote "cd '$WORKDIR' && git config --get remote.origin.url 2>/dev/null || echo NONE")"
if [[ "$REMOTE_URL" != *"bubble-ops-loop"* ]]; then
  echo "ERR: $WORKDIR is not a clone of bubble-ops-loop (remote=$REMOTE_URL)." >&2
  echo "     The console must be deployed as a git clone — see repo README." >&2
  exit 3
fi

# ── Step 2: fetch + show what would change ────────────────────────────
run_remote "cd '$WORKDIR' && git fetch --quiet origin '$BRANCH'"
BEHIND="$(run_remote "cd '$WORKDIR' && git rev-list --count HEAD..origin/${BRANCH} 2>/dev/null || echo 0")"
echo "[deploy-console] Local is $BEHIND commit(s) behind origin/${BRANCH}."
if [[ "$BEHIND" == "0" ]]; then
  echo "[deploy-console] Already up to date — nothing to deploy."
  [[ "$DRY" == "1" ]] && exit 0
fi
run_remote "cd '$WORKDIR' && git --no-pager log --oneline HEAD..origin/${BRANCH} | sed 's/^/  /'" || true

if [[ "$DRY" == "1" ]]; then
  echo "[deploy-console] DRY RUN — no pull, no restart, no verify."
  exit 0
fi

# ── Step 3: pull (fast-forward only — refuse to deploy a diverged tree) ─
# A diverged WorkingDirectory means someone edited on the box without
# pushing. Fail loud rather than create a merge commit on a prod box.
if ! run_remote "cd '$WORKDIR' && git merge --ff-only 'origin/${BRANCH}'"; then
  echo "ERR: $WORKDIR has local changes / diverged from origin/${BRANCH}." >&2
  echo "     Commit+push or stash on the box, then re-run. Refusing to merge on prod." >&2
  exit 5
fi
echo "[deploy-console] Pulled to $(run_remote "cd '$WORKDIR' && git rev-parse --short HEAD")."

# ── Step 4: restart + confirm active ──────────────────────────────────
echo "[deploy-console] Restarting ${SERVICE} …"
run_remote "sudo -n systemctl restart ${SERVICE}"
sleep 2
ACTIVE="$(run_remote "systemctl is-active ${SERVICE}")"
if [[ "$ACTIVE" != "active" ]]; then
  echo "ERR: ${SERVICE} failed to restart (state=$ACTIVE). Check journalctl." >&2
  exit 4
fi
echo "[deploy-console] Service active."

# ── Step 5: verify USER-VISIBLE state, not just files-on-disk ─────────
# is-active lies (proc-only env, trust modals, etc.). Curl the live
# endpoint and assert HTTP 200 — the only honest "it serves" signal.
PORT="$(run_remote "systemctl show ${SERVICE} -p Environment --value | tr ' ' '\n' | sed -n 's/^CONSOLE_BIND_PORT=//p'")"
PORT="${PORT:-8642}"
CODE="$(run_remote "TOK=\$(sudo cat /proc/\$(systemctl show ${SERVICE} -p MainPID --value)/environ | tr '\0' '\n' | sed -n 's/^CONSOLE_BEARER_TOKEN=//p'); curl -s -o /dev/null -w '%{http_code}' -H \"Authorization: Bearer \$TOK\" http://127.0.0.1:${PORT}/")"
if [[ "$CODE" != "200" ]]; then
  echo "ERR: console did not return 200 after deploy (got $CODE). Rolling concern — check journalctl." >&2
  exit 6
fi
echo "[deploy-console] ✓ Live endpoint returns 200 — deploy verified."
echo "[deploy-console] Done."
