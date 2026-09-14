#!/usr/bin/env bash
# bubble-board-token-refresh.sh — mint a fresh board token into /run for the
# cockpit to read WITHOUT sudo (the cockpit runs NoNewPrivileges=yes, so it
# cannot sudo at request time). Run as root by a systemd timer every ~45min.
# Writes /run/bubble-board/token (tmpfs, 0640, claude-readable) — short-lived,
# never persisted to disk, min-scope (issues:read via the existing minter).
#
# Board #1251: ALSO drops a per-dept copy at /run/bubble-board/token.<dept>
# (tmpfs, 0640, owned root:agent-<dept>) for every uid-isolated dept agent.
# Why: a dept's own Claude-Code Bash-tool session runs with NoNewPrivileges
# set on the sandboxed process, so `sudo -n bubble-board-token.sh` (the other
# fallback in emit_kanban_item.sh) can never work there regardless of any
# sudoers grant — reading a pre-minted file is the ONLY sandbox-safe path.
# The shared /run/bubble-board/token file is group `claude`-only, which
# excludes every agent-<dept> uid (post-#1120 isolation) — this is a
# read-only-file-visibility fix, NOT a broadened credential: each per-dept
# copy is readable ONLY by that same dept's own uid, which already holds an
# explicit NOPASSWD sudoers grant to mint this exact token itself
# (/etc/sudoers.d/bubble-board-token-agent-<dept>). We're just giving that
# already-authorized uid a sandbox-compatible way to read what it could mint.
# DEPT_LIST mirrors the sudoers.d roster (see /etc/sudoers.d/bubble-board-
# token-agent-*) — keep both in sync when a new dept is spawned.
DEPT_LIST="${BUBBLE_BOARD_TOKEN_DEPTS:-ben claudette maya morty tony}"
set -euo pipefail
DEST_DIR=/run/bubble-board
DEST=$DEST_DIR/token
MINTER=/usr/local/bin/bubble-board-token.sh   # root-only; mints issues:write+metadata
MAX_ATTEMPTS=4
BACKOFF_SECONDS=5

# GitHub API/auth incidents can be brief. Retry at the refresh boundary so one
# transient failure does not unnecessarily leave every board consumer on the
# previous short-lived token. Never forward helper output: stdout may contain a
# credential and stderr is not part of this wrapper's safe logging contract.
attempt=1
while true; do
  TOK=""
  if TOK=$("$MINTER" 2>/dev/null); then
    case "$TOK" in
      ghs_*)
        if (( attempt > 1 )); then
          echo "bubble-board-token-refresh: mint succeeded on attempt $attempt/$MAX_ATTEMPTS" >&2
        fi
        break
        ;;
      "")
        echo "bubble-board-token-refresh: mint attempt $attempt/$MAX_ATTEMPTS failed (empty helper output)" >&2
        ;;
      *)
        echo "bubble-board-token-refresh: mint attempt $attempt/$MAX_ATTEMPTS failed (invalid helper output withheld)" >&2
        ;;
    esac
  else
    rc=$?
    echo "bubble-board-token-refresh: mint attempt $attempt/$MAX_ATTEMPTS failed (helper exit $rc; output withheld)" >&2
  fi

  if (( attempt >= MAX_ATTEMPTS )); then
    echo "bubble-board-token-refresh: mint failed after $MAX_ATTEMPTS attempts; token file left untouched" >&2
    exit 1
  fi
  echo "bubble-board-token-refresh: retrying in ${BACKOFF_SECONDS}s" >&2
  sleep "$BACKOFF_SECONDS"
  attempt=$((attempt + 1))
  BACKOFF_SECONDS=$((BACKOFF_SECONDS * 2))
done

# Board #1251: dir mode 0751 (rwxr-x--x) instead of 0750 — the extra `--x`
# for "other" lets a non-claude-group uid (a dept's own agent-<dept>) TRAVERSE
# into the dir to open its own named per-dept token file below. It grants no
# read/listing: `ls` here still fails for anyone outside group claude, and a
# uid can only open a file whose exact name it already knows AND which it
# separately has read permission on (its own token.<dept> copy, chmod 0640
# root:agent-<dept> below) — the shared `token` file stays claude-group-only.
install -d -m 0751 -o root -g claude "$DEST_DIR"
umask 027
printf '%s' "$TOK" > "$DEST.tmp"
chown root:claude "$DEST.tmp"
chmod 0640 "$DEST.tmp"
mv -f "$DEST.tmp" "$DEST"

# Per-dept copies (board #1251 — sandbox-safe token path). Best-effort per
# dept: a dept whose unix group doesn't exist on this box (e.g. a dev/test
# host, or a dept not yet spawned) is skipped with a stderr note, never
# treated as a refresh failure — the shared claude-readable token above is
# already written and must not be held hostage by one missing dept group.
for _dept in $DEPT_LIST; do
  _dept_group="agent-${_dept}"
  if ! getent group "$_dept_group" >/dev/null 2>&1; then
    echo "bubble-board-token-refresh: skip per-dept copy for '${_dept}' — group ${_dept_group} not found on this host" >&2
    continue
  fi
  _dept_dest="${DEST}.${_dept}"
  printf '%s' "$TOK" > "${_dept_dest}.tmp"
  chown "root:${_dept_group}" "${_dept_dest}.tmp"
  chmod 0640 "${_dept_dest}.tmp"
  mv -f "${_dept_dest}.tmp" "$_dept_dest"
done
