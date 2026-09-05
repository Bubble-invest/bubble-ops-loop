#!/usr/bin/env bash
# =============================================================================
# dept-cutover.sh — board #1150: fold the PROVEN per-dept OS-user isolation
# cutover recipe (validated live on maya + tony, board #1120/#1150) into ONE
# reviewed, idempotent, root-run command.
#
# This is the missing "glue" step between:
#   (a) scripts/bootstrap-os-user.sh   — creates the OS user + home + workdir
#   (b)-(g) below                       — everything ELSE a dept needs before
#                                          its unit can be re-pointed at the
#                                          new user (board #1120 step 6/7/9 in
#                                          RUNBOOK-1120-per-dept-os-user.md,
#                                          bubble-vps-platform)
#   redeploy                            — scripts/deploy-to-morty.sh
#                                          --slug=<dept> --os-user=agent-<dept>
#                                          (a SEPARATE, later step — this
#                                          script never touches the live unit)
#
# WHY THIS EXISTS: the maya + tony cutovers each hit the SAME set of gaps that
# a hand-run checklist kept missing one of:
#   - a stale, hand-copied ~/.claude.json (or none at all) left the fresh user
#     stuck at a first-run onboarding/trust prompt (headless = hang);
#   - the telegram plugin marketplace/plugin was never (re)installed under the
#     NEW user's own $HOME, so it either fell back to a stale /home/claude
#     installPath record or wasn't installed at all;
#   - /srv/agents/<slug>/.claude/settings.json still carried `env` overrides
#     (TELEGRAM_STATE_DIR, BUBBLE_DEPT_ROOT, or other /home/claude-pathed
#     values) — settings.json's `env` block OVERRIDES the systemd unit's
#     Environment=, so a leftover override silently re-points the dept at the
#     OLD shared paths even though the unit itself is correctly configured.
#     THIS is the exact bug that broke tony's cutover the first time.
#   - codex auth (~/.codex/{auth.json,config.toml}) was never copied to the
#     new user, and per-dept persistent state (/var/lib/bubble-<slug>-*, a
#     per-dept /run drop-in) kept root:claude / claude:claude ownership,
#     invisible to the new user;
#   - the OLD telegram-watchdog-<slug>.timer (still targeting the pre-cutover
#     unit) kept running, risk #409 (watchdog "fixing" a unit that's supposed
#     to be down during the cutover window).
#
# This script performs (b)-(g) below, calling (a) first unless
# --skip-user-bootstrap is passed (e.g. a retry after (a) already succeeded).
# Every step is IDEMPOTENT — safe to re-run the whole script after a partial
# failure.
#
# Usage (on the box, as root):
#   sudo ./dept-cutover.sh --slug=maya [--dry-run]
#   sudo ./dept-cutover.sh --slug=ben --os-user=agent-ben --workdir=/srv/agents/ben
#
# --dry-run prints every command/file-write it WOULD perform without
# mutating anything (same contract as bootstrap-os-user.sh).
#
# This script does NOT:
#   - touch the live systemd unit (that's scripts/deploy-to-morty.sh, a
#     separate, later step — see RUNBOOK-1120-per-dept-os-user.md §4.2).
#   - grant any sudo (the watchdog's per-user sudoers grant is a LATER,
#     separate runbook step, same as bootstrap-os-user.sh).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BOOTSTRAP_OS_USER="$SCRIPT_DIR/bootstrap-os-user.sh"
CLAUDE_JSON_TEMPLATE="$PROJECT_ROOT/deploy/templates/dept-cutover-claude.json.template"

usage() {
  cat <<'USAGE'
Usage: dept-cutover.sh --slug=<slug> [options]

Folds the full board #1120/#1150 per-dept OS-user cutover recipe (validated
live on maya + tony) into one idempotent, root-run command. Does NOT touch
the live systemd unit — run scripts/deploy-to-morty.sh --os-user=... AFTER
this script succeeds and its verification steps pass.

Arguments:
  --slug=<slug>            Department slug, e.g. "maya". Required.
  --os-user=<user>         OS user to create/use. Default: agent-<slug>.
  --workdir=<path>         Dept workdir. Default: /srv/agents/<slug>.
                           Must be outside /home (see bootstrap-os-user.sh).
  --legacy-home=<path>     The shared legacy user's home, source of codex
                           auth + stale plugin records to clean. Default:
                           /home/claude.
  --marketplace=<owner/repo>
                           Claude Code plugin marketplace to register.
                           Default: anthropics/claude-plugins-official.
  --skip-user-bootstrap    Skip step (a) — assumes bootstrap-os-user.sh
                           already ran for this dept (e.g. a retry).
  --dry-run                Print every command/write WITHOUT mutating
                           anything. Exits 0.
  --help                   Show this message.

