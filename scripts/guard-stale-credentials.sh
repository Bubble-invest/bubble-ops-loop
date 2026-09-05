#!/bin/sh
# guard-stale-credentials.sh — neutralize a stale ~/.claude/.credentials.json that would
# SHADOW the valid env CLAUDE_CODE_OAUTH_TOKEN, causing fleet-wide 401 (incident 2026-06-25,
# board #294). claude prefers the on-disk credentials file over the env token when present;
# the shared /home/claude/.claude/.credentials.json expired 2026-06-03 and 401'd all 5 depts.
#
# Board #1150 (canonical location + per-user HOME): this script lives under
# scripts/ (like every other framework-consumed installer — install-boot-
# rearm.sh, install-channel-patches.sh, vendor-dept-libs.sh — so it copies
# cleanly from a root-owned /opt/bubble-ops-loop checkout via
# bubble-safe-install, e.g. `bubble-safe-install scripts/guard-stale-
# credentials.sh /usr/local/bin/guard-stale-credentials.sh`). It used to
# live at deploy/bin/ — a wrapper that execs the scripts/ path (matching
# every other installer) would 404 against the old location.
#
# Under per-dept OS-user isolation the credentials file to check is the
# INVOKING dept's own $HOME, never a hardcoded /home/claude — that legacy
# shared-user path silently no-ops for every agent-<slug> user (their real
# stale-credentials file, if any, is never found/moved). This runs as root
# via ExecStartPre=+, so $HOME is root's, not the dept's: bubble-agent-
# prepare (bubble-vps-platform) already exports BUBBLE_AGENT_HOME for
# validate_identity before invoking this script, and systemd Environment=
# vars are inherited by child processes — so BUBBLE_AGENT_HOME is the
# correct signal here, with $HOME as a fallback for manual/test invocation.
#
# Guard logic (reversible, fail-open, never prints secrets):
#   IF the dept env file provides CLAUDE_CODE_OAUTH_TOKEN (the intended auth)
#   AND $HOME/.claude/.credentials.json exists (for that dept's own HOME)
#   THEN move it aside (.shadowed-<ts>) so claude falls back to the env token.
# We only neutralize when an env token EXISTS to fall back to — so we never strip a
# credentials file that is the only auth available.
#
# Arg $1 = ENV_FILE path (the dept's /run/bubble-agent-<dept>/env). Runs as root (ExecStartPre +).
set -u
ENV_FILE="${1:-}"
AGENT_HOME="${BUBBLE_AGENT_HOME:-${HOME:-}}"

# Fail-open, loudly: an empty/unset HOME must never resolve to "/.claude/..."
# (root of the filesystem) — refuse instead of guessing.
if [ -z "$AGENT_HOME" ]; then
  echo "guard-stale-credentials: WARN neither BUBBLE_AGENT_HOME nor \$HOME set — skipping (fail-open)"
  exit 0
fi
CRED="${AGENT_HOME}/.claude/.credentials.json"

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
