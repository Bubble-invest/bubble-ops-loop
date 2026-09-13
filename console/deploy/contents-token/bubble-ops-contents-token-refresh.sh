#!/usr/bin/env bash
# bubble-ops-contents-token-refresh.sh — mint a fresh contents:write token into
# /run for the cockpit to read WITHOUT sudo (console runs NoNewPrivileges=yes, so
# it cannot sudo at request time). Run as root by a systemd timer every ~45min.
# Writes /run/bubble-ops-contents/token (tmpfs, 0640, claude-readable) —
# short-lived, never persisted to disk, contents:write+metadata scope.
#
# Sibling of bubble-board-token-refresh.sh (which serves the issues-only board
# token). github_reader._read_contents_token() reads this file.
set -euo pipefail
DEST_DIR=/run/bubble-ops-contents
DEST=$DEST_DIR/token
MINTER=/usr/local/bin/bubble-ops-contents-token.sh   # root-only; mints contents:write+metadata
MAX_ATTEMPTS=4
BACKOFF_SECONDS=5

# GitHub API/auth incidents can be brief. Retry at the refresh boundary so one
# transient failure does not unnecessarily leave every contents-token consumer
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
          echo "bubble-ops-contents-token-refresh: mint succeeded on attempt $attempt/$MAX_ATTEMPTS" >&2
        fi
        break
        ;;
      "")
        echo "bubble-ops-contents-token-refresh: mint attempt $attempt/$MAX_ATTEMPTS failed (empty helper output)" >&2
        ;;
      *)
        echo "bubble-ops-contents-token-refresh: mint attempt $attempt/$MAX_ATTEMPTS failed (invalid helper output withheld)" >&2
        ;;
    esac
  else
    rc=$?
    echo "bubble-ops-contents-token-refresh: mint attempt $attempt/$MAX_ATTEMPTS failed (helper exit $rc; output withheld)" >&2
  fi

  if (( attempt >= MAX_ATTEMPTS )); then
    echo "bubble-ops-contents-token-refresh: mint failed after $MAX_ATTEMPTS attempts; token file left untouched" >&2
    exit 1
  fi
  echo "bubble-ops-contents-token-refresh: retrying in ${BACKOFF_SECONDS}s" >&2
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
