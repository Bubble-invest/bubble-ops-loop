#!/bin/bash
# Render/validate by default. Only --activate mutates root launchd state.
set -Eeuo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_TEMPLATE="$HERE/operator-intents-mirror-sync.sh.template"
PLIST_TEMPLATE="$HERE/com.bubble.operator-intents-mirror.plist.template"
LIVE_SYNC='/Library/Application Support/Bubble/bin/operator-intents-mirror-sync'
LIVE_PLIST='/Library/LaunchDaemons/com.bubble.operator-intents-mirror.plist'
KEY='/Library/Application Support/Bubble/secrets/operator-intents-readonly-deploy-key'
LABEL='com.bubble.operator-intents-mirror'
VERIFY="$HERE/verify-operator-intents-isolation.sh"
MIRROR='/Library/Application Support/Bubble/operator-intents'
ACTIVATE=0
DEPLOY_KEY_ATTESTED=0
AGENT_USERS=()
RENDER_DIR=''
say() { printf 'install-operator-intents-mirror: %s\n' "$*" >&2; }
die() { say "ERROR: $*"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate) ACTIVATE=1; shift ;;
    --agent-user) AGENT_USERS+=("${2:?--agent-user requires a name}"); shift 2 ;;
    --deploy-key-readonly-attested) DEPLOY_KEY_ATTESTED=1; shift ;;
    --render-dir) RENDER_DIR="${2:?--render-dir needs an absolute path}"; shift 2 ;;
    --render-dir=*) RENDER_DIR="${1#*=}"; shift ;;
    -h|--help) sed -n '2,55p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -f "$SYNC_TEMPLATE" && -f "$PLIST_TEMPLATE" ]] || die 'mirror templates missing'
