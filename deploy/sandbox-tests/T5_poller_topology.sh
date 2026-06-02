#!/usr/bin/env bash
set -euo pipefail
# =============================================================================
# T5_poller_topology.sh  —  Telegram bun-poller topology probe (Wave 1)
# -----------------------------------------------------------------------------
# WHAT THIS DETERMINES (READ-ONLY — ps / cat /proc / systemctl only; NEVER
# getUpdates, NEVER restart, NEVER touch a live agent's state):
#   Is the telegram `bun` poller a Bash/exec CHILD of the `claude` process
#   (→ it runs INSIDE the sandbox, so it needs network.allowedDomains
#   ["api.telegram.org"] + filesystem.allowWrite for the plugin cache /
#   channels dir), OR is it a SEPARATE HOST process / its own systemd unit
#   (→ outside the sandbox, needs no sandbox allowance)?
#
#   This resolves the open question in SANDBOX-SCOPING.md:
#     allowWrite: [..., "/home/claude/.claude/channels"]  // ONLY if poller is
#     a Bash child (T5)
#
# METHOD (all non-disruptive):
#   1. Find running `bun ... telegram ... server.ts` processes (the poller).
#   2. Walk each poller's parent chain via /proc/<pid>/stat (PPid) and
#      /proc/<pid>/cmdline until we hit a `claude` ancestor or PID 1 / a
#      systemd unit boundary.
#   3. Cross-check cgroup: read /proc/<poller>/cgroup and /proc/<claude>/cgroup;
#      if the poller shares the claude process's cgroup subtree (vs its own
#      *.service slice) that confirms "child of claude", not "separate unit".
#   4. Confirm there is NO dedicated poller systemd unit.
#
# RED  (exit 1) = could not produce a clear determination (no poller found, or
#                 ambiguous ancestry). [Today, if no live agent is running this
#                 returns RED — that's fine; it means 'not determinable now'.]
# GREEN(exit 0) = produced a clear CHILD-OF-CLAUDE or SEPARATE-HOST verdict,
#                 with the parent chain + cgroup as evidence, and stated the
#                 sandbox-allowance implication.
#
# Idempotent, read-only. Does not write to the box.
# =============================================================================

SSH_HOST="${SSH_HOST:-hetzner-root}"
say() { printf '%s\n' "$*"; }

say "=== T5 poller_topology — $SSH_HOST (READ-ONLY) ==="

REMOTE_SCRIPT='
set -uo pipefail

# 1. find poller pids (bun running the telegram plugin server)
POLLERS="$(ps -eo pid,args 2>/dev/null \
  | grep -iE "bun .*server\.ts|bun run .*telegram" | grep -v grep \
  | awk "{print \$1}" | sort -u)"

if [ -z "$POLLERS" ]; then
  echo "POLLERS=none"
  exit 0
fi
echo "POLLERS=$(echo $POLLERS | tr "\n" " ")"

ppid_of()  { awk "{print \$4}" "/proc/$1/stat" 2>/dev/null; }   # field 4 = PPid
comm_of()  { tr -d "\0" < "/proc/$1/comm" 2>/dev/null; }
cmd_of()   { tr "\0" " " < "/proc/$1/cmdline" 2>/dev/null; }
cg_of()    { cat "/proc/$1/cgroup" 2>/dev/null | head -1; }

for P in $POLLERS; do
  echo "--- poller pid=$P ---"
  echo "  cmd:    $(cmd_of "$P")"
  echo "  cgroup: $(cg_of "$P")"
  # walk ancestors
  CUR="$P"; CLAUDE_ANCESTOR=""; HOPS=0
  CHAIN=""
  while [ -n "$CUR" ] && [ "$CUR" != "0" ] && [ "$CUR" != "1" ] && [ "$HOPS" -lt 12 ]; do
    C="$(comm_of "$CUR")"
    CHAIN="$CHAIN $CUR($C)"
    if [ "$C" = "claude" ]; then CLAUDE_ANCESTOR="$CUR"; fi
    CUR="$(ppid_of "$CUR")"
    HOPS=$((HOPS+1))
  done
  echo "  chain:  $CHAIN -> ...$CUR"
  if [ -n "$CLAUDE_ANCESTOR" ]; then
    echo "  VERDICT=child-of-claude ancestor_pid=$CLAUDE_ANCESTOR"
    echo "  claude_cgroup: $(cg_of "$CLAUDE_ANCESTOR")"
  else
    echo "  VERDICT=no-claude-ancestor"
  fi
done

# 4. is there a dedicated poller systemd unit? (there should NOT be if it is a
#    claude child)
echo "--- dedicated poller unit check ---"
sudo systemctl list-units --type=service --all 2>/dev/null \
  | grep -iE "poll|telegram" | grep -vi "ops-loop|claude-agent" || echo "  (no dedicated telegram/poller unit)"
'

RES="$(ssh "$SSH_HOST" "sudo -u root bash -lc $(printf '%q' "$REMOTE_SCRIPT")" 2>&1 || true)"

POLLERS_LINE="$(printf '%s\n' "$RES" | sed -n 's/^POLLERS=//p' | head -1)"

if [ -z "$POLLERS_LINE" ] || [ "$POLLERS_LINE" = "none" ]; then
  say "  RED   no running bun telegram poller found → topology not determinable right now."
  say "        (Re-run while at least one agent OR the fixture loop is live.)"
  echo
  printf '%s\n' "$RES" | sed 's/^/    /'
  say "T5 RESULT: RED — no poller running; cannot determine topology."
  exit 1
fi

CHILD_HITS="$(printf '%s\n' "$RES" | grep -c 'VERDICT=child-of-claude' || true)"
NOANC_HITS="$(printf '%s\n' "$RES" | grep -c 'VERDICT=no-claude-ancestor' || true)"

echo
say "  --- evidence ---"
printf '%s\n' "$RES" | sed 's/^/    /'
echo

if [ "${CHILD_HITS:-0}" -ge 1 ] && [ "${NOANC_HITS:-0}" -eq 0 ]; then
  say "  GREEN DETERMINATION: poller is a CHILD of the claude process."
  say "         IMPLICATION: it runs INSIDE the sandbox. The managed block MUST grant"
  say "         it network.allowedDomains:[\"api.telegram.org\"] AND filesystem.allowWrite"
  say "         for the plugin cache + channels dir (e.g. /home/claude/.claude/plugins/cache,"
  say "         /home/claude/.claude/channels, /home/claude/.bun). Keep that allowWrite line."
  say "T5 RESULT: GREEN — clear determination (child-of-claude)."
  exit 0
elif [ "${NOANC_HITS:-0}" -ge 1 ] && [ "${CHILD_HITS:-0}" -eq 0 ]; then
  say "  GREEN DETERMINATION: poller is a SEPARATE HOST process (no claude ancestor)."
  say "         IMPLICATION: it runs OUTSIDE the sandbox → no sandbox allowance needed;"
  say "         the channels/telegram allowWrite + allowedDomains lines can be dropped."
  say "T5 RESULT: GREEN — clear determination (separate-host)."
  exit 0
fi

say "  RED   ambiguous: mixed/zero verdicts (child=${CHILD_HITS:-0} noanc=${NOANC_HITS:-0})."
say "T5 RESULT: RED — topology ambiguous; inspect evidence above."
exit 1
