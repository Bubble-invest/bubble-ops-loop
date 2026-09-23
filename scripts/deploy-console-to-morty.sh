#!/usr/bin/env bash
# deploy-console-to-morty.sh — git-pull console deploy (post-migration).
#
# Board #1081: after pulling, this also re-syncs the systemd UNIT from
# console/deploy/bubble-ops-console.service.template via
# deploy-console-to-morty.sh's sibling scripts/deploy-console-to-vps.sh
# (--no-restart — this script does its own restart+verify below). That
# closes the gap where template edits (e.g. #1074's StateDirectory) never
# reached the box: previously this script restarted the OLD installed unit,
# never the checked-in one. For a box that has never had the unit installed
# at all, run scripts/deploy-console-to-vps.sh directly first (fresh box —
# see deploy/INSTALL.md step 2); this script assumes the unit already
# exists (it reads WorkingDirectory from it in step 1 below).
#
# Migration 2026-05-31 ({{OPERATOR}} msg 3443/3445): bubble-ops-loop is now its
# OWN GitHub repo (Bubble-invest/bubble-ops-loop). There is NO local copy on
# anyone's Mac anymore.
#
# So "deploy" is no longer rsync-from-Mac. It is:
#   1. (you) commit + push your change to origin/main
#   2. this script: get the code onto the box, restart the console, verify
#
# Board #1463 (cockpit uid isolation): the console's WorkingDirectory is now
# the ROOT-OWNED, read-only infra clone /opt/bubble-ops-loop — the SAME
# "framework-source" clone `bubble-deploy-infra.timer` already keeps current
# every 15 min via `bubble-deploy.sh --infra-only` (running as root; see
# deploy/INSTALL.md step 4, "the isolated loop layer floor" already reads the
# very same clone instead of the retired shared `claude` identity). It is NOT
# a bespoke deploy copy invented for the console, and it is NOT
# claude-writable — so step 2 below can no longer `git fetch`/`merge` it
# in place (the deploying identity has no write access there by design, and
# must not be given any). Instead this script now WAITS for the existing
# timer to converge (optionally nudging it via a narrow, already-precedented
# `sudo -n systemctl start bubble-deploy-infra.service` grant) rather than
# pulling the tree itself. `/home/claude/bubble-ops-loop` (claude-writable)
# still exists as its own independent sibling clone for Rick's own git
# operations — it is simply no longer what the console RUNS from.
#
# Why the old flow (git pull in the console's OWN WorkingDirectory) still
# applies verbatim on a claude-writable WORKDIR (e.g. a dev/staging box that
# hasn't cut over yet): we always read WorkingDirectory from systemd (single
# source of truth) and verify a marker grep AFTER restart, so a misconfigured
# clone path can never silently serve stale code — that part is unchanged.
#
# Usage (from anywhere with SSH to the box, OR on the box itself):
#   scripts/deploy-console-to-morty.sh              # sync + restart + verify
#   scripts/deploy-console-to-morty.sh --dry-run    # show what would change, no restart
#
# Requires:
#   - SSH alias to the box (default: $BUBBLE_VPS_HOST, else "morty"), OR run on the box
#   - sudo NOPASSWD for `systemctl restart bubble-ops-console` on the box, plus
#     (for the Step 3.5 unit re-sync) `install` and `systemctl daemon-reload` —
#     see scripts/deploy-console-to-vps.sh
#   - the box's git credential helper can read the private repo (GitHub App)
#   - IF WorkingDirectory is root-owned (the normal #1463 shape): sudo NOPASSWD
#     for `systemctl start bubble-deploy-infra.service` (optional — without it,
#     this script just waits up to BUBBLE_DEPLOY_INFRA_WAIT_SEC for the existing
#     15-min timer to converge on its own; it never writes into a root-owned
#     WorkingDirectory itself)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SSH_HOST="${SSH_HOST:-${BUBBLE_VPS_HOST:-morty}}"
SERVICE="${SERVICE:-bubble-ops-console}"
BRANCH="${BRANCH:-main}"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

# Detect "on the box" so we use local commands instead of self-SSH (which
# fails on hosts without their own known-hosts entry). {{OPERATOR}} flagged
# 2026-05-25 (msg 3165): Morty must be able to redeploy itself.
HOSTNAME_DETECTED="$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo unknown)"
ON_MORTY=0
if [[ "$HOSTNAME_DETECTED" == "${BUBBLE_VPS_HOST:-morty}" ]] || [[ "$HOSTNAME_DETECTED" == "morty" ]] || \
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

