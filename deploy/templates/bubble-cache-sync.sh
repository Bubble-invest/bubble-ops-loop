#!/usr/bin/env bash
# bubble-cache-sync.sh — Sync /srv/bubble-ops/repos/ cache.
#
# For each bubble-ops-<slug> repo visible to the bubble-ops-bot GitHub App,
# clone it (if absent) or fetch+reset (if present), using short-lived tokens
# minted by bubble-token-broker.
#
# SECURITY:
#   - Token captured into local var; NEVER echo'd, NEVER set -x.
#   - Token section runs without set -x to avoid accidental leak.
#   - board #923 (same class as #921/PR#310): the token travels ONLY via a
#     GIT_CONFIG_* extraHeader env-var triad passed to the specific `git
#     clone`/`git fetch` invocation — NEVER embedded in the remote URL /
#     argv (subprocess argv lands in `ps`/transcripts; the old
#     `x-access-token:$TOKEN@github.com/...` URL form does not). Remote
#     `origin` is always the clean, token-free URL.
#   - Token var(s) unset immediately after use.
#
# Idempotent: re-running is a no-op (fetch --depth 1 + reset --hard).
#
# Requires:
#   BUBBLE_BROKER_PEM_PATH — path to pre-decrypted GitHub App PEM
#   GITHUB_APP_ID          — GitHub App numeric ID
#   GITHUB_APP_INSTALLATION_ID_BUBBLE_OPS_FIXTURE — installation ID for fixture
#   (Loaded from /run/claude-agent-fixture/env or passed via systemd EnvironmentFile)

set -uo pipefail

CACHE_DIR=/srv/bubble-ops/repos
LOG_DIR=/var/log/bubble-cache-sync
LOG_FILE="${LOG_DIR}/sync-$(date -u +%Y-%m-%d).log"
BROKER=/opt/bubble-token-broker/bin/bubble-token-broker
GITHUB_ORG=vdk888

# Repos to sync — seed with fixture; script will be extended for new depts.
# These are the bubble-ops-<slug> repo names.
REPOS=(
  bubble-ops-fixture
)

log() {
  local ts
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "${ts} $*" | tee -a "${LOG_FILE}"
}

# board #923 (same class as #921/PR#310): run `git "$@"` with the token
# ($1 — already the base64-encoded "x-access-token:<token>" Basic-auth
# value) injected via a GIT_CONFIG_* extraHeader env triad passed only to
# THIS invocation — never argv, never written to any .git/config on disk.
# Also neutralizes credential.helper so no ambient helper chain can
# intercept/duplicate the auth attempt. Mirrors
# scripts/lib/dispatch_helpers.py's _env_with_bearer_auth_header (PR#310).
# NOTE: this script never runs under `set -x` (see SECURITY header above),
# so these env values are never echoed by tracing either.
_git_authed() {
  local b64="$1"; shift
  GIT_CONFIG_COUNT=2 \
  GIT_CONFIG_KEY_0=credential.helper \
  GIT_CONFIG_VALUE_0= \
  GIT_CONFIG_KEY_1='http.https://github.com/.extraHeader' \
  GIT_CONFIG_VALUE_1="Authorization: Basic ${b64}" \
  git "$@"
}

# Count every dirty path, including untracked files.  This intentionally
# mirrors scripts/sync-local-dept-clones.sh's any_dirt_count guard: checking
# only tracked changes misses scratch files and leaves reset/clean-style syncs
# one refactor away from silent data loss.
any_dirt_count() {
  local dir="$1"
  git -C "$dir" status --porcelain 2>/dev/null | grep -c '.' || true
}

# Quarantine all worktree dirt before reset --hard.  A failed stash is a hard
# stop for this repo: freshness can wait; an operator's hand-patch cannot be
# reconstructed after an unattended reset.
quarantine_endangered_state() {
  local dir="$1" slug="$2" dirt_n="$3" ts
  [[ "${dirt_n:-0}" -gt 0 ]] || return 0
  ts="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  if git -C "$dir" stash push --include-untracked \
      -m "cache-sync-quarantine-${ts}" \
      >>"${LOG_DIR}/git-${slug}.err" 2>&1; then
    log "WARN: ${slug} had ${dirt_n} dirty path(s); quarantined in git stash before reset --hard"
    return 0
  fi
  log "ERROR: ${slug} has ${dirt_n} dirty path(s) and quarantine FAILED; refusing reset --hard: $(tail -n1 "${LOG_DIR}/git-${slug}.err" 2>/dev/null)"
  return 1
}

log "bubble-cache-sync START"
log "cache_dir=${CACHE_DIR} repos=(${REPOS[*]})"