Exit codes:
  0    all steps completed (or --dry-run plan printed)
  2    structural error (bad args, missing template/helper)
  64   bad CLI args

Example (dry-run, safe to run anywhere):
  ./dept-cutover.sh --slug=maya --dry-run

Example (real cutover, root only):
  sudo ./dept-cutover.sh --slug=maya
USAGE
}

SLUG=""
OS_USER=""
WORKDIR=""
LEGACY_HOME="/home/claude"
MARKETPLACE="anthropics/claude-plugins-official"
SKIP_USER_BOOTSTRAP=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --slug=*) SLUG="${arg#*=}" ;;
    --os-user=*) OS_USER="${arg#*=}" ;;
    --workdir=*) WORKDIR="${arg#*=}" ;;
    --legacy-home=*) LEGACY_HOME="${arg#*=}" ;;
    --marketplace=*) MARKETPLACE="${arg#*=}" ;;
    --skip-user-bootstrap) SKIP_USER_BOOTSTRAP=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 64 ;;
  esac
done

[[ -z "$SLUG" ]] && { echo "ERROR: --slug required" >&2; usage >&2; exit 64; }
[[ "$SLUG" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "ERROR: --slug must be kebab-case" >&2; exit 64; }

[[ -z "$OS_USER" ]] && OS_USER="agent-${SLUG}"
[[ -z "$WORKDIR" ]] && WORKDIR="/srv/agents/${SLUG}"

if [[ "$OS_USER" == "root" || "$OS_USER" == "claude" ]]; then
  echo "ERROR: refusing --os-user=${OS_USER} (see bootstrap-os-user.sh)" >&2
  exit 64
fi

HOME_DIR="/home/${OS_USER}"

[[ -f "$BOOTSTRAP_OS_USER" ]] || { echo "ERROR: missing $BOOTSTRAP_OS_USER" >&2; exit 2; }
[[ -f "$CLAUDE_JSON_TEMPLATE" ]] || { echo "ERROR: missing $CLAUDE_JSON_TEMPLATE" >&2; exit 2; }

say() { echo "[dept-cutover:${SLUG}] $*"; }
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  + $*"
  else
    "$@"
  fi
}

say "os_user=${OS_USER} home=${HOME_DIR} workdir=${WORKDIR} legacy_home=${LEGACY_HOME} dry_run=${DRY_RUN}"

# ── (a) create the OS user + home + workdir ─────────────────────────────────
step_a_bootstrap_user() {
  if [[ "$SKIP_USER_BOOTSTRAP" == "1" ]]; then
    say "(a) --skip-user-bootstrap set — assuming ${OS_USER} already exists"
    return 0
  fi
  say "(a) bootstrap-os-user.sh --os-user=${OS_USER} --workdir=${WORKDIR}"
  local args=(--os-user="${OS_USER}" --workdir="${WORKDIR}")
  [[ "$DRY_RUN" == "1" ]] && args+=(--dry-run)
  bash "$BOOTSTRAP_OS_USER" "${args[@]}"
}

# ── (b) seed a MINIMAL ~/.claude.json — never copy-forward a legacy         ─
#     management settings.json wholesale.                                   ─
step_b_seed_claude_json() {
  local dest="${HOME_DIR}/.claude.json"
  say "(b) seed ${dest} (hasCompletedOnboarding + hasTrustDialogAccepted + trusted project ${WORKDIR})"
  if [[ -f "$dest" ]]; then
    say "    ${dest} already exists — leaving it alone (idempotent; delete it first to reseed)"
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  + render ${CLAUDE_JSON_TEMPLATE} (WORKDIR=${WORKDIR}) -> ${dest}"
    echo "  + chown ${OS_USER}:${OS_USER} ${dest}"
    echo "  + chmod 0600 ${dest}"
    return 0
  fi
  local tmp; tmp="$(mktemp)"
  TEMPLATE_FILE="$CLAUDE_JSON_TEMPLATE" WORKDIR="$WORKDIR" OUT_FILE="$tmp" python3 - <<'PY'
import os

path = os.environ["TEMPLATE_FILE"]
workdir = os.environ["WORKDIR"]
out = os.environ["OUT_FILE"]

with open(path) as fh:
    raw = fh.read()

# __WORKDIR__ sits inside a JSON string literal in the template — encode it
# the same way json.dumps would (minus the surrounding quotes) so any
# path with a backslash/quote in it round-trips safely.
import json
encoded = json.dumps(workdir)[1:-1]
raw = raw.replace("__WORKDIR__", encoded)

with open(out, "w") as fh:
    fh.write(raw)
PY
  mv "$tmp" "$dest"
  chown "${OS_USER}:${OS_USER}" "$dest"
  chmod 0600 "$dest"
}

