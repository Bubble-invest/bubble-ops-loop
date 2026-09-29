#!/usr/bin/env bash
# Mint one repository-scoped, read-only GitHub App token per isolated dept.
#
# Dept Claude Code sessions run with NoNewPrivileges, so their normal sudo
# credential helper cannot mint at git-fetch time.  This root timer writes the
# same short-lived credential into tmpfs ahead of time, readable only by that
# dept's own agent-<slug> group.  Push remains exclusively governed by
# bubble-git-guard and its separately minted write token.

set -uo pipefail

SUDOERS_DIR="${BUBBLE_GIT_READ_TOKEN_SUDOERS_DIR:-/etc/sudoers.d}"
SUDOERS_PREFIX="bubble-board-token-agent-"
AGENTS_ROOT="${BUBBLE_GIT_READ_TOKEN_AGENTS_ROOT:-/srv/agents}"
DEST_DIR="${BUBBLE_GIT_READ_TOKEN_RUN_DIR:-/run/bubble-git}"
TMPFS_DIR="${BUBBLE_GIT_READ_TOKEN_TMPFS_DIR:-/run/lock}"

APP_ID=3782718
INST_ID=135214360
SOPS_PEM="${BUBBLE_GIT_READ_TOKEN_PEM_ENC:-/srv/bubble-secrets/github-app-bubble-ops-bot.private-key.sops.pem}"
AGE_KEY="${BUBBLE_GIT_READ_TOKEN_AGE_KEY_FILE:-/etc/age/key.txt}"

log_failure() {
  printf 'bubble-git-read-token-refresh: %s\n' "$*" >&2
}

_dept_is_authorized() {
  [ -f "${SUDOERS_DIR}/${SUDOERS_PREFIX}${1}" ]
}

