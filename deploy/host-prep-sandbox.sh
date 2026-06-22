#!/usr/bin/env bash
# =============================================================================
# host-prep-sandbox.sh
# =============================================================================
# PURPOSE
#   Bring the Bubble Invest VPS (Hetzner, Ubuntu 24.04.4) from
#   "Claude Code sandbox CANNOT run" to "sandbox dependencies READY".
#   Layer B of agent hardening (OS-level Bash sandbox via bubblewrap).
#
# WHAT THIS CHANGES AT HOST LEVEL (review before running)
#   1. apt:  installs `bubblewrap` (bwrap) + `socat` (network relay).
#   2. npm:  installs `@anthropic-ai/sandbox-runtime` globally — the OPTIONAL
#            seccomp helper that adds Unix-domain-socket blocking.
#   3. AppArmor: writes /etc/apparmor.d/bwrap and reloads AppArmor.
#        >>> SECURITY-RELEVANT <<<  Ubuntu 24.04 ships
#        `kernel.apparmor_restrict_unprivileged_userns = 1`, which blocks the
#        unprivileged user namespace bwrap needs. This profile RE-PERMITS
#        unprivileged userns FOR THE /usr/bin/bwrap BINARY ONLY (not for the
#        commands bwrap runs inside the sandbox). This is a deliberate
#        host-level posture change. Reverse it with host-prep-sandbox-rollback.sh.
#
#   This script does NOT touch: managed-settings.json, agents, systemd services,
#   secrets, sops, sudoers, or any guard. It ONLY installs deps + the profile.
#
# APPROVAL
#   Approved by {{OPERATOR}} (Telegram msg 3621, 2026-06-02): host-prep approved,
#   fixture-first, quality over speed. Reference:
#     projects/bubble-ops-loop/deploy/sandbox-tests/CONTEXT.md §4
#     projects/bubble-ops-loop/deploy/SANDBOX-SCOPING.md (TL;DR remediation)
#
# SAFETY
#   - Idempotent: detects already-done state, skips, never errors on re-run.
#   - Root-only: refuses unless EUID 0.
#   - Verifies after every step; prints per-step PASS/FAIL + a final GREEN/RED.
#   - WRITTEN by the host-prep-scoper subagent; EXECUTED by Rick + {{OPERATOR}} by hand
#     after review. Do not auto-run.
#
# INVOKE
#     sudo bash host-prep-sandbox.sh
# =============================================================================

set -euo pipefail