# ── (c) register the marketplace + install telegram@ for the NEW user      ─
step_c_register_plugin() {
  say "(c) register marketplace ${MARKETPLACE} + install telegram plugin as ${OS_USER}"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  + runuser -l ${OS_USER} -c 'claude plugin marketplace add ${MARKETPLACE}'"
    echo "  + runuser -l ${OS_USER} -c 'claude plugin install telegram@claude-plugins-official --scope user -y'"
  else
    runuser -l "${OS_USER}" -c "claude plugin marketplace add '${MARKETPLACE}'" \
      || say "    WARN: marketplace add failed/already-present — continuing"
    runuser -l "${OS_USER}" -c "claude plugin install telegram@claude-plugins-official --scope user -y" \
      || { echo "ERROR: telegram plugin install failed for ${OS_USER}" >&2; exit 1; }
  fi

  say "    cleaning any legacy ${LEGACY_HOME} installPath records from installed_plugins.json"
  local f
  while IFS= read -r -d '' f; do
    if [[ "$DRY_RUN" == "1" ]]; then
      echo "  + strip '${LEGACY_HOME}' installPath entries from $f"
    else
      LEGACY_HOME="$LEGACY_HOME" TARGET_FILE="$f" python3 - <<'PY'
import json, os, sys

path = os.environ["TARGET_FILE"]
legacy = os.environ["LEGACY_HOME"]

with open(path) as fh:
    data = json.load(fh)

def is_legacy(v):
    return isinstance(v, str) and legacy in v

def scrub(obj):
    if isinstance(obj, dict):
        drop = [k for k, v in obj.items()
                if (k == "installPath" and is_legacy(v)) or is_legacy(k)]
        for k in drop:
            obj.pop(k, None)
        for v in obj.values():
            scrub(v)
    elif isinstance(obj, list):
        obj[:] = [v for v in obj if not is_legacy(v)]
        for v in obj:
            scrub(v)

scrub(data)
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
print(f"[dept-cutover] scrubbed legacy '{legacy}' installPath entries from {path}")
PY
    fi
  done < <(find "${HOME_DIR}/.claude" -name "installed_plugins.json" -print0 2>/dev/null)
}

# ── (d) strip settings.json env overrides — the tony-cutover bug ──────────
step_d_strip_settings_env() {
  local settings="${WORKDIR}/.claude/settings.json"
  say "(d) strip legacy env overrides from ${settings}"
  if [[ ! -f "$settings" ]]; then
    say "    ${settings} not found — nothing to strip"
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  + strip TELEGRAM_STATE_DIR / BUBBLE_DEPT_ROOT / any /home/claude-pathed env override from ${settings}"
    return 0
  fi
  LEGACY_HOME="$LEGACY_HOME" TARGET_FILE="$settings" python3 - <<'PY'
import json, os

path = os.environ["TARGET_FILE"]
legacy = os.environ["LEGACY_HOME"]
# Board #1150 (the tony-cutover bug): settings.json's `env` block OVERRIDES
# the systemd unit's Environment= — a leftover override here silently
# re-points a correctly-configured unit at the OLD shared paths.
DENY_KEYS = {"TELEGRAM_STATE_DIR", "BUBBLE_DEPT_ROOT"}

with open(path) as fh:
    data = json.load(fh)

env = data.get("env")
if isinstance(env, dict):
    removed = []
    for k in list(env.keys()):
        v = env[k]
        if k in DENY_KEYS or (isinstance(v, str) and legacy in v):
            removed.append(k)
            env.pop(k, None)
    if removed:
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        print(f"[dept-cutover] stripped env overrides from {path}: {removed}")
    else:
        print(f"[dept-cutover] no legacy env overrides found in {path}")
else:
    print(f"[dept-cutover] {path} has no top-level 'env' object — nothing to strip")
PY
}

