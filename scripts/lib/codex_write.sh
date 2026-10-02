#!/usr/bin/env bash
# codex_write.sh — the ONE mandatory Codex-CLI writing call for all Bubble writing skills.
# Codex writes the prose; the dept orchestrates (select, brief, verify, gate, post/send).
# Established 2026-09-04 (Joris). Docs: skills/codex-write/SKILL.md + shared-wiki miranda_socials/codex-cli-model-and-usage.md
#
# Usage:
#   scripts/lib/codex_write.sh --brief BRIEF.txt --out OUT.txt [--model gpt-6.1-sol] [--effort high]
#   (BRIEF.txt = the assembled writing brief: topic + selected pool item + imposed voice-file reading + hard rules)
#
# SUCCESS TEST (fixed 2026-09-14, card #1301): the Codex process exit code (rc=0) PLUS a
# fresh, non-empty $OUT file is the ONLY thing that counts as success. $OUT is rm -f'd
# before the call, so a stale leftover file can never be mistaken for a fresh draft.
# Historical/transient error-looking text on the console (stdout+stderr of the `codex`
# invocation itself -- reasoning traces, recovered-error events, etc.) is NEVER allowed
# to override a valid rc=0 + non-empty-output result. Root cause of the false-logout
# bug this replaces: a broad regex matched benign console text even when Codex had
# already returned rc=0 with a good draft, and callers/operators then misread that
# false FAIL-SOFT as "Codex needs re-login" with no real auth signal behind it.
#
# Only when the success test above FAILS do we classify *why*, from the captured
# console text, into one of: auth | model | runtime. This is printed on
# stderr as a machine-greppable `codex_write: CLASSIFICATION=<kind>` line so callers/
# operators stop guessing "needs re-login" for every fail-soft. NEVER assume auth
# failure without a real auth-shaped signal in the text.
#
# CALLER CONTRACT: do not pipe this script's stdout/stderr through another command
# (e.g. `| tail`) when you intend to inspect its exit status afterwards -- `$?` after
# a pipeline is the LAST command's exit status (e.g. tail's, always 0), not this
# script's. Either check `$?` immediately after calling it directly, or run your shell
# with `set -o pipefail` first. This script does not (and cannot) fix that for you --
# it is a property of the calling shell, not of this script.
#
# Exit codes:
#   0  = success. Either Codex drafted (rc=0 AND fresh non-empty $OUT), OR — when Codex
#        was unavailable — the SONNET FALLBACK drafted it (channel-clean, authed,
#        Morty-safe `claude -p --model sonnet`; a CLASSIFICATION= line still reports why
#        Codex was skipped). Works where a token is available ($CLAUDE_CODE_OAUTH_TOKEN
#        or ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json, e.g. VPS/cron callers).
#   2  = usage error (bad args / missing brief file / missing --out)
#   3  = FAIL-SOFT: Codex unavailable AND the Sonnet fallback couldn't run (no token in
#        context, e.g. a keychain-only Mac) -> caller MUST fall back to drafting directly
#        (and flag it to operators, using the CLASSIFICATION= line for auth/model/runtime).
#        NEVER let writing halt.
set -uo pipefail

# Explicit flags override environment defaults; empty env values use the defaults.
# CLAUDE_CONFIG_DIR (else $HOME/.claude) supplies fallback credentials only;
# the fallback always runs with a fresh, isolated configuration directory.
MODEL="${CODEX_MODEL:-gpt-6.1-sol}"; EFFORT="${CODEX_REASONING_EFFORT:-high}"; BRIEF=""; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --brief|--out|--model|--effort)
      [ $# -ge 2 ] && [ -n "$2" ] && [[ "$2" != --* ]] || {
        echo "codex_write: $1 requires a value" >&2; exit 2;
      }
      ;;
  esac
  case "$1" in
    --brief) BRIEF="$2"; shift 2;;
    --out)   OUT="$2"; shift 2;;
    --model) MODEL="$2"; shift 2;;
    --effort) EFFORT="$2"; shift 2;;
    *) echo "codex_write: unknown arg $1" >&2; exit 2;;
  esac
