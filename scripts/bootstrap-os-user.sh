#!/usr/bin/env bash
# =============================================================================
# bootstrap-os-user.sh — board #1120 per-dept OS-user provisioner.
#
# Creates ONE discrete, least-privilege system user for a dept (e.g.
# "agent-ben") so it no longer shares the "claude" UID with every other dept.
# This is a SEPARATE, explicit, human/root-run step from deploy-to-morty.sh —
# the deploy script REFUSES to provision a dept unit for an os_user that
# doesn't exist yet (see deploy-to-morty.sh step 0).
#
# Design (LEAST-PRIVILEGE-MIGRATION-PLAN.md / RUNBOOK-1120-per-dept-os-user.md,
# bubble-vps-platform): a "discrete system user" per dept, NOT systemd
# DynamicUser= — depts need a STABLE home + persistent state (~/.claude,
# ~/agents/<dept>, ~/.codex), which DynamicUser='s transient per-activation
# UID + private /tmp cannot provide.
#
# What this creates, idempotently:
#   1. The system user itself:
#        useradd --system --shell /usr/sbin/nologin --no-create-home \
#          --home-dir <home> <user>
#      - --system  : UID in the system range (no password aging).
#      - nologin   : never usable for an interactive shell login.
#      - NOT added to any sudo/wheel group — least privilege by construction.
#      - locked password (passwd -l) so it can never be used for password auth
#        (it's only ever reached via systemd User= or `sudo -n -u`).
#   2. Its home dir (<home>, e.g. /home/agent-ben), mode 0750, owned by itself.
#   3. Its workdir (<workdir>, e.g. /srv/agents/ben), mode 0750, owned by itself.
#      Passed explicitly (not derived) so it matches whatever
#      deploy-to-morty.sh computed for this dept.
#   4. A `~/.claude` skeleton dir (mode 0755) so the agent's first run has
#      somewhere to write settings/sessions without a permission error.
#
# This script does NOT:
#   - touch any EXISTING dept's files, units, or secrets.
#   - chown any already-decrypted secret (that's the unit's own root
#     ExecStartPre chown chain, which now targets ${OS_USER} directly — see
#     deploy/templates/ops-loop-dept.service.template).
#   - grant any sudo. Per-dept sudoers (watchdog) are a SEPARATE, later
#     runbook step, granted ONLY after the unit is confirmed running as the
#     new user.
#
# Usage:
#   sudo ./bootstrap-os-user.sh --os-user=agent-ben --workdir=/srv/agents/ben [--dry-run]
#
# --dry-run prints every command it WOULD run (useradd/mkdir/chown/chmod/
# passwd -l) without mutating anything — this is the "dry-run of the
# user-creation + chown logic in a throwaway path" validation artifact for
# board #1120 (see tests/test_bootstrap_os_user.py, which runs ONLY this
# dry-run mode — never real useradd — so CI never mutates its runner's
# passwd database as a side effect of testing this script's argument parsing
# and command construction).
# =============================================================================
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: bootstrap-os-user.sh --os-user=<user> --workdir=<path> [--dry-run] [--help]

Creates ONE discrete, least-privilege system user for a dept (board #1120).
Must be run as root (or via sudo). Idempotent — safe to re-run.

Arguments:
  --os-user=<user>   The Unix user to create, e.g. "agent-ben". REQUIRED.
                     Refused if empty, "root", or "claude" (the legacy shared
                     user this migration is moving AWAY from).
  --workdir=<path>   The dept's on-box working directory, e.g.
                     "/srv/agents/ben". REQUIRED. Must be an absolute path
                     outside of /home (per-user workdirs live under /srv/agents,
                     decoupled from the home dir — see
                     deploy/templates/ops-loop-dept.service.template header).
  --dry-run          Print every command that WOULD run, without running it.
                     Exits 0. Use this to validate before a real cutover.
  --help             Show this message.

Example (dry-run, safe to run anywhere):
  ./bootstrap-os-user.sh --os-user=agent-ben --workdir=/srv/agents/ben --dry-run

Example (real provisioning, root only):
  sudo ./bootstrap-os-user.sh --os-user=agent-ben --workdir=/srv/agents/ben
USAGE
}

