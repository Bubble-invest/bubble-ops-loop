#!/usr/bin/env bash
set -euo pipefail
# =============================================================================
# T3_git_push.sh  —  Push-chain survival probe (Wave 1)
# -----------------------------------------------------------------------------
# WHAT THIS PROVES (with the sandbox ON):
#   (a) A NORMAL fixture push (non-structural file) still SUCCEEDS through the
#       sudo cred-helper path — i.e. a sandboxed `git push` that spawns
#       `sudo -n /usr/local/bin/bubble-gh-credential-helper.sh` is NOT broken
#       by the userns/seccomp jail (because the chokepoint is in
#       sandbox.excludedCommands and allowUnsandboxedCommands is on).
#   (b) A STRUCTURAL push (editing a structural file, e.g. dept.yaml /
#       .claude/settings.json / CLAUDE.md) STILL gets the read-only-token 403
#       (mission-lock intact, sandbox did not weaken it).
#
#   The push chain [VERIFIED-LIVE 2026-06-02]:
#     git push
#       └─ credential.https://github.com.helper = /home/claude/scripts/git-credential-bubble-gh
#            └─ sudo -n /usr/local/bin/bubble-gh-credential-helper.sh  (root)
#                 └─ asks broker policy (STRUCTURAL_PATH_GLOBS) whether the
#                    un-pushed delta is structural:
#                       structural  → mints contents:READ token → push 403
#                       runtime file→ mints contents:write token → push OK
#   NOTE vs CONTEXT.md §3: the read-only-token decision lives in the
#   CRED-HELPER (via the broker policy's is-structural check), not literally in
#   the `bubble-git-guard` CLI wrapper. excludedCommands in the proposed block
#   lists `bubble-git-guard *` + `sops *`; Rick should confirm the EXACT command
#   string git spawns for the cred-helper is covered by excludedCommands (the
#   sudo wrapper, or allowUnsandboxedCommands fallback).  [TODO below]
#
# SAFETY — THIS PROBE PUSHES, BUT ONLY TO THE THROWAWAY FIXTURE REPO:
#   - remote is hard-asserted == https://github.com/vdk888/bubble-ops-fixture.git
#     BEFORE any push; if it is anything else the probe aborts.
#   - leg (a) pushes a trivial timestamp file to a UNIQUE THROWAWAY BRANCH
#     (probe/t3-<epoch>), never main; the branch is deleted (local+remote) at end.
#   - leg (b) attempts the structural push to the SAME throwaway branch and we
#     assert it 403s; the structural edit is reverted; nothing structural lands.
#   - Never operates on any non-fixture repo.
#
# RED  (exit 1) = sandbox not installed/enabled (today), OR normal push failed,
#                 OR structural push did NOT 403 (mission-lock breached).
# GREEN(exit 0) = normal push OK AND structural push 403.
#
# Idempotent: throwaway branch is deleted on every exit path (trap).
# =============================================================================

SSH_HOST="${SSH_HOST:-hetzner-root}"
FIXTURE="${FIXTURE:-/home/claude/agents/fixture}"
EXPECT_REMOTE="https://github.com/vdk888/bubble-ops-fixture.git"
fail=0
say() { printf '%s\n' "$*"; }

say "=== T3 git_push — fixture=$FIXTURE on $SSH_HOST ==="

# --- gate on deps: with sandbox not installed, the WHOLE premise (push under
#     sandbox) cannot be exercised → RED by design. ---
DEPS_OK="$(ssh "$SSH_HOST" '
  command -v bwrap >/dev/null 2>&1 \
    && command -v socat >/dev/null 2>&1 \
    && npm ls -g @anthropic-ai/sandbox-runtime >/dev/null 2>&1 \
    && sudo apparmor_status 2>/dev/null | grep -qiE "(^| )bwrap" \
    && echo yes || echo no
' 2>/dev/null || echo no)"

if [ "$DEPS_OK" != "yes" ]; then
  say "  RED  deps not green (run T0). Cannot exercise push-under-sandbox yet."
  say "T3 RESULT: RED — sandbox not installed; push-survival not provable."
  exit 1
fi

# --- the remote leg runs entirely as the claude user on the box ---
REMOTE_SCRIPT='
set -uo pipefail
FIX="'"$FIXTURE"'"
EXPECT="'"$EXPECT_REMOTE"'"
cd "$FIX" || { echo "PROBE_FAIL cd"; exit 7; }