mkdir -p "${CACHE_DIR}"
chown claude:claude "${CACHE_DIR}" 2>/dev/null || true

EXIT_CODE=0

for REPO in "${REPOS[@]}"; do
  SLUG="${REPO#bubble-ops-}"
  REPO_DIR="${CACHE_DIR}/${REPO}"

  log "--- processing ${REPO} ---"

  # Mint a runtime_read token for this repo.
  # CRITICAL: do NOT enable set -x here — token must not appear in logs.
  {
    # Use no-sops mode if PEM is pre-decrypted (fixture service pattern)
    BROKER_OPTS="--dept fixture --action runtime_read --repo ${REPO}"
    if [[ -n "${BUBBLE_BROKER_PEM_PATH:-}" && -f "${BUBBLE_BROKER_PEM_PATH}" ]]; then
      BROKER_OPTS="${BROKER_OPTS} --no-sops --pem-path ${BUBBLE_BROKER_PEM_PATH}"
    fi

    # Capture token — never echo
    _TOKEN=$(${BROKER} mint ${BROKER_OPTS} 2>"${LOG_DIR}/broker-${SLUG}.err" || true)

    if [[ -z "${_TOKEN}" || "${_TOKEN}" != ghs_* ]]; then
      log "WARN: broker mint failed for ${REPO} (empty or non-ghs_ token), skipping"
      log "WARN: broker stderr: $(cat "${LOG_DIR}/broker-${SLUG}.err" 2>/dev/null | head -3)"
      rm -f "${LOG_DIR}/broker-${SLUG}.err"
      EXIT_CODE=1
      unset _TOKEN
      continue
    fi
    rm -f "${LOG_DIR}/broker-${SLUG}.err"

    # Use token for git operation — no echoing. #923: token travels via env
    # (GIT_CONFIG_* extraHeader, see _git_authed above), never in the URL.
    _AUTH_B64=$(printf 'x-access-token:%s' "${_TOKEN}" | base64 | tr -d '\n')

    if [[ -d "${REPO_DIR}/.git" ]]; then
      log "fetch: ${REPO_DIR} (already cloned)"
      # origin always stays the clean, token-free URL — never rewritten
      # with embedded credentials (belt-and-braces in case a prior run left
      # it pointed elsewhere).
      git -C "${REPO_DIR}" remote set-url origin \
        "https://github.com/${GITHUB_ORG}/${REPO}.git" \
        2>/dev/null
      if _git_authed "${_AUTH_B64}" -C "${REPO_DIR}" fetch --depth 1 origin main \
          2>"${LOG_DIR}/git-${SLUG}.err"; then
        dirt_n="$(any_dirt_count "${REPO_DIR}")"
        if ! quarantine_endangered_state "${REPO_DIR}" "${SLUG}" "${dirt_n}"; then
          EXIT_CODE=1
          unset dirt_n
          unset _AUTH_B64
          unset _TOKEN
          continue
        fi
        unset dirt_n
        if git -C "${REPO_DIR}" reset --hard origin/main \
            2>>"${LOG_DIR}/git-${SLUG}.err"; then
          log "OK: ${REPO} updated (fetch+reset)"
          rm -f "${LOG_DIR}/git-${SLUG}.err"
        else
          log "ERROR: git reset --hard failed for ${REPO}"
          log "ERROR: $(cat "${LOG_DIR}/git-${SLUG}.err" 2>/dev/null | head -3)"
          EXIT_CODE=1
        fi
      else
        log "ERROR: git fetch failed for ${REPO}"
        log "ERROR: $(cat "${LOG_DIR}/git-${SLUG}.err" 2>/dev/null | head -3)"
        EXIT_CODE=1
      fi
    else
      log "clone: ${REPO} → ${REPO_DIR}"
      mkdir -p "${REPO_DIR}"
      if _git_authed "${_AUTH_B64}" clone --depth 1 \
          "https://github.com/${GITHUB_ORG}/${REPO}.git" \
          "${REPO_DIR}" \
          2>"${LOG_DIR}/git-${SLUG}.err"; then
        log "OK: ${REPO} cloned"
        rm -f "${LOG_DIR}/git-${SLUG}.err"
      else
        log "ERROR: git clone failed for ${REPO}"
        log "ERROR: $(cat "${LOG_DIR}/git-${SLUG}.err" 2>/dev/null | head -3)"
        rm -rf "${REPO_DIR}" 2>/dev/null || true
        EXIT_CODE=1
      fi
    fi

    unset _AUTH_B64
    unset _TOKEN
  }
done

log "bubble-cache-sync END exit=${EXIT_CODE}"
exit "${EXIT_CODE}"