# Mirror bubble-board-token-refresh.sh: the sudoers grant files are the
# production roster.  The override is a roster-less test/dev escape valve,
# never a way to add a dept when real grant files are present.
ROSTER_DEPTS=""
for _f in "$SUDOERS_DIR"/"${SUDOERS_PREFIX}"*; do
  [ -f "$_f" ] || continue
  _roster_basename=${_f##*/}
  ROSTER_DEPTS="${ROSTER_DEPTS} ${_roster_basename#"$SUDOERS_PREFIX"}"
done

if [ -n "$ROSTER_DEPTS" ]; then
  ROSTER_PRESENT=1
  CANDIDATE_DEPTS="${ROSTER_DEPTS} ${BUBBLE_GIT_READ_TOKEN_DEPTS:-}"
else
  ROSTER_PRESENT=0
  CANDIDATE_DEPTS="${BUBBLE_GIT_READ_TOKEN_DEPTS:-}"
fi

# A dept may appear through both the real roster and the test override.  Keep
# this Bash-3-compatible because the repository's tests also run on macOS.
DEPT_LIST="$(printf '%s\n' "$CANDIDATE_DEPTS" | tr '[:space:]' '\n' | \
  awk 'NF && !seen[$0]++' | tr '\n' ' ')"

# /run is tmpfs in production.  Mode 0711 permits a dept uid to traverse to
# its exact token filename without granting directory listing/read access.
if ! install -d -m 0711 -o root -g root "$DEST_DIR"; then
  log_failure "cannot prepare token directory"
  exit 1
fi

PEM=""
# Called indirectly by the trap below.
# shellcheck disable=SC2329
cleanup() {
  _cleanup_status=$?
  if [ -n "$PEM" ] && [ -f "$PEM" ]; then
    shred -u -- "$PEM" 2>/dev/null || rm -f -- "$PEM"
  fi
  return "$_cleanup_status"
}
trap cleanup EXIT
trap 'exit 1' INT TERM

if ! PEM=$(mktemp "${TMPFS_DIR%/}/bubble-git-read-key.XXXXXX" 2>/dev/null); then
  # Production fallback if /run/lock is unavailable; still RAM-backed.
  if [ "$TMPFS_DIR" != "/run/lock" ] || \
     ! PEM=$(mktemp "/dev/shm/bubble-git-read-key.XXXXXX" 2>/dev/null); then
    log_failure "cannot create tmpfs key file"
    exit 1
  fi
fi
chmod 0600 "$PEM" 2>/dev/null || {
  log_failure "cannot secure tmpfs key file"
  exit 1
}

if ! SOPS_AGE_KEY_FILE="$AGE_KEY" sops --decrypt \
    --input-type binary --output-type binary --output "$PEM" "$SOPS_PEM" \
    2>/dev/null || [ ! -s "$PEM" ]; then
  log_failure "GitHub App key decrypt failed"
  exit 1
fi

b64url() {
  openssl base64 -e -A | tr -- '+/' '-_' | tr -d '='
}

NOW=$(date +%s)
if ! HEADER=$(printf '%s' '{"alg":"RS256","typ":"JWT"}' | b64url) ||
   ! PAYLOAD=$(printf '{"iat":%d,"exp":%d,"iss":%d}' \
       "$((NOW-60))" "$((NOW+540))" "$APP_ID" | b64url) ||
   ! SIGNATURE=$(printf '%s' "$HEADER.$PAYLOAD" | \
       openssl dgst -sha256 -sign "$PEM" -binary 2>/dev/null | b64url) ||
   [ -z "$HEADER" ] || [ -z "$PAYLOAD" ] || [ -z "$SIGNATURE" ]; then
  log_failure "GitHub App JWT generation failed"
  exit 1
fi
JWT="$HEADER.$PAYLOAD.$SIGNATURE"

failures=0
umask 027

# DEPT_LIST is generated exclusively from one-slug-per-word roster inputs.
# shellcheck disable=SC2086
for _dept in $DEPT_LIST; do
  # Reject unsafe roster suffixes before using one in a username or path.
  if [[ ! "$_dept" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    log_failure "dept roster entry is invalid; skipped"
    failures=$((failures + 1))
    continue
  fi

  if [ "$ROSTER_PRESENT" -eq 1 ] && ! _dept_is_authorized "$_dept"; then
    log_failure "skip '${_dept}': no matching sudoers grant"
    continue
  fi

  _agent="agent-${_dept}"
  _repo_dir="${AGENTS_ROOT%/}/${_dept}"
  if ! _remote=$(runuser -u "$_agent" -- \
      git -C "$_repo_dir" remote get-url origin 2>/dev/null); then
    log_failure "${_dept}: cannot resolve origin as ${_agent}"
    failures=$((failures + 1))
    continue
  fi

  _repo_path=""
  case "$_remote" in
    https://github.com/Bubble-invest/*)
      _repo_path=${_remote#https://github.com/Bubble-invest/}
      _repo=${_repo_path%.git}
      ;;
    *)
      # Not a Bubble-invest HTTPS checkout (e.g. an SSH deploy-key remote):
      # this dept does not use the helper for its origin. Skip, not a failure.
      printf 'bubble-git-read-token-refresh: %s: origin not Bubble-invest HTTPS, skipped\n' "$_dept" >&2
      continue
      ;;
  esac
  if [ -z "$_repo" ] || [ "$_repo" = "." ] || [ "$_repo" = ".." ] ||
     [[ "$_repo" == */* ]] || [[ ! "$_repo" =~ ^[A-Za-z0-9._-]+$ ]] ||
     { [ "$_repo_path" != "$_repo" ] && [ "$_repo_path" != "${_repo}.git" ]; } ||
     [ "$_repo" != "bubble-ops-${_dept}" ]; then
    # Pinned to the dept's own repo: origin is agent-writable, so a dept
    # re-pointing it at another dept's repo must NOT get a cross-dept token.
    log_failure "${_dept}: origin is not an accepted Bubble-invest HTTPS repository"
    failures=$((failures + 1))
    continue
  fi

  _request=$(printf \
    '{"permissions":{"contents":"read","metadata":"read"},"repositories":["%s"]}' \
    "$_repo")
  if ! _response=$(curl -s -X POST \
      -H "Authorization: Bearer $JWT" \
      -H "Accept: application/vnd.github+json" \
      -H "Content-Type: application/json" \
      -d "$_request" \
      "https://api.github.com/app/installations/$INST_ID/access_tokens" \
      2>/dev/null); then
    log_failure "${_dept}: GitHub token request failed"
    failures=$((failures + 1))
    continue
  fi
  _token=$(printf '%s' "$_response" | \
    python3 -c 'import json,sys; print(json.load(sys.stdin).get("token", ""))' \
    2>/dev/null || true)
  case "$_token" in
    ghs_*) ;;
    *)
      log_failure "${_dept}: GitHub returned no valid token"
      failures=$((failures + 1))
      continue
      ;;
  esac

  _dest="${DEST_DIR}/token.${_dept}"
  _tmp="${_dest}.tmp"
  if ! printf '%s' "$_token" > "$_tmp" ||
     ! chown "root:${_agent}" "$_tmp" ||
     ! chmod 0640 "$_tmp" ||
     ! mv -f "$_tmp" "$_dest"; then
    rm -f -- "$_tmp"
    log_failure "${_dept}: cannot atomically install token file"
    failures=$((failures + 1))
    continue
  fi
done

if [ "$failures" -ne 0 ]; then
  log_failure "completed with ${failures} department failure(s)"
  exit 1
fi

exit 0
