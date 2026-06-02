#!/usr/bin/env bash
# =============================================================================
# host-prep-sandbox-rollback.sh
# =============================================================================
# PURPOSE
#   Reverse host-prep-sandbox.sh cleanly.
#
# WHAT THIS DOES (default = posture-only revert)
#   1. ALWAYS: remove /etc/apparmor.d/bwrap and reload AppArmor.
#        >>> THIS IS THE SECURITY-RELEVANT REVERT <<<
#        Removing the profile RESTORES Ubuntu 24.04's default restriction on
#        unprivileged user namespaces for /usr/bin/bwrap. After this, the
#        sandbox can no longer create the userns it needs (sandbox is back to
#        "cannot run"). This is the host-level posture change being undone.
#
#   2. OPT-IN (--purge only): also
#        - npm uninstall -g @anthropic-ai/sandbox-runtime
#        - apt-get remove -y bubblewrap socat
#      These packages are ADDITIVE and HARMLESS (presence alone changes no
#      posture — only the AppArmor profile re-permits userns). So by DEFAULT we
#      LEAVE THEM INSTALLED to avoid disturbing anything else on the box that
#      might use them. Pass --purge to remove them too.
#
#   This script does NOT touch: managed-settings.json, agents, systemd services,
#   secrets, sops, sudoers, or any guard.
#
# SAFETY
#   - Idempotent: if the profile is already gone / packages already absent, it
#     reports that and does not error.
#   - Root-only: refuses unless EUID 0.
#   - Verifies after; prints per-step PASS/FAIL + a final GREEN/RED.
#
# INVOKE
#     sudo bash host-prep-sandbox-rollback.sh            # remove AppArmor profile only (default)
#     sudo bash host-prep-sandbox-rollback.sh --purge    # also npm/apt remove the deps
# =============================================================================

set -euo pipefail

