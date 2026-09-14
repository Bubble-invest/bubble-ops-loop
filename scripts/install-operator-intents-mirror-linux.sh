#!/usr/bin/env bash
# Render/validate by default. Only --activate mutates root Linux/systemd state.
set -Eeuo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VPS="$REPO_ROOT/deploy/vps"
TEMPLATES="$REPO_ROOT/deploy/templates"
SYNC_TEMPLATE="$VPS/operator-intents-mirror-sync.sh.template"
VERIFY_TEMPLATE="$VPS/verify-operator-intents-isolation.sh"
SERVICE_TEMPLATE="$TEMPLATES/operator-intents-mirror.service"
TIMER_TEMPLATE="$TEMPLATES/operator-intents-mirror.timer"
LIVE_SYNC='/usr/local/sbin/operator-intents-mirror-sync'
LIVE_VERIFY='/usr/local/sbin/verify-operator-intents-isolation'
LIVE_SERVICE='/etc/systemd/system/operator-intents-mirror.service'
LIVE_TIMER='/etc/systemd/system/operator-intents-mirror.timer'
KEY='/etc/bubble/secrets/operator-intents-readonly-deploy-key'
BASE='/opt/bubble-operator-intents-data'
CURRENT="$BASE/current"
MIRROR='/opt/bubble-operator-intents'
ACTIVATE=0
DEPLOY_KEY_ATTESTED=0
AGENT_USERS=()
RENDER_DIR=''
say() { printf 'install-operator-intents-mirror-linux: %s\n' "$*" >&2; }
die() { say "ERROR: $*"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate) ACTIVATE=1; shift ;;
    --agent-user) AGENT_USERS+=("${2:?--agent-user requires a name}"); shift 2 ;;
    --deploy-key-readonly-attested) DEPLOY_KEY_ATTESTED=1; shift ;;
    --render-dir) RENDER_DIR="${2:?--render-dir needs an absolute path}"; shift 2 ;;
    --render-dir=*) RENDER_DIR="${1#*=}"; shift ;;
    -h|--help) printf '%s\n' 'Usage: install-operator-intents-mirror-linux.sh [--render-dir PATH] [--activate --deploy-key-readonly-attested --agent-user USER ...]'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
for required in "$SYNC_TEMPLATE" "$VERIFY_TEMPLATE" "$SERVICE_TEMPLATE" "$TIMER_TEMPLATE"; do
  [[ -f "$required" ]] || die "mirror asset missing: $required"
