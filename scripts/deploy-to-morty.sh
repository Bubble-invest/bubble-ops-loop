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
#   - DO NOT hand-edit unit files or drop-ins on the box with a manual
#     `cp foo foo.bak-$(date +%Y%m%d)` copy. Re-run this provisioner instead
#     (or the relevant boot-inject/template regen). Hand-copy-with-.bak is
#     the root cause of the recurring `.bak` fossil sprawl swept in
#     bubble-ops-board#685 — every stray .bak in /etc/systemd/system or
#     /usr/local/bin came from this pattern, not from the provisioner.
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
  --model=<model>    Per-dept model pin for `claude --model` in ExecStart.
                     Default: opus[1m] (unchanged from prior behaviour). Use
                     sonnet[1m] for cheap-orchestrator depts that spawn Opus
                     subagents for heavy reasoning. Canonical per-dept value
                     lives in dept.yaml::department.model.
  --os-user=<user>   Board #1120 per-dept OS-user isolation. Unix user this
                     dept runs as. Default: "claude" (LEGACY — shared with
                     every other dept, byte-identical to pre-#1120 renders).
                     Pass "agent-<slug>" (e.g. --os-user=agent-ben) to run
                     this dept under its OWN OS user so a same-UID neighbor
                     dept can no longer read its decrypted secrets. The user
                     MUST already exist on the box (see
                     scripts/bootstrap-os-user.sh) and must NOT be "claude"
                     itself. Changes WorkingDirectory/REMOTE_REPO_PATH from
                     /home/claude/agents/<slug> to /srv/agents/<slug> (see
                     RUNBOOK-1120-per-dept-os-user.md) — the unit must be
                     stopped before the migration and the workdir git-cloned
                     fresh (or moved+chowned) at the new path.
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
# Default keeps prior behaviour: the live tony/ben/maya/accountant units pinned
# opus[1m] before this flag existed. Cost-optimization depts pass --model=sonnet[1m].
CLAUDE_MODEL="opus[1m]"
# Board #1120 per-dept OS-user isolation. Default "claude" = LEGACY, keeps
# every render byte-identical to pre-#1120 behaviour until a dept opts in.
OS_USER="claude"

for arg in "$@"; do
  case "$arg" in
    --slug=*) SLUG="${arg#*=}" ;;
    --model=*) CLAUDE_MODEL="${arg#*=}" ;;
    --os-user=*) OS_USER="${arg#*=}" ;;
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

# Board #1120: OS_GROUP always == OS_USER (server.user / useradd default
# primary group for a system user shares its own name — same convention as
# bubble-vps-platform's pyinfra/tasks/agent/_os_user.py).
OS_GROUP="${OS_USER}"

# Render the template with the dept's substitutions.
#
# Board #1120: WORKDIR (and therefore TELEGRAM_STATE_DIR + REMOTE_REPO_PATH)
# depends on OS_USER. LEGACY (claude) keeps the canonical
# /home/claude/agents/<slug> convention (Fix 1 — Sprint H+I) byte-identical.
# PER-USER (os_user != claude) moves the workdir OUT of any user's home to
# /srv/agents/<slug> — decouples the workdir from the home dir so the
# session-transcript path (claude derives it from the absolute WorkingDirectory
# by '/'->'-') has a SINGLE rename point, and matches
# bubble-vps-platform's lib/host_helpers.agent_workdir() convention for the
# pyinfra-managed concierges so both deploy systems agree.
if [[ "${OS_USER}" == "claude" ]]; then
  WORKDIR="/home/claude/agents/${SLUG}"
  TELEGRAM_STATE_DIR="/home/claude/.claude/channels/telegram-${SLUG}"
else
  WORKDIR="/srv/agents/${SLUG}"
  TELEGRAM_STATE_DIR="/home/${OS_USER}/.claude/channels/telegram-${SLUG}"