ok()   { printf '  \033[32m[PASS]\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; }
info() { printf '  \033[34m[INFO]\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

OVERALL_OK=1
fail_overall() { OVERALL_OK=0; }

PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help)
      grep '^# ' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      echo "Usage: sudo bash host-prep-sandbox-rollback.sh [--purge]" >&2
      exit 2 ;;
  esac
done

if [ "$(id -u)" != "0" ]; then
  echo "ERROR: this script must run as root." >&2
  echo "Invoke with:  sudo bash host-prep-sandbox-rollback.sh [--purge]" >&2
  exit 1
fi

APPARMOR_PROFILE_PATH="/etc/apparmor.d/bwrap"
NPM_PKG="@anthropic-ai/sandbox-runtime"

cat <<BANNER

=============================================================================
 host-prep-sandbox-rollback.sh  —  revert sandbox host-prep
 Mode: $( [ "${PURGE}" -eq 1 ] && echo "FULL PURGE (profile + npm + apt removal)" || echo "DEFAULT (remove AppArmor profile only)" )
=============================================================================
BANNER

# =============================================================================
# STEP 1 — remove the AppArmor profile (the posture revert) — ALWAYS
# =============================================================================
step "Step 1 — remove ${APPARMOR_PROFILE_PATH} (restores userns restriction)"

AA_STATUS_BIN=""
if command -v aa-status >/dev/null 2>&1; then
  AA_STATUS_BIN="aa-status"
elif command -v apparmor_status >/dev/null 2>&1; then
  AA_STATUS_BIN="apparmor_status"
fi

if [ -f "${APPARMOR_PROFILE_PATH}" ]; then
  # Unload the live profile first (best-effort), then remove the file.
  if command -v apparmor_parser >/dev/null 2>&1; then
    info "unloading live profile (apparmor_parser -R)"
    apparmor_parser -R "${APPARMOR_PROFILE_PATH}" 2>/dev/null || info "apparmor_parser -R returned non-zero (profile may already be unloaded) — continuing"
  fi
  info "removing ${APPARMOR_PROFILE_PATH}"
  rm -f "${APPARMOR_PROFILE_PATH}"
  info "reloading AppArmor (systemctl reload apparmor)"
  systemctl reload apparmor || info "systemctl reload apparmor returned non-zero — profile file already removed, restriction restored on next reload/boot"
else
  info "${APPARMOR_PROFILE_PATH} already absent — nothing to remove"
fi

# verify the profile is gone
if [ ! -f "${APPARMOR_PROFILE_PATH}" ]; then
  ok "profile file removed"
else
  bad "profile file still present"
  fail_overall
fi
if [ -n "${AA_STATUS_BIN}" ]; then
  if "${AA_STATUS_BIN}" 2>/dev/null | grep -qw 'bwrap'; then
    bad "AppArmor still reports a 'bwrap' profile loaded (may clear on reboot)"
    fail_overall
  else
    ok "AppArmor no longer reports a 'bwrap' profile"
  fi
else
  info "no aa-status/apparmor_status to verify live unload (file removed regardless)"
fi

# =============================================================================
# STEP 2 — opt-in package removal (--purge)
# =============================================================================
if [ "${PURGE}" -eq 1 ]; then
  step "Step 2 — --purge: remove npm helper + apt packages"

  # npm
  if command -v npm >/dev/null 2>&1 && npm ls -g "${NPM_PKG}" >/dev/null 2>&1; then
    info "npm uninstall -g ${NPM_PKG}"
    npm uninstall -g "${NPM_PKG}" || info "npm uninstall returned non-zero"
  else
    info "${NPM_PKG} not installed (or npm absent) — skip"
  fi
  if command -v npm >/dev/null 2>&1 && npm ls -g "${NPM_PKG}" >/dev/null 2>&1; then
    bad "${NPM_PKG} still installed"; fail_overall
  else
    ok "${NPM_PKG} not installed"
  fi

  # apt
  APT_REMOVE=()
  command -v bwrap >/dev/null 2>&1 && APT_REMOVE+=("bubblewrap")
  command -v socat >/dev/null 2>&1 && APT_REMOVE+=("socat")
  if [ "${#APT_REMOVE[@]}" -gt 0 ]; then
    info "apt-get remove -y ${APT_REMOVE[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get remove -y "${APT_REMOVE[@]}" || info "apt-get remove returned non-zero"
  else
    info "bubblewrap/socat already absent — skip"
  fi
  command -v bwrap >/dev/null 2>&1 && { bad "bwrap still present"; fail_overall; } || ok "bwrap removed"
  command -v socat >/dev/null 2>&1 && { bad "socat still present"; fail_overall; } || ok "socat removed"
else
  step "Step 2 — package removal SKIPPED (default)"
  info "bubblewrap, socat, and ${NPM_PKG} left installed (additive/harmless)."
  info "Re-run with --purge to remove them."
fi

# =============================================================================
# FINAL SUMMARY
# =============================================================================
echo
echo "============================ FINAL SUMMARY ============================"
[ ! -f "${APPARMOR_PROFILE_PATH}" ] && ok "AppArmor bwrap profile removed (userns restriction restored)" || { bad "AppArmor profile still present"; fail_overall; }
if [ "${PURGE}" -eq 1 ]; then
  command -v bwrap >/dev/null 2>&1 && { bad "bwrap still installed"; fail_overall; } || ok "bwrap removed"
  command -v socat >/dev/null 2>&1 && { bad "socat still installed"; fail_overall; } || ok "socat removed"
  npm ls -g "${NPM_PKG}" >/dev/null 2>&1 && { bad "${NPM_PKG} still installed"; fail_overall; } || ok "${NPM_PKG} removed"
else
  info "deps intentionally left in place (default mode)"
fi
echo "======================================================================"

if [ "${OVERALL_OK}" -eq 1 ]; then
  printf '\033[1;32m\nGREEN — rollback complete. Sandbox posture reverted.\033[0m\n'
  exit 0
else
  printf '\033[1;31m\nRED — rollback incomplete. Review [FAIL] lines above.\033[0m\n'
  exit 1
fi