# ── (e) copy codex auth + migrate per-dept persistent state ownership ─────
step_e_copy_codex_and_state() {
  say "(e) copy codex auth + migrate per-dept persistent state"
  local codex_src="${LEGACY_HOME}/.codex"
  local codex_dst="${HOME_DIR}/.codex"
  if [[ -f "${codex_src}/auth.json" || -f "${codex_src}/config.toml" ]]; then
    run mkdir -p "$codex_dst"
    [[ -f "${codex_src}/auth.json" ]] && run cp "${codex_src}/auth.json" "${codex_dst}/auth.json"
    [[ -f "${codex_src}/config.toml" ]] && run cp "${codex_src}/config.toml" "${codex_dst}/config.toml"
    run chown -R "${OS_USER}:${OS_USER}" "$codex_dst"
    [[ -f "${codex_dst}/auth.json" ]] && run chmod 0600 "${codex_dst}/auth.json"
  else
    say "    no ${codex_src}/{auth.json,config.toml} found — skipping codex auth copy"
  fi

  # Generic per-dept persistent state: /var/lib/bubble-<slug>-* (tmpfs /run
  # state is already handled by the unit's own ExecStartPre chown chain —
  # see bubble-agent-prepare in bubble-vps-platform).
  local d
  for d in /var/lib/bubble-"${SLUG}"-*; do
    [[ -e "$d" ]] || continue
    say "    migrating ownership: $d"
    run chown -R "${OS_USER}:${OS_USER}" "$d"
  done

  # Known EXTRA per-dept state that lives OUTSIDE the glob above (confirmed
  # in RUNBOOK-1120-per-dept-os-user.md §4.3 for Ben; extend this table as
  # more depts are cut over — do NOT assume it's exhaustive).
  case "$SLUG" in
    ben)
      for d in /var/lib/bubble-poly-ben /var/lib/bubble-saxo-ben; do
        [[ -e "$d" ]] || continue
        say "    migrating ownership (ben-specific): $d"
        run chown -R "${OS_USER}:${OS_USER}" "$d"
      done
      ;;
    tony)
      if [[ -e /run/gws-tony ]]; then
        say "    NOTE: /run/gws-tony (Joris's Google Workspace creds) is tmpfs —"
        say "          confirm it is re-created owned by ${OS_USER} on the next unit start"
        say "          (its ExecStartPre chown chain), not chowned here."
      fi
      ;;
  esac
  say "    REMINDER: any per-dept sudoers/systemd drop-in fragment that hardcodes"
  say "    the OLD 'claude' user (e.g. a Google-Workspace-readonly sudoers drop-in)"
  say "    must be REGENERATED for ${OS_USER}, not just chowned — verify explicitly."
}

# ── (f) clear stale /tmp build logs (belt-and-suspenders until board #1150's ─
#     mktemp fix — install-boot-rearm.sh / install-channel-patches.sh — is    ─
#     deployed everywhere; harmless no-op afterward)                         ─
step_f_clear_stale_tmp_logs() {
  say "(f) clearing stale shared /tmp build logs"
  run rm -f /tmp/install-boot-rearm-build.log /tmp/install-channel-patches-inject-build.log
}

# ── (g) disable the OLD shared-user watchdog timer ─────────────────────────
step_g_disable_old_watchdog() {
  local timer="telegram-watchdog-${SLUG}.timer"
  say "(g) disabling old ${timer} (targets the pre-cutover unit — risk #409)"
  if systemctl list-unit-files "$timer" >/dev/null 2>&1 && systemctl is-enabled "$timer" >/dev/null 2>&1; then
    run systemctl disable --now "$timer"
  else
    say "    ${timer} not present/enabled — nothing to disable"
  fi
  say "    NOTE: deploy the per-user watchdog for ${OS_USER} as a SEPARATE step"
  say "    (bubble-vps-platform's telegram_watchdog pyinfra task, once ${SLUG}'s"
  say "    tenant.yaml carries os_user=${OS_USER})."
}

step_a_bootstrap_user
step_b_seed_claude_json
step_c_register_plugin
step_d_strip_settings_env
step_e_copy_codex_and_state
step_f_clear_stale_tmp_logs
step_g_disable_old_watchdog

say "done. Next: scripts/deploy-to-morty.sh --slug=${SLUG} --os-user=${OS_USER} (a SEPARATE step —"
say "this script never touches the live unit), then run the RUNBOOK-1120 §4.2 verification steps."
