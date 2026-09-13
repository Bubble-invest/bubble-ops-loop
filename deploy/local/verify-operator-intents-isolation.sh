#!/bin/bash
# Read-only preflight: proves filesystem isolation and checks GitHub permission.
set -Eeuo pipefail
MIRROR='/Library/Application Support/Bubble/operator-intents'
KEY='/Library/Application Support/Bubble/secrets/operator-intents-readonly-deploy-key'
SYNC='/Library/Application Support/Bubble/bin/operator-intents-mirror-sync'
REPO='vdk888/bubble-operator-intents'
USERS=()
die() { printf 'verify-operator-intents-isolation: FAIL: %s\n' "$*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent-user) USERS+=("${2:?}"); shift 2 ;;
    --mirror) MIRROR="${2:?}"; shift 2 ;;
    --key) KEY="${2:?}"; shift 2 ;;
    --sync) SYNC="${2:?}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
(( ${#USERS[@]} > 0 )) || die 'at least one --agent-user is required'
[[ -L "$MIRROR" && -d "$MIRROR/operator-intents" ]] || die 'stable mirror is absent or not a symlink'
[[ "$(stat -f '%Su:%Sg' "$MIRROR")" == 'root:wheel' ]] || die 'stable mirror symlink is not root:wheel'
[[ "$(stat -f '%Su:%Sg:%Lp' "$KEY")" == 'root:wheel:400' ]] || die 'key is not root:wheel 0400'
[[ "$(stat -f '%Su:%Sg:%Lp' "$SYNC")" == 'root:wheel:755' ]] || die 'sync executable is not root:wheel 0755'
[[ -z "$(find -L "$MIRROR" -type d ! -perm 0555 -print -quit)" ]] || die 'mirror has a directory not mode 0555'
[[ -z "$(find -L "$MIRROR" -type f ! -perm 0444 -print -quit)" ]] || die 'mirror has a file not mode 0444'
[[ -z "$(find "$MIRROR/operator-intents" -type l -print -quit)" ]] || die 'mirror content contains a symlink'
[[ -z "$(find -L "$MIRROR" \( ! -user root -o ! -group wheel \) -print -quit)" ]] || die 'mirror content is not root:wheel'
grep -Fq "git@github.com:$REPO.git" "$SYNC" || die 'sync remote is not the exact SSH vault remote'
if grep -Eq 'https://[^/[:space:]]+@github\.com|GITHUB_TOKEN|GH_TOKEN|credential\.helper|git push' "$SYNC"; then
  die 'sync contains a credential-bearing or write transport'
fi

for user in "${USERS[@]}"; do
  id "$user" >/dev/null 2>&1 || die "unknown agent user: $user"
  sudo -n -H -u "$user" test -r "$MIRROR/operator-intents" || die "$user cannot read mirror"
  sudo -n -H -u "$user" test ! -w "$MIRROR" || die "$user can write stable mirror"
  sudo -n -H -u "$user" test ! -w "$MIRROR/operator-intents" || die "$user can create/remove mirror content"
  sudo -n -H -u "$user" test ! -O "$MIRROR/operator-intents" || die "$user owns mirror content and could chmod it"
  sudo -n -H -u "$user" test ! -r "$KEY" || die "$user can read deploy key"
  if api_output="$(sudo -n -H -u "$user" gh api "repos/$REPO" --jq .viewerPermission 2>&1)"; then
    permission="$api_output"
  elif grep -Eqi 'HTTP 404|not found' <<<"$api_output"; then
    permission=NONE
  else
    die "$user vault permission could not be verified: $api_output"
  fi
  case "$permission" in
    READ|NONE) ;;
    ADMIN|MAINTAIN|WRITE) die "$user has forbidden vault permission: $permission" ;;
    '') die "$user vault permission was empty" ;;
    *) die "$user returned unexpected vault permission: $permission" ;;
  esac
  printf 'verify-operator-intents-isolation: PASS user=%s viewerPermission=%s\n' "$user" "$permission"
done
