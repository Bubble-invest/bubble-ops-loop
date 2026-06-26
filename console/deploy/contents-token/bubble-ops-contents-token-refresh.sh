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
TOK=$(/usr/local/bin/bubble-ops-contents-token.sh)   # root-only; mints contents:write+metadata
[ -n "$TOK" ] || { echo "mint failed" >&2; exit 1; }
case "$TOK" in ghs_*) ;; *) echo "bad token" >&2; exit 1 ;; esac
install -d -m 0750 -o root -g claude "$DEST_DIR"
umask 027
printf '%s' "$TOK" > "$DEST.tmp"
chown root:claude "$DEST.tmp"
chmod 0640 "$DEST.tmp"
mv -f "$DEST.tmp" "$DEST"