done
[ -n "$BRIEF" ] && [ -f "$BRIEF" ] || { echo "codex_write: --brief FILE required and must exist" >&2; exit 2; }
[ -n "$OUT" ] || { echo "codex_write: --out FILE required" >&2; exit 2; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Clear stale output even when the CLI is missing. Never run on a stale file
# if removal failed (for example, because its parent directory is read-only).
rm -f "$OUT" || {
  echo "codex_write: FAIL-SOFT cannot remove stale output -> draft directly" >&2
  echo "codex_write: CLASSIFICATION=runtime" >&2
  exit 3
}
if command -v codex >/dev/null 2>&1; then
  # Read-only sandbox: Codex reads voice/brand files, no writes/network.
  ERR="$(codex exec -m "$MODEL" -c model_reasoning_effort="$EFFORT" -s read-only --skip-git-repo-check \
          -C "$REPO" -o "$OUT" - < "$BRIEF" 2>&1)"
  RC=$?
else
  ERR="codex CLI not found"
  RC=127
fi

# --- GROUND TRUTH: rc=0 + fresh (rm -f'd above) non-empty $OUT is success, full stop. ---
# Console text (reasoning traces, recovered/handled-error events, etc.) never overrides
# this -- that override was exactly the false-logout bug (#1301).
if [ $RC -eq 0 ] && [ -s "$OUT" ]; then
  echo "codex_write: OK ($MODEL/$EFFORT) -> $OUT" >&2
  exit 0
fi

# --- FAILURE: classify auth vs model vs runtime from the captured text. ---
# "runtime" is the deliberate default bucket for anything unrecognized (CLI crash,
# network/timeout, sandbox denial, or just no matching signature) -- there is no
# separate "unknown" bucket: an unrecognized failure is a runtime problem until
# proven otherwise, and must never default toward "auth" without real evidence.
# Never print $ERR verbatim beyond a short, bounded tail -- it may echo request/response
# framing from the CLI, and we never want to encourage logging full transcripts that
# could carry a token. Classification is pattern-based only; no credential value is
# ever inspected, matched on, or printed by this script.
CLASS="runtime"
if [ $RC -eq 0 ] && [ ! -s "$OUT" ]; then
  # Process reported success but wrote nothing (or wrote nothing NEW) -- runtime/model
  # oddity, not an auth signal. Do not let an empty-output case get mislabeled as auth.
  CLASS="runtime"
elif printf '%s' "$ERR" | grep -qiE \
    '\b(401|403)\b|unauthorized|not authenticated|not logged in|please (log|sign) in|login (required|expired)|token (expired|invalid|revoked)|please run .?codex login|auth(entication)? (failed|error)|invalid api.?key'; then
  CLASS="auth"
elif printf '%s' "$ERR" | grep -qiE \
    'not supported|invalid_request_error|model_not_found|unsupported model|unknown model'; then
  CLASS="model"
else
  # Non-zero rc with no auth/model signature: CLI crash, network/timeout, sandbox
  # denial, etc. Treat as runtime, NOT auth -- an unrecognized failure must never be
  # reported to operators as "needs re-login" without real evidence.
  CLASS="runtime"
fi

# --- SONNET FALLBACK (Joris 2026-09-21, card #1435) ---------------------------
# When Codex is unavailable (e.g. the ChatGPT account is on the free tier so paid
# models 400 -> CLASS=model; or an auth/runtime gap), draft via Claude Sonnet so
# writing never halts. This must be BOTH channel-clean AND authed:
#   * channel-clean: run in an ISOLATED CLAUDE_CONFIG_DIR with no plugins installed,
#     so NO telegram poller boots -> can never collide with Morty's bot token (#1406).
#     --strict-mcp-config also prevents project MCP servers from starting.
#     (`--bare` would skip plugins too, but it ALSO strips auth -> "Not logged in".)
#   * authed: prefer $CLAUDE_CODE_OAUTH_TOKEN from the env; else copy the caller's
#     maintained .credentials.json from CLAUDE_CONFIG_DIR (else $HOME/.claude)
#     into the isolated dir. On a keychain-only
#     Mac neither is available -> the fallback cleanly no-ops to the exit-3 contract
#     below (interactive agent callers draft directly anyway).
# ADDITIVE + guarded: accept ONLY a real draft (rc=0, non-empty, and NOT an auth/login
# error stub). Anything else falls through to the historical exit-3 FAIL-SOFT.
# Failed/partial Codex output is not a draft, even if no fallback can run.
rm -f "$OUT"
if command -v claude >/dev/null 2>&1; then
  AUTHCFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  FBCFG="$(mktemp -d 2>/dev/null)" || FBCFG=""
  if [ -n "$FBCFG" ]; then
    printf '{"hasCompletedOnboarding":true,"hasTrustDialogAccepted":true,"bypassPermissionsModeAccepted":true}' > "$FBCFG/.claude.json" 2>/dev/null
    if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -f "$AUTHCFG/.credentials.json" ]; then
      cp "$AUTHCFG/.credentials.json" "$FBCFG/.credentials.json" 2>/dev/null || true
    fi
    rm -f "$OUT"   # never mistake a stale/partial Codex $OUT for the Sonnet draft
    # Open the brief before cd so relative paths still refer to the caller's cwd.
    FBOUT="$( { cd "$REPO" && CLAUDE_CONFIG_DIR="$FBCFG" claude -p \
        "Draft text strictly per the following brief. Output ONLY the finished draft text — no preamble, no commentary." \
        --model sonnet --strict-mcp-config --dangerously-skip-permissions; } < "$BRIEF" 2>/dev/null )"
    FBRC=$?
    rm -rf "$FBCFG"
    if [ "$FBRC" -eq 0 ] && [ -n "$FBOUT" ] && \
       ! printf '%s' "$FBOUT" | grep -qiE 'not logged in|please run|/login|unauthorized|not authenticated|invalid api|api key|login (required|expired)'; then
      printf '%s' "$FBOUT" > "$OUT"
      if [ -s "$OUT" ]; then
        echo "codex_write: OK (SONNET FALLBACK; codex unavailable, CLASS=$CLASS) -> $OUT" >&2
        echo "codex_write: CLASSIFICATION=$CLASS" >&2
        exit 0
      fi
    fi
    rm -f "$OUT"   # Sonnet unavailable/unauthed here -> keep $OUT absent for the FAIL-SOFT contract
  fi
fi

echo "codex_write: FAIL-SOFT codex call failed (rc=$RC) and Sonnet fallback unavailable. Draft directly + flag operators." >&2
echo "codex_write: CLASSIFICATION=$CLASS" >&2
printf '%s\n' "$ERR" | tail -5 >&2
exit 3