fi
ENV_FILE="/run/claude-agent-${SLUG}/env"
UNIT_NAME="ops-loop-${SLUG}.service"
REMOTE_UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
# Canonical convention (Fix 1 — Sprint H+I), extended by board #1120:
# REMOTE_REPO_PATH must match the WorkingDirectory= in
# deploy/templates/ops-loop-dept.service.template, otherwise the systemd
# unit starts in a directory that has no cloned repo and crash-loops.
# Kept as its OWN variable (not just an alias for WORKDIR) so
# tests/test_systemd_path_matches_deploy.py can keep asserting the two never
# drift apart, whichever OS_USER is in play.
REMOTE_REPO_PATH="${WORKDIR}"

# Substitute placeholders (DEPT_SLUG, DEPT_SLUG_UPPER, TELEGRAM_STATE_DIR,
# ENV_FILE, OS_USER, OS_GROUP, WORKDIR, CLAUDE_MODEL).
# DEPT_SLUG_UPPER = slug upper-cased with '-'→'_' — matches the broker's
# GITHUB_APP_INSTALLATION_ID_<DEPT> env-var naming (cli.py::_resolve_installation_id).
# Use sed with `|` delimiter since paths contain `/`.
SLUG_UPPER=$(printf '%s' "${SLUG}" | tr '[:lower:]-' '[:upper:]_')
rendered=$(
  sed \
    -e "s|\${DEPT_SLUG_UPPER}|${SLUG_UPPER}|g" \
    -e "s|\${DEPT_SLUG}|${SLUG}|g" \
    -e "s|\${TELEGRAM_STATE_DIR}|${TELEGRAM_STATE_DIR}|g" \
    -e "s|\${ENV_FILE}|${ENV_FILE}|g" \
    -e "s|\${CLAUDE_MODEL}|${CLAUDE_MODEL}|g" \
    -e "s|\${OS_USER}|${OS_USER}|g" \
    -e "s|\${OS_GROUP}|${OS_GROUP}|g" \
    -e "s|\${WORKDIR}|${WORKDIR}|g" \
    "$TEMPLATE"
)

# Refuse an obviously-broken --os-user (empty after substitution, or "root" —
# never run a dept agent as root even by typo).
if [[ -z "${OS_USER}" || "${OS_USER}" == "root" ]]; then
  echo "ERROR: --os-user must be a non-empty, non-root Unix user" >&2
  exit 64
fi

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
  if [[ "${OS_USER}" != "claude" ]]; then
    echo "# 0. (board #1120) ${OS_USER} MUST already exist — run FIRST, once:"
    echo "ssh ${REMOTE} 'sudo scripts/bootstrap-os-user.sh --os-user=${OS_USER} --workdir=${WORKDIR}'"
    echo ""
  fi
  echo "# 1. Verify dept repo cloned at ${REMOTE_REPO_PATH}, or clone it (then chown to ${OS_USER}:${OS_GROUP})."
  echo "ssh ${REMOTE} 'test -d ${REMOTE_REPO_PATH} || sudo git clone https://github.com/vdk888/bubble-ops-${SLUG} ${REMOTE_REPO_PATH}'"
  echo "ssh ${REMOTE} 'sudo chown -R ${OS_USER}:${OS_GROUP} ${REMOTE_REPO_PATH}'"
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

# 0. Board #1120: refuse to run for real against a non-legacy os_user until
#    that user already exists on the box (bootstrap-os-user.sh is a SEPARATE,
#    explicit, human-run step — this script never creates OS users itself).
if [[ "${OS_USER}" != "claude" ]]; then
  if ! ssh "${REMOTE}" "id -u ${OS_USER}" >/dev/null 2>&1; then
    echo "[deploy] FAIL: OS user '${OS_USER}' does not exist on ${REMOTE}." >&2
    echo "         Run scripts/bootstrap-os-user.sh --os-user=${OS_USER} --workdir=${WORKDIR} FIRST." >&2
    exit 1
  fi
fi

# 1. Ensure the dept repo is cloned on Morty, owned by the dept's OS user.
ssh "${REMOTE}" "test -d ${REMOTE_REPO_PATH} || sudo git clone https://github.com/vdk888/bubble-ops-${SLUG} ${REMOTE_REPO_PATH}"
ssh "${REMOTE}" "sudo chown -R ${OS_USER}:${OS_GROUP} ${REMOTE_REPO_PATH}"

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
