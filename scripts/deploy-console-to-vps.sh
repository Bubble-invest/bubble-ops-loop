#!/usr/bin/env bash
# deploy-console-to-vps.sh — materialize the console systemd UNIT onto a box.
#
# Board #1081: the checked-in canonical unit source
# `console/deploy/bubble-ops-console.service.template` was never actually
# installed by anything. deploy/INSTALL.md step 2 has always cited this exact
# script name, but it did not exist — the only console deploy script
# (deploy-console-to-morty.sh) pulls code + restarts but never copies the
# unit file into /etc/systemd/system. The live unit on joris-cx33 was
# hand-installed once and had since drifted from the checked-in template.
# This script is the missing installer.
#
# What it does (idempotent — safe to re-run any time):
#   1. Reads console/deploy/bubble-ops-console.service.template byte-for-byte
#      (no rendering: the template carries no secrets or {{PLACEHOLDER}}
#      substitutions — ExecStartPre resolves secrets at service-start time
#      from the SOPS-encrypted /etc/bubble/secrets.sops.env, by path only).
#   2. Diffs it against the currently-installed /etc/systemd/system unit.
#      If identical: no-op.
#   3. If different: installs it (root:root 0644), `systemctl daemon-reload`,
#      then (unless --no-restart) enables + restarts the service and
#      verifies it comes up active.
#
# Works on a FRESH box (does not require the unit — or even the service —
# to already exist; only requires the repo already cloned at $CONSOLE_WORKDIR)
# and as the ongoing "did the template change?" sync step on every deploy.
# deploy-console-to-morty.sh calls this (with --no-restart) right after
# pulling new code, so a template edit that lands on main reaches the box on
# the very next deploy without a separate manual step.
#
# Usage (from anywhere with SSH to the box, OR on the box itself):
#   scripts/deploy-console-to-vps.sh                # install/update unit + restart
#   scripts/deploy-console-to-vps.sh --dry-run      # show what would change, no writes
#   scripts/deploy-console-to-vps.sh --no-restart   # install unit + daemon-reload only
#
# Env overrides:
#   SSH_HOST / BUBBLE_VPS_HOST   ssh alias to the box (default: morty)
#   SERVICE                      systemd unit name, no .service (default: bubble-ops-console)
#   CONSOLE_WORKDIR              path to the bubble-ops-loop clone on the box
#                                (default: /home/claude/bubble-ops-loop)
#
# Requires:
#   - SSH alias to the box, OR run on the box itself
#   - sudo NOPASSWD on the box for: install, systemctl daemon-reload/enable/restart
#   - the box already has a bubble-ops-loop clone at $CONSOLE_WORKDIR
#     (this script installs the UNIT; it does not clone the repo)

set -euo pipefail

SSH_HOST="${SSH_HOST:-${BUBBLE_VPS_HOST:-morty}}"
SERVICE="${SERVICE:-bubble-ops-console}"
WORKDIR="${CONSOLE_WORKDIR:-/home/claude/bubble-ops-loop}"
TEMPLATE_REL="console/deploy/bubble-ops-console.service.template"
UNIT_PATH="/etc/systemd/system/${SERVICE}.service"
DRY=0
NO_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --no-restart) NO_RESTART=1 ;;
    --help|-h)
      sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "ERR: unknown argument: $arg" >&2; exit 64 ;;
  esac
done

# Detect "on the box" so we use local commands instead of self-SSH (mirrors
# deploy-console-to-morty.sh's detection).
HOSTNAME_DETECTED="$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo unknown)"
ON_MORTY=0
if [[ "$HOSTNAME_DETECTED" == "${BUBBLE_VPS_HOST:-morty}" ]] || [[ "$HOSTNAME_DETECTED" == "morty" ]] || \
   [[ -d /home/claude && "$(uname -s 2>/dev/null)" == "Linux" ]]; then
  ON_MORTY=1
  echo "[deploy-console-unit] Running ON the box (hostname=$HOSTNAME_DETECTED) — local commands, no SSH."
fi

run_remote() {
  if [[ "$ON_MORTY" == "1" ]]; then bash -c "$1"; else ssh "$SSH_HOST" "$1"; fi
}

TEMPLATE_ABS="$WORKDIR/$TEMPLATE_REL"
if ! NEW_UNIT="$(run_remote "cat '$TEMPLATE_ABS'")"; then
  echo "ERR: could not read template at $TEMPLATE_ABS on the box." >&2
  echo "     Is \$CONSOLE_WORKDIR ($WORKDIR) a bubble-ops-loop clone?" >&2
  exit 3
fi
CURRENT_UNIT="$(run_remote "sudo -n cat '$UNIT_PATH' 2>/dev/null || true")"

if [[ "$NEW_UNIT" == "$CURRENT_UNIT" ]]; then
  echo "[deploy-console-unit] $UNIT_PATH already matches the checked-in template — nothing to install."
  exit 0
fi

echo "[deploy-console-unit] $UNIT_PATH differs from the checked-in template:"
diff <(echo "$CURRENT_UNIT") <(echo "$NEW_UNIT") || true

if [[ "$DRY" == "1" ]]; then
  echo "[deploy-console-unit] DRY RUN — no install, no daemon-reload, no restart."
  exit 0
fi

echo "[deploy-console-unit] Installing $UNIT_PATH from $TEMPLATE_REL …"
run_remote "cat > /tmp/${SERVICE}.service.new <<'BUBBLE_UNIT_EOF'
$NEW_UNIT
BUBBLE_UNIT_EOF
sudo -n install -o root -g root -m 0644 /tmp/${SERVICE}.service.new '$UNIT_PATH'
rm -f /tmp/${SERVICE}.service.new
sudo -n systemctl daemon-reload"
echo "[deploy-console-unit] Installed + daemon-reload done."

if [[ "$NO_RESTART" == "1" ]]; then
  echo "[deploy-console-unit] --no-restart passed — skipping enable/restart."
  exit 0
fi

run_remote "sudo -n systemctl enable ${SERVICE} >/dev/null 2>&1 || true"
echo "[deploy-console-unit] Restarting ${SERVICE} …"
run_remote "sudo -n systemctl restart ${SERVICE}"
sleep 2
ACTIVE="$(run_remote "systemctl is-active ${SERVICE}")"
if [[ "$ACTIVE" != "active" ]]; then
  echo "ERR: ${SERVICE} failed to become active after unit install (state=$ACTIVE). Check journalctl." >&2
  exit 4
fi
echo "[deploy-console-unit] ✓ ${SERVICE} active with the installed unit."
