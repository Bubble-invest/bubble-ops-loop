#!/usr/bin/env bash
# Linux-only root integration proof for the privileged systemd prestart path.
set -uo pipefail

if [[ "$EUID" != "0" || "$(uname -s)" != "Linux" ]]; then
  echo "SKIP: root Linux required"
  exit 0
fi

ROOT="${BUBBLE_OPS_LOOP_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
INSTALLER="$ROOT/scripts/install-channel-patches.sh"
PRISTINE="${CHANNEL_PATCHES_TEST_PRISTINE_SERVER:?set pristine server.ts fixture}"
TEST_USER="${CHANNEL_PATCHES_TEST_OS_USER:-nobody}"
PASSWD="$(getent passwd "$TEST_USER")" || { echo "FAIL: fixture user absent"; exit 1; }
IFS=: read -r _ _ TEST_UID TEST_GID _ TEST_HOME _ <<<"$PASSWD"
[[ "$TEST_UID" != "0" && "$TEST_HOME" == /* ]] || { echo "FAIL: unsafe fixture identity"; exit 1; }
[[ ! -e "$TEST_HOME" && ! -L "$TEST_HOME" ]] || {
  echo "FAIL: fixture passwd home already exists; refusing to reuse it"
  exit 1
}

cleanup() { rm -rf -- "$TEST_HOME"; }
trap cleanup EXIT
install -d -m 0700 -o "$TEST_UID" -g "$TEST_GID" "$TEST_HOME"
PLUGIN="$TEST_HOME/.claude/plugins/cache/claude-plugins-official/telegram/9.9.9"
install -d -m 0700 -o "$TEST_UID" -g "$TEST_GID" "$PLUGIN"
# Match the 0664 mode produced by the Linux plugin manager. The installer may
# accept group-write only because this gid is the fixture UID's isolated
# primary group; the live Ben proof exercises the same condition.
install -m 0664 -o "$TEST_UID" -g "$TEST_GID" "$PRISTINE" "$PLUGIN/server.ts"
printf '{}\n' > "$PLUGIN/package.json"
chown -R "$TEST_UID:$TEST_GID" "$TEST_HOME"

BEFORE="$(sha256sum "$PLUGIN/server.ts" | awk '{print $1}')"
set +e
BAD="$(HOME="$TEST_HOME/wrong" BUBBLE_AGENT_HOME="$TEST_HOME/wrong" \
  BUBBLE_AGENT_OS_USER="$TEST_USER" CHANNEL_PATCHES_PLUGIN_GLOB="$PLUGIN/" \
  CHANNEL_PATCHES_BUN=/bin/true bash "$INSTALLER" --strict 2>&1)"
BAD_RC=$?
set -e
[[ "$BAD_RC" != "0" && "$(sha256sum "$PLUGIN/server.ts" | awk '{print $1}')" == "$BEFORE" ]] || {
  echo "FAIL: wrong declared home was accepted or touched plugin"
  exit 1
}

OUT="$(HOME="$TEST_HOME" BUBBLE_AGENT_HOME="$TEST_HOME" \
  BUBBLE_AGENT_OS_USER="$TEST_USER" CHANNEL_PATCHES_PLUGIN_GLOB="$PLUGIN/" \
  CHANNEL_PATCHES_BUN=/bin/true AGENT_MESSAGE_CONFIG="$TEST_HOME/no-peer/config.json" \
  bash "$INSTALLER" --strict 2>&1)"
grep -q "dropping root prestart to declared uid $TEST_USER" <<<"$OUT" || {
  echo "FAIL: privilege-drop evidence missing"
  exit 1
}
grep -q bootRearmNotification "$PLUGIN/server.ts" || { echo "FAIL: boot patch missing"; exit 1; }
grep -q bubble-inject "$PLUGIN/server.ts" || { echo "FAIL: maintenance patch missing"; exit 1; }
[[ "$(stat -c %u "$PLUGIN/server.ts")" == "$TEST_UID" ]] || { echo "FAIL: plugin owner changed"; exit 1; }
for backup in "$PLUGIN"/server.ts.bak-*; do
  [[ "$(stat -c %u "$backup")" == "$TEST_UID" ]] || { echo "FAIL: root-owned backup proves no drop"; exit 1; }
done
[[ "$(grep -c BUBBLE-AGENT-MESSAGE-WATCHER-v1 "$PLUGIN/server.ts" || true)" == "0" ]] || {
  echo "FAIL: peer identity invented without config"
  exit 1
}

echo "PASS: root prestart validated declared passwd home and patched as uid $TEST_UID"
