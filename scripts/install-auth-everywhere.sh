#!/usr/bin/env bash
# install-auth-everywhere.sh — wire the EXISTING GitHub credential helper into
# every context on the box, and make anonymous git fall through LOUDLY instead
# of silently.
#
# Part of the bubble-ops-loop install package. Idempotent: safe to re-run on
# every deploy / fresh box bring-up. root-only (writes /etc/environment,
# root's git config, and root's safe.directory list).
#
# ============================================================================
# WHY THIS EXISTS (evidence from the 2026-07-16 session)
#   - As ROOT, no credential helper was wired: `git fetch` failed with
#     "could not read Username for 'https://github.com'" but exited 0 (git's
#     credential-helper protocol treats a helper miss as "try anonymous", and
#     anonymous itself failed silently on a private repo) — leaving stale refs
#     and a confusing "Repository not found" on the next command, with no
#     nonzero exit to signal anything was wrong.
#   - GIT_TERMINAL_PROMPT was set NOWHERE, so any git invocation that hit an
#     interactive credential prompt would hang (or, worse, half-fail) instead
#     of erroring immediately.
#   - root's git config carried a blanket `safe.directory=*` AND several
#     redundant explicit entries (belt-and-suspenders that had drifted from
#     the claude user's convention of one explicit entry per repo).
#
# ============================================================================
# WHAT THIS DOES NOT DO — READ BEFORE TOUCHING
#   - Does NOT create a second token source. It wires
#     `credential.https://github.com.helper` to the SAME root-owned
#     /usr/local/bin/bubble-gh-credential-helper.sh that already mints tokens
#     for the claude user (via its sudo wrapper /home/claude/scripts/
#     git-credential-bubble-gh). root runs the helper DIRECTLY (no sudo
#     needed — root already is root); this is the only difference from the
#     claude-side wiring, everything else is identical.
#   - Does NOT touch the helper's LOCK LOGIC. The mission-file lock
#     (is-structural-push.py decides read-only vs write BEFORE the helper
#     mints a token) is untouched by this script — it lives entirely inside
#     bubble-gh-credential-helper.sh / bubble-is-structural-push.py, neither
#     of which this installer modifies. This script only changes WHERE the
#     helper is invoked from (which git contexts call it), never WHAT it
#     decides.
#   - Does NOT broaden token scope. root gets the exact same helper, same
#     lock, same PERMS logic as claude already has.
#   - Does NOT apply anything except to the box this is run ON, and only when
#     invoked explicitly (no auto-run, no dept unit wires it, no cron).
#
# Usage (as root, on the box, after review):
#   bash scripts/install-auth-everywhere.sh
#   bash scripts/install-auth-everywhere.sh --dry-run
#
# Exit codes:
#   0  installed (or already wired — no-op)
#   1  must run as root
#   2  structural error (helper script missing, etc.)

set -euo pipefail

HELPER_BIN="${AUTH_EVERYWHERE_HELPER:-/usr/local/bin/bubble-gh-credential-helper.sh}"
ETC_ENVIRONMENT="${AUTH_EVERYWHERE_ETC_ENV:-/etc/environment}"

# repo dirs that MUST be in root's safe.directory list, mirroring the claude
# user's convention (one explicit entry per repo — no blanket wildcard).
ROOT_SAFE_DIRS=(
  "/home/claude/bubble-ops-loop"
  "/home/claude/agents/bubble-ops-ben"
  "/home/claude/agents/bubble-ops-maya"
  "/home/claude/agents/bubble-ops-tony"
  "/home/claude/agents/bubble-ops-cgp"
  "/home/claude/agents/bubble-ops-content"
  "/home/claude/agents/bubble-ops-accountant"
  "/home/claude/agents/bubble-ops-fixture"
  "/home/claude/agents/claudette"
)

# repo dirs missing from the claude user's safe.directory list today
# (bubble-ops-content and claudette exist under /home/claude/agents/ but were
# never added).
CLAUDE_MISSING_SAFE_DIRS=(
  "/home/claude/agents/claudette"
)

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