# ---- pretty logging ---------------------------------------------------------
ok()   { printf '  \033[32m[PASS]\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; }
info() { printf '  \033[34m[INFO]\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

OVERALL_OK=1
fail_overall() { OVERALL_OK=0; }

# ---- root gate --------------------------------------------------------------
if [ "$(id -u)" != "0" ]; then
  echo "ERROR: this script must run as root." >&2
  echo "Invoke with:  sudo bash host-prep-sandbox.sh" >&2
  exit 1
fi

APPARMOR_PROFILE_PATH="/etc/apparmor.d/bwrap"
NPM_PKG="@anthropic-ai/sandbox-runtime"

cat <<'BANNER'

=============================================================================
 host-prep-sandbox.sh  —  install Claude Code sandbox dependencies
 Target: Ubuntu 24.04 (Hetzner). Approved by {{OPERATOR}} (msg 3621, 2026-06-02).
 Changes: apt(bubblewrap,socat) + npm(sandbox-runtime) + AppArmor bwrap profile.
=============================================================================
BANNER

# =============================================================================
# STEP 1 — apt: bubblewrap + socat
# =============================================================================
step "Step 1/3 — apt packages: bubblewrap, socat"

NEED_APT=()
if command -v bwrap >/dev/null 2>&1; then
  info "bwrap already present ($(command -v bwrap)) — skip"
else
  NEED_APT+=("bubblewrap")
fi
if command -v socat >/dev/null 2>&1; then
  info "socat already present ($(command -v socat)) — skip"
else
  NEED_APT+=("socat")
fi

if [ "${#NEED_APT[@]}" -gt 0 ]; then
  info "installing: ${NEED_APT[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y "${NEED_APT[@]}"
else
  info "both apt packages already installed — nothing to do"
fi

# verify
if command -v bwrap >/dev/null 2>&1; then ok "bwrap present: $(command -v bwrap)"; else bad "bwrap MISSING after install"; fail_overall; fi
if command -v socat >/dev/null 2>&1; then ok "socat present: $(command -v socat)"; else bad "socat MISSING after install"; fail_overall; fi

# =============================================================================
# STEP 2 — npm: @anthropic-ai/sandbox-runtime (optional seccomp helper)
# =============================================================================
step "Step 2/3 — npm global: ${NPM_PKG} (seccomp / unix-socket helper)"

if ! command -v npm >/dev/null 2>&1; then
  bad "npm not found on PATH — cannot install ${NPM_PKG}"
  fail_overall
elif npm ls -g "${NPM_PKG}" >/dev/null 2>&1; then
  info "${NPM_PKG} already installed globally — skip"
  ok "${NPM_PKG} present"
else
  info "installing ${NPM_PKG} globally"
  npm install -g "${NPM_PKG}"
  if npm ls -g "${NPM_PKG}" >/dev/null 2>&1; then
    ok "${NPM_PKG} installed"
  else
    bad "${NPM_PKG} MISSING after install"
    fail_overall
  fi
fi

# =============================================================================
# STEP 3 — AppArmor profile for /usr/bin/bwrap (re-permit unprivileged userns)
# =============================================================================
step "Step 3/3 — AppArmor profile ${APPARMOR_PROFILE_PATH}"

# Profile content is the exact form from the official docs
# (https://code.claude.com/docs/en/sandboxing) and CONTEXT.md §4.
# abi <abi/4.0> matches AppArmor 4.x on Ubuntu 24.04 (parser 4.0.1, abi/4.0 present).
read -r -d '' DESIRED_PROFILE <<'EOF' || true
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF

# Only re-permit userns if the kernel actually restricts it (docs guidance).
RESTRICT=""
if RESTRICT=$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null); then
  info "kernel.apparmor_restrict_unprivileged_userns = ${RESTRICT}"
else
  info "kernel.apparmor_restrict_unprivileged_userns key absent"
fi
if [ "${RESTRICT}" != "1" ]; then
  info "userns restriction not enforced (key absent or 0); profile still installed for determinism/idempotency, harmless either way"
fi

# idempotent write: only rewrite if content differs
NEED_RELOAD=1
if [ -f "${APPARMOR_PROFILE_PATH}" ] && [ "$(cat "${APPARMOR_PROFILE_PATH}")" = "${DESIRED_PROFILE}" ]; then
  info "profile already present with exact content — skip write"
  NEED_RELOAD=0
else
  info "writing ${APPARMOR_PROFILE_PATH}"
  printf '%s\n' "${DESIRED_PROFILE}" > "${APPARMOR_PROFILE_PATH}"
  chmod 0644 "${APPARMOR_PROFILE_PATH}"
fi

if [ "${NEED_RELOAD}" -eq 1 ]; then
  info "reloading AppArmor (systemctl reload apparmor)"
  if systemctl reload apparmor; then
    ok "apparmor reloaded"
  else
    info "systemctl reload failed; trying direct apparmor_parser -r"
    if apparmor_parser -r "${APPARMOR_PROFILE_PATH}"; then
      ok "profile loaded via apparmor_parser -r"
    else
      bad "could not load AppArmor profile"
      fail_overall
    fi
  fi
else
  info "no reload needed (profile unchanged)"
fi

# verify profile loaded. aa-status / apparmor_status are the same binary on this box.
AA_STATUS_BIN=""
if command -v aa-status >/dev/null 2>&1; then
  AA_STATUS_BIN="aa-status"
elif command -v apparmor_status >/dev/null 2>&1; then
  AA_STATUS_BIN="apparmor_status"
fi

if [ -n "${AA_STATUS_BIN}" ]; then
  if "${AA_STATUS_BIN}" 2>/dev/null | grep -qw 'bwrap'; then
    ok "AppArmor reports a 'bwrap' profile loaded (${AA_STATUS_BIN})"
  else
    bad "AppArmor does NOT report a 'bwrap' profile (${AA_STATUS_BIN})"
    fail_overall
  fi
else
  bad "neither aa-status nor apparmor_status found — cannot verify profile load"
  fail_overall
fi

# Best-effort functional smoke test: bwrap can now create a userns.
if command -v bwrap >/dev/null 2>&1; then
  if bwrap --ro-bind / / --unshare-user --uid 0 true >/dev/null 2>&1; then
    ok "bwrap userns smoke test succeeded (unprivileged userns works)"
  else
    info "bwrap userns smoke test did not pass under root context; not fatal — Claude Code runs bwrap as the agent user. Verify via /sandbox Dependencies tab."
  fi
fi

# =============================================================================
# FINAL SUMMARY
# =============================================================================
echo
echo "============================ FINAL SUMMARY ============================"
command -v bwrap  >/dev/null 2>&1 && ok "bwrap installed"        || { bad "bwrap missing";  fail_overall; }
command -v socat  >/dev/null 2>&1 && ok "socat installed"        || { bad "socat missing";  fail_overall; }
npm ls -g "${NPM_PKG}" >/dev/null 2>&1 && ok "${NPM_PKG} installed" || { bad "${NPM_PKG} missing"; fail_overall; }
[ -f "${APPARMOR_PROFILE_PATH}" ] && ok "AppArmor profile file present" || { bad "AppArmor profile file missing"; fail_overall; }
if [ -n "${AA_STATUS_BIN}" ] && "${AA_STATUS_BIN}" 2>/dev/null | grep -qw 'bwrap'; then
  ok "AppArmor bwrap profile loaded"
else
  bad "AppArmor bwrap profile NOT loaded"; fail_overall
fi
echo "======================================================================"

if [ "${OVERALL_OK}" -eq 1 ]; then
  printf '\033[1;32m\nGREEN — sandbox dependencies are READY.\033[0m\n'
  echo "Next: restart Claude Code, run /sandbox, confirm the Dependencies tab is all green."
  echo "Sandbox stays fail-open (failIfUnavailable:false) until T1 is green — do NOT flip it here."
  exit 0
else
  printf '\033[1;31m\nRED — one or more steps failed. Review [FAIL] lines above. Box NOT ready.\033[0m\n'
  exit 1
fi
