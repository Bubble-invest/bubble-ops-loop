#!/usr/bin/env bash
# bubble-cockpit-approver-token-refresh.sh — mint a fresh pull_requests:write +
# statuses:write App-installation token into /run for the cockpit to read
# WITHOUT sudo (console runs NoNewPrivileges=yes, so it cannot sudo at
# request time). Run as root by a systemd timer every ~45min. Writes
# /run/bubble-cockpit-approver/token (tmpfs, 0640, claude-readable) —
# short-lived, never persisted to disk, pull_requests:write+statuses:write
# +metadata scope (statuses:write added board #1462, for the App-posted
# `structural-approval` commit status).
#
# 1:1 sibling of bubble-ops-contents-token-refresh.sh (board #1432 follow-up —
# the cockpit-approver minter originally shipped with a request-time `sudo -n`
# call, which cannot work under the console unit's NoNewPrivileges=true; this
# refresher fixes that the same way contents-token already did).
# console/services/pr_approver.py::_mint_token() reads this file.
set -euo pipefail
DEST_DIR=/run/bubble-cockpit-approver
DEST=$DEST_DIR/token
MINTER=/usr/local/bin/bubble-cockpit-approver-token.sh   # root-only; mints pull_requests:write+statuses:write+metadata
MAX_ATTEMPTS=4
BACKOFF_SECONDS=5

# GitHub API/auth incidents can be brief. Retry at the refresh boundary so one
# transient failure does not unnecessarily leave every approver-token consumer
# on the previous short-lived token. Never forward helper output: stdout may
# contain a credential and stderr is not part of this wrapper's safe logging
# contract.
attempt=1
while true; do
  TOK=""
  if TOK=$("$MINTER" 2>/dev/null); then
    case "$TOK" in
      ghs_*)
        if (( attempt > 1 )); then
          echo "bubble-cockpit-approver-token-refresh: mint succeeded on attempt $attempt/$MAX_ATTEMPTS" >&2
        fi
        break
        ;;
      "")
        echo "bubble-cockpit-approver-token-refresh: mint attempt $attempt/$MAX_ATTEMPTS failed (empty helper output)" >&2
        ;;
      *)
        echo "bubble-cockpit-approver-token-refresh: mint attempt $attempt/$MAX_ATTEMPTS failed (invalid helper output withheld)" >&2
        ;;
    esac
  else
    rc=$?
    echo "bubble-cockpit-approver-token-refresh: mint attempt $attempt/$MAX_ATTEMPTS failed (helper exit $rc; output withheld)" >&2
  fi

  if (( attempt >= MAX_ATTEMPTS )); then
    # Fail-closed, not fail-loud: if the App key genuinely isn't provisioned
    # yet (Joris hasn't dropped the .pem), this is EXPECTED and must not spam
    # the journal as a hard failure on every boot — the minter itself already
    # prints a clear "private key not provisioned" line to its own stderr
    # (withheld above), and pr_approver.py reports the same clean
    # "not_provisioned" status to the cockpit UI when the token file is simply
    # absent. Exit non-zero so systemd still records the attempt, but leave
    # the token file untouched either way.
    echo "bubble-cockpit-approver-token-refresh: mint failed after $MAX_ATTEMPTS attempts; token file left untouched (may be expected: key not provisioned yet)" >&2
    exit 1
  fi
  echo "bubble-cockpit-approver-token-refresh: retrying in ${BACKOFF_SECONDS}s" >&2
  sleep "$BACKOFF_SECONDS"
  attempt=$((attempt + 1))
  BACKOFF_SECONDS=$((BACKOFF_SECONDS * 2))
done

install -d -m 0750 -o root -g claude "$DEST_DIR"
umask 027
printf '%s' "$TOK" > "$DEST.tmp"
chown root:claude "$DEST.tmp"
chmod 0640 "$DEST.tmp"
mv -f "$DEST.tmp" "$DEST"
