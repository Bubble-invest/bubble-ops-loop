#!/bin/bash
# bubble-gh-credential-helper.sh — git credential helper that mints a
# short-lived GitHub App installation token. Used by Morty + all VPS depts to
# push to vdk888/* and Bubble-invest/* repos without an SSH key or PAT.
#
# Wire via: git config --global credential.https://github.com.helper "/usr/local/bin/bubble-gh-credential-helper.sh"
#
# Git calls this with "get" on the stdin protocol; we echo back username + password.
#
# ===================================================================
# MISSION-FILE LOCK (governance fix 2026-06-01, {{OPERATOR}} msg 3582):
#   Agents must NOT be able to push to their own mission-definition files
#   (dept.yaml, layers/**, missions/**, skills/**, tools/**, subagents/**,
#   policies/**, templates/**, .claude/agents/**, .claude/settings.json,
#   .claude/CLAUDE.md — the STRUCTURAL_PATH_GLOBS in the broker policy).
#   Those may only change via a PR a human ({{OPERATOR}}/{{OPERATOR_2}}) merges.
#
#   THIS helper is the SOLE source of GitHub tokens on the box and runs as
#   ROOT (via sudo NOPASSWD) with cwd = the repo being pushed. So it is the
#   one chokepoint every push must pass. Before minting, it asks
#   is-structural-push.py whether the un-pushed delta touches a structural
#   path. If yes -> it mints a READ-ONLY token (contents:read), so the agent's
#   `git push` of a mission file FAILS at the auth layer (GitHub 403). Normal
#   runtime pushes (outputs/**, queues/**, inbox/**) are non-structural -> a
#   contents:write token is minted exactly as before (fast path, unchanged).
#
#   Unbypassable by the agent: it cannot edit this root-owned helper, cannot
#   --no-verify around an auth decision, and has NO other GitHub token source.
#   See ~/.claude/agent-memory/rnd/reference_mission_file_lock_gap.md.
# ===================================================================
#
# ===================================================================
# AGENT CALLERS ARE READ-ONLY (#1619 step 2, Joris tg 10208 "option A")
#   When sudo'd by a dept OS user (SUDO_USER=agent-<slug>) this helper mints
#   contents:READ (+ pull_requests:write, which `bubble-gh pr create` in
#   propose-settings-pr needs) scoped to the ONE repo in git's path= line. A
#   dept's runtime push goes through `bubble-git-guard push` -> the sudo broker
#   shim -> bubble-broker-mint-settings-root.sh, which enforces the path
#   policy IN CODE; a raw `git push` with this helper's token gets a 403.
#   Chesterton: until now this helper was the SOLE write path and the only
#   mission-file check was the best-effort delta check below (still applied to
#   the exceptions). Exceptions (agent callers that KEEP contents:write):
#     * agent-morty (Hermes; no broker policy, no installation-id mapping yet);
#     * an agent's OWN vault repo bubble-<slug>-vault (research notes, no
#       mission files; maya's policy declares no vault write rule) - the token
#       is scoped to that single repo.
#   claude / root callers are unchanged (cron + Rick flows; separate sudoers).
#   A call with no path= (no repo to scope) gets read-only too, never a
#   repo-wide write. Staged rollout: see /etc/bubble/cred-readonly-agents below.
# ===================================================================
#
# Secret-leak hardening (2026-05-29 Eliot audit, Rick impl):
#   - The GitHub App private key is decrypted to a tmpfs file (/run/lock or
#     /dev/shm), NEVER to /tmp on the persistent disk. tmpfs = RAM-only.
#   - `trap cleanup EXIT INT TERM` shred-removes the key on every exit path,
#     so it does not persist 300s and cannot be orphaned by a crash.
#   - Uses `--output FILE` (the sops-guard wrapper BLOCKS decrypt-to-stdout, so
#     a process-substitution pipe is not an option here).
#   - umask 077 + the tmpfs file is created 0600 by sops --output.

ACTION="${1:-get}"
if [[ "$ACTION" != "get" ]]; then
  exit 0   # store / erase / etc. — no-op
fi

set -o pipefail
umask 077

SOPS_PEM=/srv/bubble-secrets/github-app-bubble-ops-bot.private-key.sops.pem

# --- Capture cwd BEFORE anything changes it. cwd = the repo git is pushing.
REPO_DIR="$(pwd)"

# --- Read git's stdin protocol (path=org/repo.git, etc.) into a buffer so we
#     can both pick the installation id AND keep behaviour identical.
STDIN_BUF="$(cat)"

# tmpfs working file — RAM-only, shredded on exit.
PEM=$(mktemp --tmpdir=/run/lock bubble-gh-key.XXXXXX 2>/dev/null) \
  || PEM=$(mktemp --tmpdir=/dev/shm bubble-gh-key.XXXXXX) || exit 0
cleanup() { [[ -f "$PEM" ]] && { shred -u "$PEM" 2>/dev/null || rm -f "$PEM"; }; }
trap cleanup EXIT INT TERM