say() { echo "[install-auth-everywhere] $*"; }
run() { if [[ "$DRY" == "1" ]]; then echo "  DRY: $*"; else eval "$*"; fi; }

if [[ "$DRY" != "1" && "$(id -u)" != "0" ]]; then
  echo "ERR: must run as root (writes /etc/environment + root's git config)" >&2
  exit 1
fi

[[ -f "$HELPER_BIN" ]] || { echo "ERR: missing $HELPER_BIN — nothing to wire" >&2; exit 2; }

# ── step 1: wire the credential helper for root's global git config ────────
# root runs the helper directly (already root — no sudo wrapper needed).
CURRENT_HELPER="$(git config --global --get credential.https://github.com.helper 2>/dev/null || true)"
if [[ "$CURRENT_HELPER" == "$HELPER_BIN" ]]; then
  say "root credential.helper already wired — no-op"
else
  say "wiring root credential.helper -> $HELPER_BIN"
  run "git config --global credential.https://github.com.helper '$HELPER_BIN'"
  run "git config --global credential.https://github.com.useHttpPath true"
fi

# ── step 2: normalize root's safe.directory list (explicit entries, drop the
#    blanket wildcard + de-dupe) ─────────────────────────────────────────────
say "normalizing root safe.directory entries"
run "git config --global --unset-all safe.directory 2>/dev/null || true"
for d in "${ROOT_SAFE_DIRS[@]}"; do
  run "git config --global --add safe.directory '$d'"
done

# ── step 3: GIT_TERMINAL_PROMPT=0 fleet-wide via /etc/environment ──────────
# Read by PAM for every login session (root's interactive SSH shells AND the
# claude user's), so anonymous-fallback git now ERRORS LOUDLY (nonzero exit,
# "terminal prompts disabled") instead of hanging or silently no-op'ing.
# Does NOT affect the dept systemd units (they don't source /etc/environment
# by default) — those already avoid the anonymous-fallback trap entirely by
# using the bubble-git/bubble-gh pre-auth wrappers, never plain git.
if [[ -f "$ETC_ENVIRONMENT" ]] && grep -q '^GIT_TERMINAL_PROMPT=' "$ETC_ENVIRONMENT" 2>/dev/null; then
  say "GIT_TERMINAL_PROMPT already set in $ETC_ENVIRONMENT — no-op"
else
  say "appending GIT_TERMINAL_PROMPT=0 to $ETC_ENVIRONMENT"
  run "printf '%s\n' 'GIT_TERMINAL_PROMPT=0' | tee -a '$ETC_ENVIRONMENT' >/dev/null"
fi

# ── step 4: fill the claude user's missing safe.directory entries ──────────
# (bubble-ops-content and claudette agent dirs exist but were never added.)
say "checking claude user's safe.directory entries for gaps"
EXISTING_CLAUDE_SAFE="$(sudo -u claude git config --global --get-all safe.directory 2>/dev/null || true)"
for d in "${CLAUDE_MISSING_SAFE_DIRS[@]}"; do
  if echo "$EXISTING_CLAUDE_SAFE" | grep -qxF "$d"; then
    say "  claude safe.directory already has $d — no-op"
  else
    say "  adding claude safe.directory: $d"
    run "sudo -u claude git config --global --add safe.directory '$d'"
  fi
done

say "done."
say ""
say "Post-install test plan (run manually — see PR body):"
say "  1. root fetch now authenticates:  cd /home/claude/bubble-ops-loop && git fetch"
say "  2. anonymous fallback errors loud: unset the helper temporarily on a throwaway"
say "     clone of a private repo and confirm nonzero exit (not a silent hang/no-op)."
say "  3. structural-file push STILL 403s: attempt to push a change to dept.yaml (or"
say "     any STRUCTURAL_PATH_GLOBS path) from a non-structural-lock-bypassed context"
say "     and confirm the mint downgrades to read-only + the push fails."
exit 0
