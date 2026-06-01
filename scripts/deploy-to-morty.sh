#!/usr/bin/env bash
# =============================================================================
# deploy-to-morty.sh - UX-5 systemd provisioner for ops-loop-<slug> on Morty.
#
# Renders deploy/templates/ops-loop-dept.service.template, installs it to
# Morty's /etc/systemd/system/, daemon-reload, enable + start, verify.
#
# Strict doctrine:
#   - DO NOT touch /etc/systemd/system/claude-agent-morty.service
#   - DO NOT use tmux (Step 4 documented 404 regression)
#   - DO NOT use `claude -p` (becomes paid June 15)
#   - DO use script(1) for pty allocation + plugin:telegram channel
#
# Usage:
#   ./deploy-to-morty.sh --slug=<kebab> [--remote=user@host] [--dry-run]
#
# Defaults:
#   --remote=$BUBBLE_MORTY_HOST (fallback: claude@morty)
#
# --dry-run prints every SSH/scp command without running them.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$PROJECT_ROOT/deploy/templates/ops-loop-dept.service.template"

usage() {
  cat <<'USAGE'
Usage: deploy-to-morty.sh --slug=<kebab> [--remote=user@host] [--dry-run]

Synopsis:
  Provisions an ops-loop-<slug>.service systemd unit on the Morty VPS by
  rendering deploy/templates/ops-loop-dept.service.template and installing
  it via SSH.

Arguments:
  --slug=<slug>      Department slug (kebab-case). REQUIRED.
  --remote=<host>    SSH target. Default: $BUBBLE_MORTY_HOST (fallback claude@morty).
  --dry-run          Print the rendered unit + every SSH command without
                     running them. Exits 0 if template is renderable.
  --help             Show this message.

Critical doctrine:
  - This script NEVER touches /etc/systemd/system/claude-agent-morty.service
    (MD5 ecfc78ac20e182ca302e5081e2c80943).
  - tmux is forbidden — the unit uses /usr/bin/script for pty allocation.
  - claude -p is forbidden — the unit uses the interactive `claude` binary
    with --dangerously-skip-permissions + --channels plugin:telegram@...

Example (preview):
  ./deploy-to-morty.sh --slug=miranda --dry-run

Example (real provisioning):
  ./deploy-to-morty.sh --slug=miranda --remote=claude@morty.tailnet
USAGE
}

SLUG=""
REMOTE=""
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --slug=*) SLUG="${arg#*=}" ;;
    --remote=*) REMOTE="${arg#*=}" ;;
    --dry-run) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ -z "$SLUG" ]]; then
  echo "ERROR: --slug required" >&2
  usage >&2
  exit 64
fi

if [[ -z "$REMOTE" ]]; then
  REMOTE="${BUBBLE_MORTY_HOST:-claude@morty}"
fi

if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERROR: template not found: $TEMPLATE" >&2
  exit 1
fi

# Render the template with the dept's substitutions.
TELEGRAM_STATE_DIR="/home/claude/.claude/channels/telegram-${SLUG}"
ENV_FILE="/run/claude-agent-${SLUG}/env"
UNIT_NAME="ops-loop-${SLUG}.service"
REMOTE_UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
# Canonical convention (Fix 1 — Sprint H+I):
# REMOTE_REPO_PATH must match the WorkingDirectory= in
# deploy/templates/ops-loop-dept.service.template, otherwise the systemd
# unit starts in a directory that has no cloned repo and crash-loops.
# Canonical path = /home/claude/agents/<slug> (matches Morty fixture,
# docs/ARCHITECTURE.md §1, docs/OPERATOR-GUIDE.md, Phase-G smoke test).
REMOTE_REPO_PATH="/home/claude/agents/${SLUG}"

# Substitute placeholders (DEPT_SLUG, TELEGRAM_STATE_DIR, ENV_FILE).
# Use sed with `|` delimiter since paths contain `/`.
rendered=$(
  sed \
    -e "s|\${DEPT_SLUG}|${SLUG}|g" \
    -e "s|\${TELEGRAM_STATE_DIR}|${TELEGRAM_STATE_DIR}|g" \
    -e "s|\${ENV_FILE}|${ENV_FILE}|g" \
    "$TEMPLATE"
)