OS_USER=""
WORKDIR=""
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --os-user=*) OS_USER="${arg#*=}" ;;
    --workdir=*) WORKDIR="${arg#*=}" ;;
    --dry-run) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ -z "${OS_USER}" ]]; then
  echo "ERROR: --os-user required" >&2
  usage >&2
  exit 64
fi

if [[ -z "${WORKDIR}" ]]; then
  echo "ERROR: --workdir required" >&2
  usage >&2
  exit 64
fi

if [[ "${OS_USER}" == "root" || "${OS_USER}" == "claude" ]]; then
  echo "ERROR: refusing to provision --os-user=${OS_USER}" >&2
  echo "       ('root' is never a dept user; 'claude' is the LEGACY shared" >&2
  echo "       user this migration is moving depts AWAY from — pass" >&2
  echo "       agent-<slug> instead)." >&2
  exit 64
fi

case "${WORKDIR}" in
  /*) ;;
  *) echo "ERROR: --workdir must be an absolute path, got: ${WORKDIR}" >&2; exit 64 ;;
esac
case "${WORKDIR}" in
  /home/*)
    echo "ERROR: --workdir must NOT live under /home (decouples the workdir" >&2
    echo "       from the home dir — see RUNBOOK-1120-per-dept-os-user.md" >&2
    echo "       'single transcript-rename point' rationale). Use /srv/agents/<slug>." >&2
    exit 64
    ;;
esac

HOME_DIR="/home/${OS_USER}"

run() {
  if [[ "$DRY_RUN" = "1" ]]; then
    echo "+ $*"
  else
    "$@"
  fi
}

echo "[bootstrap-os-user] user=${OS_USER} home=${HOME_DIR} workdir=${WORKDIR} dry_run=${DRY_RUN}"

# 1) Create the system user — idempotent (id check first; useradd itself
#    would exit non-zero on an existing user, which we treat as success here
#    so a re-run of the whole runbook step is safe).
if [[ "$DRY_RUN" = "1" ]] || ! id -u "${OS_USER}" >/dev/null 2>&1; then
  run useradd --system --shell /usr/sbin/nologin --no-create-home \
    --home-dir "${HOME_DIR}" "${OS_USER}"
else
  echo "[bootstrap-os-user] user ${OS_USER} already exists — skipping useradd"
fi

# 2) Lock the password — this user is NEVER reachable via password auth,
#    only via systemd User= or `sudo -n -u`.
run passwd -l "${OS_USER}"

# 3) Confirm it landed in NO sudo-capable group (fail loudly if it did —
#    this would defeat the entire migration).
if [[ "$DRY_RUN" != "1" ]]; then
  if id -nG "${OS_USER}" | tr ' ' '\n' | grep -qxE 'sudo|wheel|admin'; then
    echo "ERROR: ${OS_USER} landed in a sudo-capable group — aborting." >&2
    exit 1
  fi
fi

# 4) Home dir, owned by itself, 0750 (no world access).
run mkdir -p "${HOME_DIR}"
run chown "${OS_USER}:${OS_USER}" "${HOME_DIR}"
run chmod 0750 "${HOME_DIR}"

# 5) ~/.claude skeleton so the agent's first run has somewhere to write.
run mkdir -p "${HOME_DIR}/.claude"
run chown "${OS_USER}:${OS_USER}" "${HOME_DIR}/.claude"
run chmod 0755 "${HOME_DIR}/.claude"

# 6) Workdir (outside /home — see the guard above), owned by itself, 0750.
run mkdir -p "${WORKDIR}"
run chown "${OS_USER}:${OS_USER}" "${WORKDIR}"
run chmod 0750 "${WORKDIR}"

echo "[bootstrap-os-user] done. ${OS_USER} has NO sudo grant yet (by design —"
echo "  the per-dept watchdog sudoers grant is a LATER, separate runbook step,"
echo "  applied only after the unit is confirmed running as ${OS_USER})."