# Decrypt the App private key into the tmpfs file (respects sops-guard: --output).
SOPS_AGE_KEY_FILE=/etc/age/key.txt /usr/local/bin/sops --decrypt --output "$PEM" "$SOPS_PEM" 2>/dev/null
chmod 600 "$PEM"
[[ -s "$PEM" ]] || exit 0   # decrypt failed — emit nothing, let git fall through

# Determine which installation to use based on git's input (path=org/repo).
# vdk888 install (134075326) covers vdk888/* ; Bubble-invest install (135214360) covers Bubble-invest/*.
INST_ID=134075326
while IFS='=' read -r key value; do
  if [[ "$key" == "path" ]]; then
    case "$value" in
      Bubble-invest/*) INST_ID=135214360 ;;
      vdk888/*)        INST_ID=134075326 ;;
    esac
  fi
done <<< "$STDIN_BUF"

# --- Who is calling? sudo sets SUDO_USER (we are root); never trusted from argv.
AGENT_SLUG=""
if [[ "${SUDO_USER:-}" =~ ^agent-([a-z0-9][a-z0-9_-]*)$ ]]; then
  AGENT_SLUG="${BASH_REMATCH[1]}"
fi
REPO_NAME=""
while IFS='=' read -r key value; do
  if [[ "$key" == "path" && "$value" =~ ^[A-Za-z0-9_.-]+/([A-Za-z0-9_.-]+)$ ]]; then
    REPO_NAME="${BASH_REMATCH[1]%.git}"
  fi
done <<< "$STDIN_BUF"

# --- MISSION-FILE LOCK: decide token permission class.
# Default = today's behaviour (contents:write). Downgrade to read-only ONLY if
# the un-pushed delta in $REPO_DIR positively touches a structural path.
PERMS='{"contents":"write","metadata":"read","pull_requests":"write"}'
STRUCTURAL_CHECK=/usr/local/bin/bubble-is-structural-push.py
if [[ -x "$STRUCTURAL_CHECK" ]] || [[ -f "$STRUCTURAL_CHECK" ]]; then
  if python3 "$STRUCTURAL_CHECK" --repo-dir "$REPO_DIR" >/dev/null 2>&1; then
    # exit 0 = structural path detected -> mint READ-ONLY so the push fails.
    PERMS='{"contents":"read","metadata":"read","pull_requests":"write"}'
    # Audit (metadata only, NEVER a token). Best-effort; never block on logging.
    logger -t bubble-gh-cred "mission-file-lock: structural push from ${REPO_DIR} -> read-only token" 2>/dev/null || true
  fi
fi

# --- #1619 step 2: agent callers are read-only (see the header block).
# Staged rollout switch (root-owned, outside agent reach): if
# /etc/bubble/cred-readonly-agents exists, ONLY the slugs listed in it (one per
# line) are switched; if it is absent EVERY agent-* (except morty) is. Rollout:
# create it with "tony", add ben, maya, then delete it. Fail direction = stricter.
READONLY_AGENTS_FILE=/etc/bubble/cred-readonly-agents
AGENT_RO=0
if [[ -n "$AGENT_SLUG" && "$AGENT_SLUG" != "morty" ]]; then
  if [[ ! -f "$READONLY_AGENTS_FILE" ]] || grep -qxF -- "$AGENT_SLUG" "$READONLY_AGENTS_FILE" 2>/dev/null; then
    AGENT_RO=1
  fi
fi
REPO_SCOPE=""
if [[ "$AGENT_RO" == 1 ]]; then
  if [[ -n "$REPO_NAME" ]]; then
    REPO_SCOPE=",\"repositories\":[\"$REPO_NAME\"]"
  fi
  if [[ "$REPO_NAME" != "bubble-${AGENT_SLUG}-vault" ]]; then
    PERMS='{"contents":"read","metadata":"read","pull_requests":"write"}'
    logger -t bubble-gh-cred "agent-readonly: caller=${SUDO_USER} repo=${REPO_NAME:-none} -> contents:read" 2>/dev/null || true
  fi
fi

APP_ID=3782718
NOW=$(date +%s)
HEADER=$(echo -n '{"alg":"RS256","typ":"JWT"}' | openssl base64 -e -A | tr -- '+/' '-_' | tr -d '=')
PAYLOAD=$(echo -n "{\"iat\":$((NOW-60)),\"exp\":$((NOW+540)),\"iss\":$APP_ID}" | openssl base64 -e -A | tr -- '+/' '-_' | tr -d '=')
SIG=$(echo -n "$HEADER.$PAYLOAD" | openssl dgst -sha256 -sign "$PEM" | openssl base64 -e -A | tr -- '+/' '-_' | tr -d '=')
JWT="$HEADER.$PAYLOAD.$SIG"
TOKEN_JSON=$(curl -s -X POST -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" -H "Content-Type: application/json" -d "{\"permissions\":$PERMS$REPO_SCOPE}" "https://api.github.com/app/installations/$INST_ID/access_tokens" 2>/dev/null)
TOKEN=$(echo "$TOKEN_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)

if [[ -n "$TOKEN" && "$TOKEN" == ghs_* ]]; then
  echo "username=x-access-token"
  echo "password=$TOKEN"
fi
