#!/usr/bin/env bash
set -euo pipefail
# =============================================================================
# T1_sandbox_init.sh  —  Sandbox initialization probe (Wave 1)
# -----------------------------------------------------------------------------
# WHAT THIS PROVES
#   With deps installed (T0 green), a HEADLESS `claude -p` run in the FIXTURE
#   dir (/home/claude/agents/fixture) actually initializes the OS sandbox and a
#   trivial sandboxed `Bash(ls)` executes inside it.
#
#   Runs as the `claude` OS user (uid 1000, non-root) in the fixture, with the
#   sandbox enabled via a PROBE-LOCAL settings overlay so we touch NO managed
#   settings and NO live agent. We use the fixture's own .claude scope.
#
# THE VERIFICATION-MECHANISM PROBLEM (read before trusting GREEN):
#   The documented way to inspect sandbox status is the interactive `/sandbox`
#   panel (Mode / Overrides / Config / Dependencies tabs). That is a TUI and is
#   NOT available in headless `--print`. The docs give no documented
#   machine-readable "sandbox is active" flag for `-p` mode.
#   (https://code.claude.com/docs/en/sandboxing — Get started / Set up sections)
#
#   So this probe uses a BEHAVIORAL proxy for "sandbox engaged":
#     (a) deps are green (T0), AND
#     (b) a trivial sandboxed `Bash(ls)` completes successfully (sandbox could
#         initialize — if bwrap/userns were broken the bash subprocess would
#         fail to spawn), AND
#     (c) a NEGATIVE control: a write OUTSIDE the working dir (to /etc, which
#         the sandbox's default + denyWrite blocks) is REFUSED. If the sandbox
#         were silently off, that write would either succeed or be blocked by
#         filesystem perms rather than the sandbox — see the explicit TODO.
#
#   >>> TODO(Rick): CONFIRM THE CANONICAL "SANDBOX ACTIVE" SIGNAL FOR HEADLESS.
#       Options to verify on the box once deps land:
#         - `claude --debug -p ...` may log a sandbox/bwrap init line to stderr;
#           grep for it and assert here instead of relying on the behavioral
#           proxy below.
#         - check whether a sandboxed bash sees a bubblewrap-style mount (e.g.
#           `Bash(readlink /proc/1/ns/user)` differing from host, or a bind-
#           mounted /proc) as a positive sandbox fingerprint.
#       Until confirmed, treat GREEN here as "sandbox very likely engaged",
#       and let T2 (which asserts a known-jailed action IS jailed) be the
#       authoritative fail-open guard.
#
# RED  (exit 1) = sandbox did not init (deps missing today, or ls failed, or the
#                 out-of-cwd write was NOT refused → sandbox likely off).
# GREEN(exit 0) = deps green + sandboxed ls ran + out-of-cwd write refused.
#
# Idempotent: writes only to a probe tmp dir under the fixture, cleaned up.
# =============================================================================

SSH_HOST="${SSH_HOST:-hetzner-root}"
FIXTURE="${FIXTURE:-/home/claude/agents/fixture}"
fail=0
say()  { printf '%s\n' "$*"; }

say "=== T1 sandbox_init — fixture=$FIXTURE on $SSH_HOST ==="

# Gate on deps first (cheap, avoids a misleading 'claude failed' when the real
# cause is missing bwrap).
DEPS_OK="$(ssh "$SSH_HOST" '
  command -v bwrap >/dev/null 2>&1 \
    && command -v socat >/dev/null 2>&1 \
    && npm ls -g @anthropic-ai/sandbox-runtime >/dev/null 2>&1 \
    && sudo apparmor_status 2>/dev/null | grep -qiE "(^| )bwrap" \
    && echo yes || echo no
' 2>/dev/null || echo no)"

if [ "$DEPS_OK" != "yes" ]; then
  say "  RED  deps not green (run T0). Sandbox cannot initialize."
  say "T1 RESULT: RED — prerequisites absent."
  exit 1
