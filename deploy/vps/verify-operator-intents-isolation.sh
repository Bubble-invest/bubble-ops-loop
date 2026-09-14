#!/usr/bin/env bash
# Read-only preflight for the Linux operator-intents mirror and credential boundary.
set -Eeuo pipefail
MIRROR='/opt/bubble-operator-intents'
KEY='/etc/bubble/secrets/operator-intents-readonly-deploy-key'
SYNC='/usr/local/sbin/operator-intents-mirror-sync'
REPO='Bubble-invest/bubble-operator-intents'
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
[[ "$(stat -c '%U:%G:%a' "$KEY")" == 'root:root:400' ]] || die 'key is not root:root 0400'
[[ "$(stat -c '%U:%G:%a' "$SYNC")" == 'root:root:755' ]] || die 'sync executable is not root:root 0755'
grep -Fq "git@github.com:$REPO.git" "$SYNC" || die 'sync remote is not the exact SSH vault remote'
if grep -Eq 'https://[^/[:space:]]+@github\.com|GITHUB_TOKEN|GH_TOKEN|credential\.helper|git push' "$SYNC"; then
  die 'sync contains a credential-bearing or write transport'
fi

probe_push_denied() {
  local user="$1" remote="$2" label="$3" git_bin="$4" output
  if output="$(sudo -n -H -u "$user" /bin/bash -s -- "$remote" "$git_bin" <<'SH' 2>&1
set -euo pipefail
export GIT_TERMINAL_PROMPT=0
probe="$(mktemp -d "${TMPDIR:-/tmp}/intent-push-probe.XXXXXX")"
trap 'rm -rf -- "$probe"' EXIT
/usr/bin/git -C "$probe" init -q
/usr/bin/git -C "$probe" config user.name mirror-preflight
/usr/bin/git -C "$probe" config user.email mirror-preflight@invalid
/usr/bin/git -C "$probe" commit --allow-empty -qm probe
set +e
result="$("$2" -C "$probe" push --dry-run "$1" HEAD:refs/heads/operator-intents-preflight-deny 2>&1)"
rc=$?
set -e
if [[ "$rc" == 0 ]]; then
  echo 'remote accepted push dry-run' >&2
  exit 10
fi
if grep -Eqi 'permission denied|denied to|repository not found|write access.+not granted|could not read Username|HTTP 403|publickey|not have permission' <<<"$result"; then
  exit 0
fi
printf 'inconclusive push denial: %s\n' "$result" >&2
exit 11
SH
)"; then
    return 0
  fi
  die "$user $label push dry-run was allowed or inconclusive: $output"
}

for user in "${USERS[@]}"; do
  id "$user" >/dev/null 2>&1 || die "unknown agent user: $user"
  agent_uid="$(id -u "$user")"
  [[ "$agent_uid" -gt 0 ]] || die "$user must have a non-root UID"
  sudo -n -H -u "$user" test ! -r "$KEY" || die "$user can read deploy key"
  probe_push_denied "$user" "https://github.com/$REPO.git" HTTPS /usr/bin/git
  probe_push_denied "$user" "git@github.com:$REPO.git" SSH /usr/bin/git
  if [[ -x /usr/local/bin/bubble-git ]]; then
    probe_push_denied "$user" "https://github.com/$REPO.git" broker /usr/local/bin/bubble-git
  fi
  if (( CREDENTIAL_ONLY == 0 )); then
    [[ -L "$MIRROR" && -d "$MIRROR/operator-intents" ]] || die 'stable mirror is absent or not a symlink'
    [[ "$(stat -c '%U:%G' "$MIRROR")" == 'root:root' ]] || die 'stable mirror symlink is not root-owned'
    [[ -z "$(find -L "$MIRROR" -type d ! -perm 0555 -print -quit)" ]] || die 'mirror has a directory not mode 0555'
    [[ -z "$(find -L "$MIRROR" -type f ! -perm 0444 -print -quit)" ]] || die 'mirror has a file not mode 0444'
    [[ -z "$(find "$MIRROR/operator-intents" -type l -print -quit)" ]] || die 'mirror content contains a symlink'
    [[ -z "$(find -L "$MIRROR" \( ! -user root -o ! -group root \) -print -quit)" ]] || die 'mirror content is not root:root'
    sudo -n -H -u "$user" test -r "$MIRROR/operator-intents" || die "$user cannot read mirror"
    sudo -n -H -u "$user" test ! -w "$MIRROR" || die "$user can write stable mirror"
    sudo -n -H -u "$user" test ! -w "$MIRROR/operator-intents" || die "$user can create/remove mirror content"
    sudo -n -H -u "$user" test ! -O "$MIRROR/operator-intents" || die "$user owns mirror content and could chmod it"
  fi
  printf 'verify-operator-intents-isolation: PASS user=%s\n' "$user"
done
printf '%s\n' 'verify-operator-intents-isolation: PASS deploy key read-only status requires GitHub API attestation during install.'
