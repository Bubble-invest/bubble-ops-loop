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
#   - Uses git clone https://x-access-token:$TOKEN@... pattern.
#   - Token var is unset immediately after use.
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

    # Use token for git operation — no echoing
    if [[ -d "${REPO_DIR}/.git" ]]; then
      log "fetch: ${REPO_DIR} (already cloned)"
      # Temporarily set remote URL with token, fetch, reset URL
      git -C "${REPO_DIR}" remote set-url origin \
        "https://x-access-token:${_TOKEN}@github.com/${GITHUB_ORG}/${REPO}.git" \
        2>/dev/null
      if git -C "${REPO_DIR}" fetch --depth 1 origin main \
          2>"${LOG_DIR}/git-${SLUG}.err"; then
        git -C "${REPO_DIR}" reset --hard origin/main 2>>"${LOG_DIR}/git-${SLUG}.err"
        # Reset remote URL to unauthenticated form (token expired anyway)
        git -C "${REPO_DIR}" remote set-url origin \
          "https://github.com/${GITHUB_ORG}/${REPO}.git" 2>/dev/null
        log "OK: ${REPO} updated (fetch+reset)"
        rm -f "${LOG_DIR}/git-${SLUG}.err"
      else
        log "ERROR: git fetch failed for ${REPO}"
        log "ERROR: $(cat "${LOG_DIR}/git-${SLUG}.err" 2>/dev/null | head -3)"
        git -C "${REPO_DIR}" remote set-url origin \
          "https://github.com/${GITHUB_ORG}/${REPO}.git" 2>/dev/null
        EXIT_CODE=1
      fi
    else
      log "clone: ${REPO} → ${REPO_DIR}"
      mkdir -p "${REPO_DIR}"
      if git clone --depth 1 \
          "https://x-access-token:${_TOKEN}@github.com/${GITHUB_ORG}/${REPO}.git" \
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

    unset _TOKEN
  }
done

log "bubble-cache-sync END exit=${EXIT_CODE}"
exit "${EXIT_CODE}"