# Board #1463: WORKDIR is root-owned when it's the shared infra clone
# (/opt/bubble-ops-loop). We must never attempt to write there ourselves —
# neither `git fetch` nor `git merge` (fetch writes .git/objects + FETCH_HEAD,
# same problem as merge). Branch on ownership instead of hardcoding the path,
# so this still works unchanged against a claude-writable WORKDIR (e.g. a
# not-yet-cut-over dev/staging box).
WORKDIR_OWNER="$(run_remote "stat -c '%U' '$WORKDIR' 2>/dev/null || echo unknown")"
ROOT_OWNED_INFRA=0
[[ "$WORKDIR_OWNER" == "root" ]] && ROOT_OWNED_INFRA=1

if [[ "$ROOT_OWNED_INFRA" == "1" ]]; then
  echo "[deploy-console] WorkingDirectory is the root-owned infra clone ($WORKDIR_OWNER) — deferring to bubble-deploy-infra.timer instead of pulling it ourselves."

  # ── Step 2 (root-owned variant): read-only drift check ────────────────
  # `git ls-remote` needs no local write access — safe read-only network
  # call from any identity.
  REMOTE_HEAD="$(run_remote "git ls-remote '$REMOTE_URL' 'refs/heads/${BRANCH}'" | awk '{print $1}')"
  LOCAL_HEAD="$(run_remote "cd '$WORKDIR' && git rev-parse HEAD 2>/dev/null || echo ''")"
  if [[ -z "$REMOTE_HEAD" ]]; then
    echo "ERR: could not resolve origin/${BRANCH} via ls-remote." >&2
    exit 3
  fi
  if [[ "$LOCAL_HEAD" == "$REMOTE_HEAD" ]]; then
    echo "[deploy-console] $WORKDIR already at origin/${BRANCH} ($LOCAL_HEAD) — nothing to sync."
    [[ "$DRY" == "1" ]] && exit 0
  else
    echo "[deploy-console] $WORKDIR ($LOCAL_HEAD) is behind origin/${BRANCH} ($REMOTE_HEAD)."
    if [[ "$DRY" == "1" ]]; then
      echo "[deploy-console] DRY RUN — would nudge/wait for bubble-deploy-infra.timer, then restart+verify."
      exit 0
    fi
    # Best-effort nudge: if the deploying identity has the (narrow, optional)
    # sudoers grant, fire the sync now instead of waiting up to 15 min for
    # the timer's own cadence. Never fatal if the grant is absent — the timer
    # converges on its own.
    if run_remote "sudo -n systemctl start bubble-deploy-infra.service" 2>/dev/null; then
      echo "[deploy-console] Nudged bubble-deploy-infra.service."
    else
      echo "[deploy-console] No sudo grant to nudge bubble-deploy-infra.service (or it's already running) — waiting for it to converge."
    fi
    WAIT_SEC="${BUBBLE_DEPLOY_INFRA_WAIT_SEC:-120}"
    WAITED=0
    while [[ "$WAITED" -lt "$WAIT_SEC" ]]; do
      LOCAL_HEAD="$(run_remote "cd '$WORKDIR' && git rev-parse HEAD 2>/dev/null || echo ''")"
      [[ "$LOCAL_HEAD" == "$REMOTE_HEAD" ]] && break
      sleep 5
      WAITED=$((WAITED + 5))
    done
    if [[ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]]; then
      echo "ERR: $WORKDIR still at $LOCAL_HEAD after waiting ${WAIT_SEC}s for bubble-deploy-infra.timer." >&2
      echo "     Check 'journalctl -u bubble-deploy-infra.service' on the box, or re-run once it converges." >&2
      exit 5
    fi
    echo "[deploy-console] $WORKDIR converged to $LOCAL_HEAD."
  fi
else
  # ── Step 2 (claude-writable variant, unchanged): fetch + show diff ────
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

  # ── Step 3 (claude-writable variant): pull (fast-forward only) ────────
  # A diverged WorkingDirectory means someone edited on the box without
  # pushing. Fail loud rather than create a merge commit on a prod box.
  if ! run_remote "cd '$WORKDIR' && git merge --ff-only 'origin/${BRANCH}'"; then
    echo "ERR: $WORKDIR has local changes / diverged from origin/${BRANCH}." >&2
    echo "     Commit+push or stash on the box, then re-run. Refusing to merge on prod." >&2
    exit 5
  fi
  echo "[deploy-console] Pulled to $(run_remote "cd '$WORKDIR' && git rev-parse --short HEAD")."
fi

# ── Step 3.5: re-sync the systemd unit from the checked-in template ────
# Board #1081: without this, a template edit (e.g. #1074's StateDirectory)
# lands on origin/main but the box keeps running whatever unit was
# hand-installed at some point in the past — this step is what makes
# "git pull" actually apply unit changes too. Restart happens once, below
# (Step 4), so pass --no-restart here.
SSH_HOST="$SSH_HOST" SERVICE="$SERVICE" CONSOLE_WORKDIR="$WORKDIR" \
  "$SCRIPT_DIR/deploy-console-to-vps.sh" --no-restart || {
  echo "ERR: unit re-sync (deploy-console-to-vps.sh) failed — see above." >&2
  exit 7
}

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