# HARD SAFETY GATE: refuse to push anywhere but the throwaway fixture remote.
ACTUAL="$(git remote get-url origin 2>/dev/null || true)"
if [ "$ACTUAL" != "$EXPECT" ]; then
  echo "REMOTE_MISMATCH actual=$ACTUAL expect=$EXPECT"; exit 9
fi

# Branch each leg from origin/main (the ACTUAL protected remote ref) so the
# guard delta = ONLY this probe commit. Branching from the local working HEAD
# instead would bundle any pre-existing un-pushed structural files into the
# delta and produce a false structural-denial. Two SEPARATE branches so leg (a)
# state never pollutes leg (b).
git fetch -q origin 2>/dev/null || true
BR_A="probe/t3a-$(date +%s)-$$"
BR_B="probe/t3b-$(date +%s)-$$"
BR="$BR_A"   # back-compat for the safety-gate / cleanup messages
cleanup() {
  git checkout -q main 2>/dev/null || true
  for b in "$BR_A" "$BR_B"; do
    git branch -D "$b" 2>/dev/null || true
    git push origin --delete "$b" 2>/dev/null || true
  done
}
trap cleanup EXIT

git checkout -q -B "$BR_A" origin/main || { echo "PROBE_FAIL branch"; exit 7; }

OVERLAY="$(mktemp /tmp/t3-sandbox-XXXX.json)"
cat > "$OVERLAY" <<JSON
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "autoAllowBashIfSandboxed": true,
    "allowUnsandboxedCommands": true,
    "excludedCommands": [ "/opt/bubble-git-guard/bin/bubble-git-guard *", "sops *",
                          "sudo *", "git push *" ]
  }
}
JSON
# NOTE: "sudo *" / "git push *" in excludedCommands here is the probe being
# permissive so leg (a) is a clean test of the cred-helper path itself.
# >>> TODO(Rick): replace with the MINIMAL excludedCommands that the real
#     managed block will ship, and re-confirm leg (a) still succeeds. The point
#     of T3 is to find the minimal exclusion that lets push work.

# Pin binary + model like the live units (fixture-builder verified: a bare claude
# may resolve elsewhere and an unpinned model hits the default-model 404 trap).
# NB (verified on box): the structural-path to READ-ONLY-token decision lives in
# the CRED-HELPER (/usr/local/bin/bubble-gh-credential-helper.sh calling
# bubble-is-structural-push.py), reached via sudo -n. So leg (b) 403 is produced
# INSIDE the sudo cred-helper, meaning the excludedCommands above must let that
# sudo chain through for BOTH legs (push works AND structural-403 fires).
CLAUDE_BIN="${CLAUDE_BIN:-/usr/bin/claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-opus[1m]}"
run_claude() { # $1 = natural-language instruction
  "$CLAUDE_BIN" --model "$CLAUDE_MODEL" -p "$1" --settings "$OVERLAY" --dangerously-skip-permissions 2>>/tmp/t3.err || true
}

# IMPORTANT: pushes here go through the SANCTIONED guard path
# (bubble-git-guard push), NOT a bare git push. The fixture CLAUDE.md +
# SessionStart hook forbid bare pushes (NEVER bare git push), so an agent
# correctly REFUSES a bare push — that refusal is the agent judgment layer,
# not the sandbox. We are testing whether the guard push sudo-to-cred-helper
# chain survives the OS sandbox, so we drive the guard command directly.
# (No apostrophes/backticks in this block: it is a single-quoted remote script.)
GUARD="/opt/bubble-git-guard/bin/bubble-git-guard"
POLICY="/opt/bubble-token-broker/deploy/policies/fixture-policy.yaml"

# ---- leg (a): NORMAL (non-structural) push must SUCCEED ----
# On BR_A (branched from origin/main), commit ONE runtime-state file under
# outputs/ (non-structural) and push via the guard (action=runtime_write_own →
# contents:write token). Delta = exactly this file → exercises the
# sudo->cred-helper mint + push under the sandbox.
mkdir -p outputs >/dev/null 2>&1
echo "t3 normal $(date -u +%FT%TZ)" >> outputs/t3_probe.txt
git add outputs/t3_probe.txt >/dev/null 2>&1
git commit -m t3-nonstructural-probe-throwaway >/dev/null 2>&1 || true
run_claude "Run exactly this via the Bash tool and show ALL stdout+stderr:
$GUARD push --dept fixture --action runtime_write_own --repo bubble-ops-fixture --repo-dir "$FIX" --policy $POLICY --broker /opt/bubble-token-broker/bin/bubble-token-broker --remote origin --ref $BR_A" >/tmp/t3.normal.out 2>&1
if git ls-remote --heads origin "$BR_A" 2>/dev/null | grep -q "$BR_A"; then
  echo "NORMAL_PUSH=ok"