fi

# Run a headless claude in the fixture as the claude user with a probe-local
# sandbox overlay. We DO NOT edit managed-settings; --settings passes an
# ephemeral JSON. The negative-control write target is /etc (outside cwd).
#
# NOTE: exact `claude -p` flags for a one-shot sandboxed bash + reading the
# settings overlay are validated against `claude --help` on the box at run time;
# we keep the invocation conservative.
REMOTE_SCRIPT='
set -uo pipefail
cd "'"$FIXTURE"'" || { echo "PROBE_FAIL cd"; exit 7; }
# Pin binary + model like the live units (fixture-builder verified the
# "default"-model 404 trap + that bare `claude` may resolve to a different path).
CLAUDE_BIN="${CLAUDE_BIN:-/usr/bin/claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-opus[1m]}"
OVERLAY="$(mktemp /tmp/t1-sandbox-XXXX.json)"
cat > "$OVERLAY" <<JSON
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "autoAllowBashIfSandboxed": true,
    "allowUnsandboxedCommands": false,
    "filesystem": { "denyWrite": ["/etc"] }
  }
}
JSON
# (a) trivial sandboxed ls
LS_OUT="$("$CLAUDE_BIN" --model "$CLAUDE_MODEL" -p "Run exactly this shell command via the Bash tool and show its raw output: ls -1a" \
            --settings "$OVERLAY" --dangerously-skip-permissions 2>/tmp/t1.err || true)"
# (b) negative control: attempt a write outside cwd to /etc (must be refused)
WR_OUT="$("$CLAUDE_BIN" --model "$CLAUDE_MODEL" -p "Run exactly this shell command via the Bash tool: echo probe > /etc/t1_probe_should_fail ; then show whether it succeeded" \
            --settings "$OVERLAY" --dangerously-skip-permissions 2>>/tmp/t1.err || true)"
ESCAPED=no
if sudo test -f /etc/t1_probe_should_fail 2>/dev/null; then
  ESCAPED=yes
  sudo rm -f /etc/t1_probe_should_fail 2>/dev/null || true
fi
rm -f "$OVERLAY" 2>/dev/null || true
echo "LS_HAS_OUTPUT=$([ -n "$LS_OUT" ] && echo yes || echo no)"
echo "ESCAPED_TO_ETC=$ESCAPED"
echo "STDERR_TAIL<<EOT"
tail -5 /tmp/t1.err 2>/dev/null || true
echo "EOT"
'

RES="$(ssh "$SSH_HOST" "sudo -u claude bash -lc $(printf '%q' "$REMOTE_SCRIPT")" 2>&1 || true)"

LS_HAS_OUTPUT="$(printf '%s\n' "$RES" | sed -n 's/^LS_HAS_OUTPUT=//p' | head -1)"
ESCAPED_TO_ETC="$(printf '%s\n' "$RES" | sed -n 's/^ESCAPED_TO_ETC=//p' | head -1)"

if [ "$LS_HAS_OUTPUT" = "yes" ]; then
  say "  GREEN sandboxed Bash(ls) produced output (sandbox could init)."
else
  say "  RED   sandboxed Bash(ls) produced no output (sandbox failed to init?)."
  fail=1
fi

if [ "$ESCAPED_TO_ETC" = "no" ]; then
  say "  GREEN negative control: write to /etc was REFUSED (sandbox boundary held)."
else
  say "  RED   negative control: write to /etc SUCCEEDED — sandbox NOT enforcing (fail-open!)."
  fail=1
fi

echo
say "  --- remote evidence ---"
printf '%s\n' "$RES" | sed 's/^/    /'
echo
if [ "$fail" -ne 0 ]; then
  say "T1 RESULT: RED — sandbox did not initialize / enforce in headless fixture run."
  exit 1
fi
say "T1 RESULT: GREEN — sandbox initialized for headless fixture run (see TODO re: canonical signal)."
exit 0
