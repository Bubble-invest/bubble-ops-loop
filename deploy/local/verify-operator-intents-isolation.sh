#!/bin/bash
# Read-only preflight: proves filesystem isolation and checks GitHub permission.
set -Eeuo pipefail
MIRROR='/Library/Application Support/Bubble/operator-intents'
KEY='/Library/Application Support/Bubble/secrets/operator-intents-readonly-deploy-key'
SYNC='/Library/Application Support/Bubble/bin/operator-intents-mirror-sync'
REPO='vdk888/bubble-operator-intents'
USERS=()
CREDENTIAL_ONLY=0
die() { printf 'verify-operator-intents-isolation: FAIL: %s\n' "$*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent-user) USERS+=("${2:?}"); shift 2 ;;
    --mirror) MIRROR="${2:?}"; shift 2 ;;
    --key) KEY="${2:?}"; shift 2 ;;
    --sync) SYNC="${2:?}"; shift 2 ;;
    --credential-only) CREDENTIAL_ONLY=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
(( ${#USERS[@]} > 0 )) || die 'at least one --agent-user is required'
[[ "$(stat -f '%Su:%Sg:%Lp' "$KEY")" == 'root:wheel:400' ]] || die 'key is not root:wheel 0400'
[[ "$(stat -f '%Su:%Sg:%Lp' "$SYNC")" == 'root:wheel:755' ]] || die 'sync executable is not root:wheel 0755'
grep -Fq "git@github.com:$REPO.git" "$SYNC" || die 'sync remote is not the exact SSH vault remote'
if grep -Eq 'https://[^/[:space:]]+@github\.com|GITHUB_TOKEN|GH_TOKEN|credential\.helper|git push' "$SYNC"; then
  die 'sync contains a credential-bearing or write transport'
fi

console_uid="$(stat -f '%u' /dev/console 2>/dev/null || true)"
console_user="$(stat -f '%Su' /dev/console 2>/dev/null || true)"
[[ "$console_uid" =~ ^[0-9]+$ && "$console_uid" -gt 0 && -n "$console_user" ]] \
  || die 'interactive console operator identity is unavailable; cannot prove UID separation'
operator_home="$(dscl . -read "/Users/$console_user" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
[[ "$operator_home" == /* && -d "$operator_home" ]] || die 'operator home could not be resolved'

probe_push_denied() {
  local user="$1" remote="$2" label="$3" probe_output
  if probe_output="$(sudo -n -H -u "$user" /bin/bash -s -- "$remote" <<'SH' 2>&1
set -euo pipefail
export GIT_TERMINAL_PROMPT=0
probe="$(mktemp -d "${TMPDIR:-/tmp}/intent-push-probe.XXXXXX")"
trap 'rm -rf -- "$probe"' EXIT
/usr/bin/git -C "$probe" init -q
/usr/bin/git -C "$probe" config user.name mirror-preflight
/usr/bin/git -C "$probe" config user.email mirror-preflight@invalid
/usr/bin/git -C "$probe" commit --allow-empty -qm probe
set +e
output="$(/usr/bin/git -C "$probe" push --dry-run "$1" HEAD:refs/heads/operator-intents-preflight-deny 2>&1)"
rc=$?
set -e
if [[ "$rc" == 0 ]]; then
  echo 'remote accepted push dry-run' >&2
  exit 10
fi
if grep -Eqi 'permission denied|denied to|repository not found|write access.+not granted|could not read Username|HTTP 403|publickey' <<<"$output"; then
  exit 0
fi
printf 'inconclusive push denial: %s\n' "$output" >&2
exit 11
SH
)"; then
    return 0
  fi
  die "$user $label push dry-run was allowed or inconclusive: $probe_output"
}

for user in "${USERS[@]}"; do
  id "$user" >/dev/null 2>&1 || die "unknown agent user: $user"
  agent_uid="$(id -u "$user")"
  [[ "$agent_uid" -gt 0 && "$agent_uid" != "$console_uid" ]] \
    || die "$user must have a distinct non-root UID from interactive operator $console_user"
  sudo -n -H -u "$user" test ! -r "$KEY" || die "$user can read deploy key"
  credential_paths=(
    "$operator_home/.config/gh/hosts.yml"
    "$operator_home/.git-credentials"
    "$operator_home/Library/Application Support/GitHub CLI/hosts.yml"
  )
  while IFS= read -r credential; do credential_paths+=("$credential"); done \
    < <(find "$operator_home/.ssh" -maxdepth 1 -type f \( -name 'id_*' -o -name 'config' \) 2>/dev/null || true)
  for credential in "${credential_paths[@]}"; do
    [[ -e "$credential" ]] || continue
    sudo -n -H -u "$user" test ! -r "$credential" \
      || die "$user can read interactive operator credential store: $credential"
  done
  query='query($owner:String!,$name:String!){repository(owner:$owner,name:$name){viewerPermission}}'
  api_output="$(sudo -n -H -u "$user" gh api graphql -f "query=$query" -F owner=vdk888 -F name=bubble-operator-intents --jq '.data.repository.viewerPermission // "NONE"' 2>&1)" \
    || die "$user vault GraphQL viewerPermission query was inconclusive: $api_output"
  permission="$api_output"
  case "$permission" in
    READ|NONE) ;;
    ADMIN|MAINTAIN|WRITE) die "$user has forbidden vault permission: $permission" ;;
    '') die "$user vault permission was empty" ;;
    *) die "$user returned unexpected vault permission: $permission" ;;
  esac
  probe_push_denied "$user" "https://github.com/$REPO.git" HTTPS
  probe_push_denied "$user" "git@github.com:$REPO.git" SSH
  if (( CREDENTIAL_ONLY == 0 )); then
    [[ -L "$MIRROR" && -d "$MIRROR/operator-intents" ]] || die 'stable mirror is absent or not a symlink'
    [[ "$(stat -f '%Su:%Sg' "$MIRROR")" == 'root:wheel' ]] || die 'stable mirror symlink is not root:wheel'
    [[ -z "$(find -L "$MIRROR" -type d ! -perm 0555 -print -quit)" ]] || die 'mirror has a directory not mode 0555'
    [[ -z "$(find -L "$MIRROR" -type f ! -perm 0444 -print -quit)" ]] || die 'mirror has a file not mode 0444'
    [[ -z "$(find "$MIRROR/operator-intents" -type l -print -quit)" ]] || die 'mirror content contains a symlink'
    [[ -z "$(find -L "$MIRROR" \( ! -user root -o ! -group wheel \) -print -quit)" ]] || die 'mirror content is not root:wheel'
    sudo -n -H -u "$user" test -r "$MIRROR/operator-intents" || die "$user cannot read mirror"
    sudo -n -H -u "$user" test ! -w "$MIRROR" || die "$user can write stable mirror"
    sudo -n -H -u "$user" test ! -w "$MIRROR/operator-intents" || die "$user can create/remove mirror content"
    sudo -n -H -u "$user" test ! -O "$MIRROR/operator-intents" || die "$user owns mirror content and could chmod it"
  fi
  printf 'verify-operator-intents-isolation: PASS user=%s viewerPermission=%s\n' "$user" "$permission"
done
printf '%s\n' 'verify-operator-intents-isolation: HUMAN ATTESTATION STILL REQUIRED: GitHub deploy-key setting “Allow write access” is disabled.'
