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
# Board #1251 r2 (manager review): DEPT_LIST is DERIVED from the actual
# sudoers.d grants, not hand-maintained — a hardcoded list enforced only by
# a "keep these in sync" comment lets a copy-paste dept-spawn slip silently
# hand an unauthorized uid a token it could never mint itself (a credential
# widening by omission, with nothing to catch it). Each
# /etc/sudoers.d/bubble-board-token-agent-<dept> file IS the authorization
# grant for <dept> to mint this exact token itself
# (agent-<dept> ALL=(root) NOPASSWD: /usr/local/bin/bubble-board-token.sh) —
# so <dept> is read from that filename, and the SAME file is re-checked
# per-dept below before any copy is written. This makes the property
# structural: the script cannot mint a copy for a dept that lacks the
# grant, no matter what any list says, and a dept that loses its grant
# stops getting a copy on the very next refresh — the correct direction to
# fail.
#
# BUBBLE_BOARD_TOKEN_DEPTS is kept ONLY as a fallback candidate source for a
# host with NO sudoers.d roster at all (this repo's test suite has none) —
# it is NOT a way to add a dept beyond what the roster grants. Whenever ANY
# bubble-board-token-agent-* file exists on the host, the roster is
# authoritative: every candidate (roster- AND override-derived) is
# re-verified against its own specific grant file before it gets a copy —
# an override dept with no matching sudoers file gets NO token, full stop.
# BUBBLE_BOARD_TOKEN_SUDOERS_DIR overrides where that roster is read from
# (test harness only; production is always /etc/sudoers.d).
SUDOERS_DIR="${BUBBLE_BOARD_TOKEN_SUDOERS_DIR:-/etc/sudoers.d}"
SUDOERS_PREFIX="bubble-board-token-agent-"

_dept_is_authorized() {
  # The actual authorization check — distinct from `getent group` below,
  # which only proves the unix group exists, NOT that this dept was ever
  # granted the minter. A dept without this file was never authorized.
  [ -f "${SUDOERS_DIR}/${SUDOERS_PREFIX}${1}" ]
}

ROSTER_DEPTS=""
for _f in "$SUDOERS_DIR"/${SUDOERS_PREFIX}*; do
  [ -f "$_f" ] || continue
  ROSTER_DEPTS="${ROSTER_DEPTS} ${_f##*/${SUDOERS_PREFIX}}"
done

if [ -n "$ROSTER_DEPTS" ]; then
  # A real roster exists on this host — it is authoritative. Still fold in
  # any override so an unauthorized override dept is CONSIDERED and then
  # explicitly REJECTED below (loud, testable), rather than just silently
  # never appearing.
  ROSTER_PRESENT=1
  CANDIDATE_DEPTS="${ROSTER_DEPTS} ${BUBBLE_BOARD_TOKEN_DEPTS:-}"
else
  # No sudoers.d roster at all on this host (e.g. a test/dev box) — nothing
  # to authorize against, so the override (if any) is used as-is.
  ROSTER_PRESENT=0
  CANDIDATE_DEPTS="${BUBBLE_BOARD_TOKEN_DEPTS:-}"
fi

# De-dup (a dept can appear via both the roster and an override).
DEPT_LIST="$(printf '%s\n' $CANDIDATE_DEPTS | awk 'NF && !seen[$0]++' | tr '\n' ' ')"

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
  # Authorization gate (r2 review — the check that actually matters): only
  # enforced when a real roster exists on this host at all (ROSTER_PRESENT),
  # so the BUBBLE_BOARD_TOKEN_DEPTS escape valve still works on a roster-less
  # test host. Whenever a roster IS present, every candidate — whether it
  # came from the roster itself or from an override — must have its OWN
  # grant file; `getent group` below proves the group exists, not that this
  # dept was ever authorized to mint the token, so it cannot substitute.
  if [ "$ROSTER_PRESENT" -eq 1 ] && ! _dept_is_authorized "$_dept"; then
    echo "bubble-board-token-refresh: skip per-dept copy for '${_dept}' — no matching ${SUDOERS_DIR}/${SUDOERS_PREFIX}${_dept} grant (unauthorized, not just unlisted)" >&2
    continue
  fi
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
