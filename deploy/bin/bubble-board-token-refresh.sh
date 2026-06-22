#!/usr/bin/env bash
# bubble-board-token-refresh.sh — mint a fresh board token into /run for the
# cockpit to read WITHOUT sudo (the cockpit runs NoNewPrivileges=yes, so it
# cannot sudo at request time). Run as root by a systemd timer every ~45min.
# Writes /run/bubble-board/token (tmpfs, 0640, claude-readable) — short-lived,
# never persisted to disk, min-scope (issues:read via the existing minter).
set -euo pipefail
DEST_DIR=/run/bubble-board
DEST=$DEST_DIR/token
TOK=$(/usr/local/bin/bubble-board-token.sh)   # already root-only; mints issues:write+metadata
[ -n "$TOK" ] || { echo "mint failed" >&2; exit 1; }
case "$TOK" in ghs_*) ;; *) echo "bad token" >&2; exit 1 ;; esac
install -d -m 0750 -o root -g claude "$DEST_DIR"
umask 027
printf '%s' "$TOK" > "$DEST.tmp"
chown root:claude "$DEST.tmp"
chmod 0640 "$DEST.tmp"
mv -f "$DEST.tmp" "$DEST"