if [[ -z "$RENDER_DIR" ]]; then RENDER_DIR="$(mktemp -d "${TMPDIR:-/tmp}/operator-intents-render.XXXXXX")"; fi
[[ "$RENDER_DIR" == /* ]] || die '--render-dir must be absolute'
mkdir -p "$RENDER_DIR"
RENDER_SYNC="$RENDER_DIR/operator-intents-mirror-sync"
RENDER_PLIST="$RENDER_DIR/com.bubble.operator-intents-mirror.plist"
install -m 0700 "$SYNC_TEMPLATE" "$RENDER_SYNC"
install -m 0600 "$PLIST_TEMPLATE" "$RENDER_PLIST"
/bin/bash -n "$RENDER_SYNC" || die 'rendered sync script failed bash -n'
/usr/bin/python3 - "$RENDER_PLIST" <<'PY' || exit 1
import plistlib, sys
with open(sys.argv[1], 'rb') as handle:
    data = plistlib.load(handle)
assert data['Label'] == 'com.bubble.operator-intents-mirror'
assert data['UserName'] == 'root'
assert data['RunAtLoad'] is True and data['StartInterval'] == 900
PY
if command -v plutil >/dev/null 2>&1; then plutil -lint "$RENDER_PLIST" >/dev/null || die 'rendered plist failed plutil'; fi
say "rendered and validated $RENDER_SYNC and $RENDER_PLIST"

if [[ "$ACTIVATE" != 1 ]]; then
  say 'render-only: no live file or launchd state changed; review then use --activate'
  exit 0
fi
[[ "$(id -u)" == 0 ]] || die '--activate requires uid 0'
(( ${#AGENT_USERS[@]} > 0 )) || die '--activate requires at least one repeated --agent-user'
(( DEPLOY_KEY_ATTESTED == 1 )) \
  || die '--activate requires --deploy-key-readonly-attested after live GitHub verification'
[[ -f "$KEY" && ! -L "$KEY" ]] || die 'dedicated deploy key must be provisioned by a human/root operator first'
[[ "$(stat -f '%Su:%Sg:%Lp' "$KEY")" == 'root:wheel:400' ]] || die 'deploy key must be root:wheel mode 0400'
[[ -x "$VERIFY" ]] || die 'read-only isolation verifier missing'

# Credential boundary preflight happens before any live mirror/launchd mutation.
preflight_dir="$(mktemp -d "${TMPDIR:-/tmp}/operator-intents-preflight.XXXXXX")"
preflight_sync="$preflight_dir/operator-intents-mirror-sync"
install -o root -g wheel -m 0755 "$RENDER_SYNC" "$preflight_sync"
verify_args=(--credential-only --sync "$preflight_sync")
for user in "${AGENT_USERS[@]}"; do verify_args+=(--agent-user "$user"); done
if ! "$VERIFY" "${verify_args[@]}"; then
  rm -rf -- "$preflight_dir"
  die 'credential/UID preflight failed before activation; live state unchanged'
fi
rm -rf -- "$preflight_dir"

# Existing system ancestors must stay root-owned and non-writable by agents.
# In particular, macOS normally owns /Library/Application Support as
# root:admin; never chgrp that shared system directory just to install Bubble.
for ancestor in /Library '/Library/Application Support'; do
  [[ -d "$ancestor" && ! -L "$ancestor" ]] \
    || die "required system ancestor is missing, symlinked, or not a directory: $ancestor"
  [[ "$(stat -f '%Su:%Lp' "$ancestor")" == 'root:755' ]] \
    || die "required system ancestor is not root-owned mode 0755: $ancestor"
done
if [[ -e "$BASE" || -L "$BASE" ]]; then
  [[ -d "$BASE" && ! -L "$BASE" ]] || die "Bubble base is symlinked or not a directory: $BASE"
fi
install -d -o root -g wheel -m 0755 "$BASE"
[[ "$(stat -f '%Su:%Sg:%Lp' "$BASE")" == 'root:wheel:755' ]] \
  || die "Bubble base is not root:wheel 0755: $BASE"
install -d -o root -g wheel -m 0755 "$(dirname "$LIVE_SYNC")"
install -d -o root -g wheel -m 0755 "$(dirname "$LIVE_PLIST")"
backup="$(mktemp -d "${TMPDIR:-/tmp}/operator-intents-activate.XXXXXX")"
old_sync=0; old_plist=0; was_loaded=0
[[ -f "$LIVE_SYNC" ]] && { cp -p "$LIVE_SYNC" "$backup/sync"; old_sync=1; }
[[ -f "$LIVE_PLIST" ]] && { cp -p "$LIVE_PLIST" "$backup/plist"; old_plist=1; }
/bin/launchctl print "system/$LABEL" >/dev/null 2>&1 && was_loaded=1

restore() {
  if (( old_sync )); then install -o root -g wheel -m 0755 "$backup/sync" "$LIVE_SYNC"; else rm -f -- "$LIVE_SYNC"; fi
  if (( old_plist )); then install -o root -g wheel -m 0644 "$backup/plist" "$LIVE_PLIST"; else rm -f -- "$LIVE_PLIST"; fi
}

rollback_needed=1
new_loaded=0
sync_candidate=''; plist_candidate=''
finish_activation() {
  rc=$?
  rm -f -- "$sync_candidate" "$plist_candidate"
  if (( rc != 0 && rollback_needed )); then
    if (( new_loaded )); then /bin/launchctl bootout "system/$LABEL" >/dev/null 2>&1 || true; fi
    restore || true
    if (( was_loaded && old_plist )) && ! /bin/launchctl print "system/$LABEL" >/dev/null 2>&1; then
      /bin/launchctl bootstrap system "$LIVE_PLIST" >/dev/null 2>&1 || true
    fi
  fi
  rm -rf -- "$backup"
  trap - EXIT
  exit "$rc"
}
trap finish_activation EXIT
sync_candidate="$(mktemp "$(dirname "$LIVE_SYNC")/.operator-intents-sync.candidate.XXXXXX")"
plist_candidate="$(mktemp "$(dirname "$LIVE_PLIST")/.operator-intents-plist.candidate.XXXXXX")"
install -o root -g wheel -m 0755 "$RENDER_SYNC" "$sync_candidate"
install -o root -g wheel -m 0644 "$RENDER_PLIST" "$plist_candidate"
if (( was_loaded )); then
  /bin/launchctl bootout "system/$LABEL" || die 'could not unload prior LaunchDaemon; rollback armed'
fi
mv -f "$sync_candidate" "$LIVE_SYNC"; sync_candidate=''
mv -f "$plist_candidate" "$LIVE_PLIST"; plist_candidate=''
# Do not mistake a pre-existing mirror for proof that this newly installed
# binary can authenticate, compare main, publish, and validate successfully.
"$LIVE_SYNC" || die 'new sync binary failed its synchronous activation run; rollback armed'
if ! /bin/launchctl bootstrap system "$LIVE_PLIST"; then
  /bin/launchctl bootout "system/$LABEL" >/dev/null 2>&1 || true
  die 'activation failed; transactional rollback armed'
fi
new_loaded=1
full_verify_args=()
for user in "${AGENT_USERS[@]}"; do full_verify_args+=(--agent-user "$user"); done
"$VERIFY" "${full_verify_args[@]}" \
  || die 'post-RunAtLoad isolation verification failed; rollback armed'
rollback_needed=0
say 'ACTIVATED root LaunchDaemon; key contents were never copied or printed'