# Doctrine check: NON-comment lines of rendered unit must NOT reference
# morty's own unit. (Comments may carry safety-rule reminders.)
if echo "$rendered" | grep -v "^[[:space:]]*#" | grep -q "claude-agent-morty"; then
  echo "ERROR: rendered unit references claude-agent-morty.service in a non-comment line" >&2
  echo "       This is a doctrine violation. Refusing to deploy." >&2
  exit 1
fi

if [[ "$DRY_RUN" = "1" ]]; then
  echo "==================== rendered unit ===================="
  echo "$rendered"
  echo "======================================================="
  echo ""
  echo "DRY RUN: the following SSH commands WOULD have been run against"
  echo "  remote = ${REMOTE}"
  echo "  unit   = ${UNIT_NAME}"
  echo ""
  echo "Doctrine reminder: NEVER touch /etc/systemd/system/claude-agent-morty.service"
  echo ""
  echo "# 1. Verify dept repo cloned at ${REMOTE_REPO_PATH}, or clone it."
  echo "ssh ${REMOTE} 'test -d ${REMOTE_REPO_PATH} || sudo git clone https://github.com/vdk888/bubble-ops-${SLUG} ${REMOTE_REPO_PATH}'"
  echo ""
  echo "# 2. Write the rendered unit to /tmp on Morty."
  echo "ssh ${REMOTE} 'cat > /tmp/${UNIT_NAME}' < <rendered>"
  echo ""
  echo "# 3. Install + reload + enable + start."
  echo "ssh ${REMOTE} 'sudo mv /tmp/${UNIT_NAME} ${REMOTE_UNIT_PATH} && sudo chown root:root ${REMOTE_UNIT_PATH} && sudo chmod 0644 ${REMOTE_UNIT_PATH} && sudo systemctl daemon-reload && sudo systemctl enable ${UNIT_NAME} && sudo systemctl start ${UNIT_NAME}'"
  echo ""
  echo "# 4. Verify active (running)."
  echo "ssh ${REMOTE} 'sudo systemctl status ${UNIT_NAME} --no-pager | head -15'"
  echo ""
  echo "# 5. After service is up, send /start to dept's Telegram bot to pair {{OPERATOR}}' chat_id."
  exit 0
fi

# --- Real provisioning path (NOT exercised by tests; SSH is mocked) ---

echo "[deploy] target: ${REMOTE}"
echo "[deploy] unit:   ${UNIT_NAME}"
echo "[deploy] repo:   ${REMOTE_REPO_PATH}"

# 1. Ensure the dept repo is cloned on Morty.
ssh "${REMOTE}" "test -d ${REMOTE_REPO_PATH} || sudo git clone https://github.com/vdk888/bubble-ops-${SLUG} ${REMOTE_REPO_PATH}"

# 2. Push the rendered unit via stdin SSH.
echo "$rendered" | ssh "${REMOTE}" "sudo tee /tmp/${UNIT_NAME} > /dev/null"

# 3. Install + reload + enable + start.
ssh "${REMOTE}" "sudo mv /tmp/${UNIT_NAME} ${REMOTE_UNIT_PATH} && sudo chown root:root ${REMOTE_UNIT_PATH} && sudo chmod 0644 ${REMOTE_UNIT_PATH} && sudo systemctl daemon-reload && sudo systemctl enable ${UNIT_NAME} && sudo systemctl start ${UNIT_NAME}"

# 4. Verify.
if ssh "${REMOTE}" "sudo systemctl is-active ${UNIT_NAME}" | grep -q "^active$"; then
  echo "[deploy] SUCCESS: ${UNIT_NAME} is active (running)"
  echo ""
  echo "Next step: send /start to the dept's Telegram bot from {{OPERATOR}}'s"
  echo "account so the per-dept access.json picks up the pairing."
  exit 0
else
  echo "[deploy] FAIL: ${UNIT_NAME} not active. Inspect:" >&2
  ssh "${REMOTE}" "sudo systemctl status ${UNIT_NAME} --no-pager | head -30" >&2 || true
  exit 1
fi