done
if [[ -z "$RENDER_DIR" ]]; then RENDER_DIR="$(mktemp -d "${TMPDIR:-/tmp}/operator-intents-linux-render.XXXXXX")"; fi
[[ "$RENDER_DIR" == /* ]] || die '--render-dir must be absolute'
mkdir -p "$RENDER_DIR"
RENDER_SYNC="$RENDER_DIR/operator-intents-mirror-sync"
RENDER_VERIFY="$RENDER_DIR/verify-operator-intents-isolation"
RENDER_SERVICE="$RENDER_DIR/operator-intents-mirror.service"
RENDER_TIMER="$RENDER_DIR/operator-intents-mirror.timer"
install -m 0700 "$SYNC_TEMPLATE" "$RENDER_SYNC"
install -m 0700 "$VERIFY_TEMPLATE" "$RENDER_VERIFY"
install -m 0600 "$SERVICE_TEMPLATE" "$RENDER_SERVICE"
install -m 0600 "$TIMER_TEMPLATE" "$RENDER_TIMER"
/bin/bash -n "$RENDER_SYNC" || die 'rendered sync script failed bash -n'
/bin/bash -n "$RENDER_VERIFY" || die 'rendered verifier failed bash -n'
if command -v systemd-analyze >/dev/null 2>&1 && [[ -x "$LIVE_SYNC" ]]; then
  systemd-analyze verify "$RENDER_SERVICE" "$RENDER_TIMER" >/dev/null || die 'rendered units failed systemd-analyze verify'
fi
say "rendered and validated Linux mirror bundle in $RENDER_DIR"

if [[ "$ACTIVATE" != 1 ]]; then
  say 'render-only: no live file or systemd state changed; review then use --activate'
  exit 0
fi
[[ "$(id -u)" == 0 ]] || die '--activate requires uid 0'
(( ${#AGENT_USERS[@]} > 0 )) || die '--activate requires at least one repeated --agent-user'
(( DEPLOY_KEY_ATTESTED == 1 )) || die '--activate requires --deploy-key-readonly-attested after live GitHub verification'
[[ -f "$KEY" && ! -L "$KEY" ]] || die 'dedicated deploy key must be provisioned by a root operator first'
[[ "$(stat -c '%U:%G:%a' "$KEY")" == 'root:root:400' ]] || die 'deploy key must be root:root mode 0400'

preflight_dir="$(mktemp -d "${TMPDIR:-/tmp}/operator-intents-linux-preflight.XXXXXX")"
preflight_sync="$preflight_dir/operator-intents-mirror-sync"
install -o root -g root -m 0755 "$RENDER_SYNC" "$preflight_sync"
verify_args=(--credential-only --sync "$preflight_sync")
for user in "${AGENT_USERS[@]}"; do verify_args+=(--agent-user "$user"); done
if ! "$RENDER_VERIFY" "${verify_args[@]}"; then
  rm -rf -- "$preflight_dir"
  die 'credential/UID preflight failed before activation; live state unchanged'
fi
rm -rf -- "$preflight_dir"

[[ -d /opt && ! -L /opt && "$(stat -c '%U:%G:%a' /opt)" == 'root:root:755' ]] || die '/opt must be a root:root 0755 directory'
if [[ -e "$BASE" || -L "$BASE" ]]; then
  [[ -d "$BASE" && ! -L "$BASE" ]] || die "$BASE is symlinked or not a directory"
  [[ "$(stat -c '%U:%G:%a' "$BASE")" == 'root:root:755' ]] || die "$BASE must be root:root 0755"
fi
if [[ -e "$MIRROR" || -L "$MIRROR" ]]; then
  [[ -L "$MIRROR" && "$(readlink "$MIRROR")" == "$CURRENT" ]] || die "$MIRROR is not the managed stable symlink"
fi

backup="$(mktemp -d "${TMPDIR:-/tmp}/operator-intents-linux-activate.XXXXXX")"
created_mirror=0
was_enabled=0
was_active=0
systemctl is-enabled operator-intents-mirror.timer >/dev/null 2>&1 && was_enabled=1
systemctl is-active operator-intents-mirror.timer >/dev/null 2>&1 && was_active=1
for pair in "sync:$LIVE_SYNC" "verify:$LIVE_VERIFY" "service:$LIVE_SERVICE" "timer:$LIVE_TIMER"; do
  name="${pair%%:*}"; live="${pair#*:}"
  [[ -f "$live" ]] && cp -p "$live" "$backup/$name"
done
rollback_needed=1
rollback() {
  local rc=$?
  if (( rc != 0 && rollback_needed )); then
    systemctl disable --now operator-intents-mirror.timer >/dev/null 2>&1 || true
    for pair in "sync:$LIVE_SYNC" "verify:$LIVE_VERIFY" "service:$LIVE_SERVICE" "timer:$LIVE_TIMER"; do
      name="${pair%%:*}"; live="${pair#*:}"
      if [[ -f "$backup/$name" ]]; then install -o root -g root -m "$([[ "$name" == sync || "$name" == verify ]] && printf 0755 || printf 0644)" "$backup/$name" "$live"; else rm -f -- "$live"; fi
    done
    (( created_mirror == 0 )) || rm -f -- "$MIRROR"
    systemctl daemon-reload >/dev/null 2>&1 || true
    (( was_enabled == 0 )) || systemctl enable operator-intents-mirror.timer >/dev/null 2>&1 || true
    (( was_active == 0 )) || systemctl start operator-intents-mirror.timer >/dev/null 2>&1 || true
  fi
  rm -rf -- "$backup"
  trap - EXIT
  exit "$rc"
}
trap rollback EXIT

install -d -o root -g root -m 0755 "$BASE"
if [[ ! -L "$MIRROR" ]]; then
  link_tmp="/opt/.bubble-operator-intents.$$"
  ln -s -- "$CURRENT" "$link_tmp"
  chown -h root:root "$link_tmp"
  mv -T -- "$link_tmp" "$MIRROR"
  created_mirror=1
fi
install -o root -g root -m 0755 "$RENDER_SYNC" "$LIVE_SYNC"
install -o root -g root -m 0755 "$RENDER_VERIFY" "$LIVE_VERIFY"
install -o root -g root -m 0644 "$RENDER_SERVICE" "$LIVE_SERVICE"
install -o root -g root -m 0644 "$RENDER_TIMER" "$LIVE_TIMER"
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$LIVE_SERVICE" "$LIVE_TIMER" >/dev/null || die 'installed units failed systemd-analyze verify; rollback armed'
fi
systemctl daemon-reload
"$LIVE_SYNC" || die 'new sync binary failed its synchronous activation run; rollback armed'
full_verify_args=()
for user in "${AGENT_USERS[@]}"; do full_verify_args+=(--agent-user "$user"); done
"$LIVE_VERIFY" "${full_verify_args[@]}" || die 'post-sync isolation verification failed; rollback armed'
systemctl enable --now operator-intents-mirror.timer
systemctl is-enabled --quiet operator-intents-mirror.timer || die 'mirror timer is not enabled'
systemctl is-active --quiet operator-intents-mirror.timer || die 'mirror timer is not active'
rollback_needed=0
say 'ACTIVATED root systemd mirror; key contents were never copied or printed'