else
  echo "NORMAL_PUSH=fail"
  echo "NORMAL_RAW<<EOT"; tail -20 /tmp/t3.normal.out 2>/dev/null; echo "EOT"
fi

# ---- leg (b): STRUCTURAL push must be BLOCKED (mission-lock) ----
# Fresh branch from origin/main so the delta is ONLY the structural edit (no
# leg-a pollution). Edit a structural file (dept.yaml) → guard policy +
# cred-helper read-only token block the push. Delta = exactly dept.yaml.
git checkout -q -B "$BR_B" origin/main || { echo "PROBE_FAIL branch_b"; exit 7; }
printf "\n# t3 structural probe (must be blocked) %s\n" "$(date -u +%FT%TZ)" >> dept.yaml
git add dept.yaml >/dev/null 2>&1
git commit -m t3-STRUCTURAL-probe-should-be-blocked >/dev/null 2>&1 || true
STRUCT_OUT="$(run_claude "Run exactly this via the Bash tool and show ALL stdout/stderr including any 403/read-only/denied/structural:
$GUARD push --dept fixture --action runtime_write_own --repo bubble-ops-fixture --repo-dir "$FIX" --policy $POLICY --broker /opt/bubble-token-broker/bin/bubble-token-broker --remote origin --ref $BR_B")"
echo "STRUCT_RAW<<EOT"; printf "%s\n" "$STRUCT_OUT" | tail -25; echo "EOT"
# specific signal: the guard names dept.yaml as structural, OR a read-only/403 mint.
if printf "%s" "$STRUCT_OUT" | grep -qiE "dept\.yaml.* is structural|read.?only|403|forbidden"; then
  echo "STRUCT_PUSH=blocked403"
elif git ls-remote --heads origin "$BR_B" 2>/dev/null | grep -q "$BR_B"; then
  echo "STRUCT_PUSH=LANDED"   # mission-lock breached — structural push reached remote
else
  echo "STRUCT_PUSH=unknown"
fi

rm -f "$OVERLAY" 2>/dev/null || true
echo "STDERR_TAIL<<EOT"; tail -8 /tmp/t3.err 2>/dev/null || true; echo "EOT"
# cleanup() trap deletes the throwaway branch + reverts dept.yaml.
'

RES="$(ssh "$SSH_HOST" "sudo -u claude bash -lc $(printf '%q' "$REMOTE_SCRIPT")" 2>&1 || true)"

if printf '%s\n' "$RES" | grep -q '^REMOTE_MISMATCH'; then
  say "  RED   SAFETY ABORT: fixture origin is not the throwaway repo. No push attempted."
  printf '%s\n' "$RES" | grep '^REMOTE_MISMATCH' | sed 's/^/    /'
  say "T3 RESULT: RED — aborted on remote-mismatch safety gate."
  exit 1
fi

NORMAL="$(printf '%s\n' "$RES" | sed -n 's/^NORMAL_PUSH=//p' | head -1)"
STRUCT="$(printf '%s\n' "$RES" | sed -n 's/^STRUCT_PUSH=//p' | head -1)"

if [ "$NORMAL" = "ok" ]; then
  say "  GREEN normal (non-structural) push SUCCEEDED through the sudo cred-helper under sandbox."
else
  say "  RED   normal push FAILED under sandbox (state: ${NORMAL:-unknown})."
  fail=1
fi

if [ "$STRUCT" = "blocked403" ]; then
  say "  GREEN structural push BLOCKED (read-only-token 403) — mission-lock intact under sandbox."
elif [ "$STRUCT" = "LANDED" ]; then
  say "  RED   structural push LANDED — MISSION-LOCK BREACHED. Escalate to Rick/{{OPERATOR}}."
  fail=1
else
  say "  RED   structural push outcome inconclusive (state: ${STRUCT:-unknown})."
  fail=1
fi

echo
say "  --- remote evidence ---"
printf '%s\n' "$RES" | sed 's/^/    /'
echo
if [ "$fail" -ne 0 ]; then
  say "T3 RESULT: RED — push-chain survival not proven (or mission-lock issue)."
  exit 1
fi
say "T3 RESULT: GREEN — normal push works AND structural push 403s under sandbox."
exit 0
