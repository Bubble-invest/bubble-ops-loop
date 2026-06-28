#!/bin/sh
# guard-stale-credentials.sh — neutralize a stale ~/.claude/.credentials.json that would
# SHADOW the valid env CLAUDE_CODE_OAUTH_TOKEN, causing fleet-wide 401 (incident 2026-06-25,
# board #294). claude prefers the on-disk credentials file over the env token when present;
# the shared /home/claude/.claude/.credentials.json expired 2026-06-03 and 401'd all 5 depts.
#
# Guard logic (reversible, fail-open, never prints secrets):
#   IF the dept env file provides CLAUDE_CODE_OAUTH_TOKEN (the intended auth)
#   AND ~/.claude/.credentials.json exists
#   THEN move it aside (.shadowed-<ts>) so claude falls back to the env token.
# We only neutralize when an env token EXISTS to fall back to — so we never strip a
# credentials file that is the only auth available.
#
# Arg $1 = ENV_FILE path (the dept's /run/claude-agent-<dept>/env). Runs as root (ExecStartPre +).
set -u
ENV_FILE="${1:-}"
CRED="/home/claude/.claude/.credentials.json"

# Only act if the env file actually carries a CLAUDE_CODE_OAUTH_TOKEN to fall back to.
if [ -n "$ENV_FILE" ] && [ -r "$ENV_FILE" ] && grep -q '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE" 2>/dev/null; then
  if [ -e "$CRED" ]; then
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    mv -f "$CRED" "${CRED}.shadowed-${ts}" 2>/dev/null \
      && echo "guard-stale-credentials: moved shadowing $CRED aside (env token is authoritative)" \
      || echo "guard-stale-credentials: WARN could not move $CRED (continuing fail-open)"
  fi
fi
# Always succeed — this guard must never block a dept from starting.
exit 0
